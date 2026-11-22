// dsh-notifier inbound/qq-gw.mjs
// QQ 官方机器人 WebSocket 网关入站（v0.3.0 阶段 2）：裸协议实现，零 SDK 依赖。
// 背景：QQ 官方 Node SDK（bot-node-sdk → qq-guild-bot）改名两次后事实弃维，
// 活下去的是协议本身——WS 网关收事件 + REST 发消息 + getAppAccessToken 换 token，
// 全是裸 HTTP/WS，原生 fetch + globalThis.WebSocket（Node 22 内置）即可。
// v0.14 统一网关模式（transport='gateway'，配置 gatewayUrl+gatewaySecret 启用）：
//   入站事件源换成 noti-gateway 的 SSE（/stream?types=qq_event，HMAC v1 签名），
//   出站发送换成 /n/{source} raw_qq 透传；QQ WS 协议层（HELLO/IDENTIFY/心跳/RESUME）
//   与交互回应（PUT /interactions）全部由网关托管，本进程不再持有 QQ 凭证——
//   零 QQ 直连。直连模式（transport='direct'）原样保留，未配网关时行为不变。
//  - 事件：C2C_MESSAGE_CREATE（单聊）/ GROUP_AT_MESSAGE_CREATE（群 @，仅被 @ 时投递）
//  - 网关协议：op10 HELLO → op2 IDENTIFY（或 op6 RESUME）→ op1/op11 心跳；
//    op0 DISPATCH 携带 s 序号（心跳带回）；op7 RECONNECT / op9 INVALID_SESSION 走重连
//  - 审批：v0.8.4 按钮化（2026-08 实测平台已开放 markdown+内嵌键盘）：审批卡优先
//    msg_type=2 + keyboard 长形式（content.rows），回调按钮 action.data 携带契约
//    协议「ap:<decision>:<approvalKey>:<token>」（与 telegram/飞书同构，HMAC 验签）；
//    发送失败自动降级文本编号回复（能力探测免配置）
//  - 点击回传：INTERACTION_CREATE（intent INTERACTION 1<<26）经本 WS 网关推送，
//    收到后立即异步 PUT /interactions/{id} ACK（3s 窗口），再把显式 key 裁决送 bus.decide
//  - intents 默认 GROUP_AND_C2C(1<<25) | INTERACTION(1<<26)；被@/单聊/按钮点击都在这两档
// 军规：任何异常只 warn 不抛；断线指数退避重连（RESUME 优先）；stop() 清干净全部定时器。
// 频控：Bot 维度 60qpm ≈ 1 条/秒（复用出站 qq-bot 的限速门经验值）；被动回复
// （带 msg_id 关联事件）有独立配额，5 条内免主动消息权限。

import { createHmac, createHash, randomBytes } from 'node:crypto'
import { createTokenManager, createRateGate, normalizeTtlMs } from '../adapters/_tokens.mjs'
import { setBounded, createThrottledWarn } from './_bounded.mjs'
import { resolveNotifyTargets } from './target-guard.mjs'
import { buildApprovalAction, parseApprovalAction, buildQuestionAction, parseQuestionAction } from './_contract.mjs'
import { parseQQImageMessage, parseQqAttachments } from './message.mjs'
import { splitByCodePoints } from './segment.mjs'
import { stringsOf } from '../strings.mjs'
import { createLog } from '../log.mjs'

const TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
const DEFAULT_API_BASE = 'https://api.sgroup.qq.com'
const INTENT_GROUP_AND_C2C = 1 << 25
const INTENT_INTERACTION = 1 << 26 // INTERACTION_CREATE：消息按钮点击回调（v0.8.4 按钮化）
const CHAT_STATE_MAX = 1024
/** 纯附件（无正文）占位正文：仅在信封确有附件时使用，投递链会剥离（conversation.mjs）。 */
const IMAGE_PLACEHOLDER_TEXT = '[图片消息]'
const FILE_PLACEHOLDER_TEXT = '[文件消息]'
// 出站单条上限按 Unicode 码点计（非 UTF-16 码元）：文本 2000 / Markdown 3000（官方限制）。
// 码点语义经 splitByCodePoints 保证——码元切片会把星体平面字符切成孤立代理项（G-22 同根）。
const QQ_TEXT_MAX_CODEPOINTS = 2000
const QQ_MARKDOWN_MAX_CODEPOINTS = 3000
/** G-22：被动回复条数配额（QQ 官方平台限制：c2c 4 条 / 群 5 条，按同一 msg_id 的被动回复计）。
 *  超限时平台静默丢弃（无错误码、无回执）——本侧不硬阻塞投递（硬阻塞把「可能仍送达」
 *  变成「必然不送达」），仅 warn 出声让丢弃可见。主动消息（无 msg_id）不受此配额约束。 */
const QQ_PASSIVE_REPLY_QUOTA = { user: 4, group: 5 }
/** QQ keyboard 按钮 label 上限 = **15 码点**（实测值，非参考实现取值）。
 *  原实现沿用腾讯 dsh-qqbot 参考实现的 BUTTON_LABEL_MAX=10，比平台真实上限保守 5 个字符，
 *  导致 11~15 字的选项被提前截成「1. 一次性验证…」。2026-10-28 实测：向平台直接发送
 *  10/15/20/30 字的 label，15 字完整显示、20 字起被省略号截断，故取 15。
 *  按码点截断（不用 .length 的 UTF-16 语义——emoji/星体字符按码元切会把代理项劈成
 *  两半，G-22 同根），超长取前 14 码点补省略号。 */
const QQ_BUTTON_LABEL_MAX_CODEPOINTS = 15

// WS op codes（QQ 网关协议）
const OP_DISPATCH = 0
const OP_HEARTBEAT = 1
const OP_IDENTIFY = 2
const OP_RESUME = 6
const OP_RECONNECT = 7
const OP_INVALID_SESSION = 9
const OP_HELLO = 10
const OP_HEARTBEAT_ACK = 11

/** G-07：close 4008（连接被限速）是服务端给出的硬性等待窗——按官方 SDK 语义固定等
 *  60s 再重连，不走指数退避（限流期短退避重连等于连续撞墙，反而加剧限流）。 */
const CLOSE_4008_WAIT_MS = 60000

/**
 * 解析并校验 inbound.qq 配置。
 * @param {object} raw - inbound.qq 原始配置
 * @param {{ credentials?: object }} [options] - 扫码落盘凭证回退（store 'qq:account'，config 显式配置优先）
 * @returns {{ ok: true, config: object } | { ok: false, reason: string }}
 */
export function resolveQqInboundConfig(raw, options = {}) {
  const cfg = (raw !== null && typeof raw === 'object') ? raw : {}
  const creds = (options.credentials !== null && typeof options.credentials === 'object') ? options.credentials : {}
  const appId = String(cfg.appId ?? creds.appId ?? '').trim()
  const appSecret = String(cfg.appSecret ?? creds.appSecret ?? '').trim()
  // v0.14 统一网关模式：配置 gatewayUrl + gatewaySecret 即经 noti-gateway 接入
  // （入站 = 网关 SSE /stream?types=qq_event，出站 = /n/{source} raw_qq 透传）。
  // QQ WS 协议层（HELLO/IDENTIFY/心跳/RESUME/交互回应）由网关托管，本进程不再
  // 需要 appId/appSecret——QQ 凭证只存在于网关侧（keys.conf/config.yaml）。
  const gatewayUrl = String(cfg.gatewayUrl ?? '').trim().replace(/\/+$/, '')
  const gatewaySecret = String(cfg.gatewaySecret ?? '').trim()
  const gatewaySource = String(cfg.gatewaySource ?? '').trim() || 'dsh'
  const gatewayKeyId = String(cfg.gatewayKeyId ?? '').trim() || (gatewaySecret !== '' ? gatewaySource : '')
  const isGateway = gatewayUrl !== '' && gatewaySecret !== ''
  if (!isGateway) {
    if (appId === '' || appSecret === '') {
      return { ok: false, reason: `QQ inbound 需要 gatewayUrl+gatewaySecret（统一网关模式，当前 gatewayUrl ${gatewayUrl !== '' ? '已配置' : '缺失'}，gatewaySecret ${gatewaySecret !== '' ? '已配置' : '缺失'}），或 appId+appSecret（直连模式，当前 appId ${appId !== '' ? '已配置' : '缺失'}，appSecret ${appSecret !== '' ? '已配置' : '缺失'}）` }
    }
  }
  const notifyUsers = (Array.isArray(cfg.notifyUsers) ? cfg.notifyUsers : []).map((id) => String(id).trim()).filter((id) => id !== '')
  const notifyGroups = (Array.isArray(cfg.notifyGroups) ? cfg.notifyGroups : []).map((id) => String(id).trim()).filter((id) => id !== '')
  const intents = Number.isInteger(cfg.intents) && cfg.intents >= 0 ? cfg.intents : (INTENT_GROUP_AND_C2C | INTENT_INTERACTION)
  return {
    ok: true,
    config: {
      // transport: 'gateway'（统一网关）| 'direct'（QQ WS 直连，legacy 保留）
      transport: isGateway ? 'gateway' : 'direct',
      gatewayUrl,
      gatewaySecret,
      gatewayKeyId,
      gatewaySource,
      appId,
      appSecret,
      apiBase: (String(cfg.apiBase ?? '').trim() || DEFAULT_API_BASE).replace(/\/+$/, ''),
      intents,
      notifyUsers,
      notifyGroups,
      timeoutMs: Math.min(60000, Math.max(1000, Number(cfg.timeoutMs) || 10000)),
    },
  }
}

/**
 * G-40：群消息 @ 机器人占位白名单化——仅剥已证实形态，绝不「假定剥不掉也无害」。
 * 已证实形态：`<@!数字ID>`、`<@数字ID>`（官方占位）与行首 `@名字+空格`（纯文本形态）。
 * 未命中白名单但形似提及（以 @ / <@ 开头）→ 保留原文并由调用方 debug 出声：
 * @ 残片会污染 agent 语境与 /pair 参数；平台若改格式，日志可见而非静默漏剥。
 * 官方文档称群 content 已自动去 @ 前缀（docs/protocol-preflight/qq-bot.md），但真机
 * 样本不足——白名单+出声是「漏剥可见」与「误剥可见」之间的保守中点。
 */
const MENTION_WHITELIST = [
  /^<@!\d+>\s*/, // 官方占位形态一：<@!数字ID>（占位自定界，尾随空格可缺省）
  /^<@\d+>\s*/, // 官方占位形态二：<@数字ID>
  /^@\S+\s+/, // 纯文本形态：行首 @名字+空格（空格是「名字结束」判据，缺空格视为未知）
]

/** QQ keyboard 按钮 label 截断（#26 对齐腾讯 dsh-qqbot `button-utils.ts` 的
 *  BUTTON_LABEL_MAX=10）：整体 ≤10 码点，超长取前 9 码点补省略号。按码点计
 *  不产生孤立代理项（G-22 同根）。 */
function clampButtonLabel(label) {
  const points = Array.from(String(label ?? '').trim())
  if (points.length <= QQ_BUTTON_LABEL_MAX_CODEPOINTS) return points.join('')
  return points.slice(0, QQ_BUTTON_LABEL_MAX_CODEPOINTS - 1).join('') + '…'
}

/** 剥离群消息行首的 @ 机器人占位；返回 { text, matched, mentionLike }（matched=命中
 *  白名单已剥；mentionLike=形似提及但未命中，调用方据此 debug 出声）。 */
function stripMention(content) {
  const text = String(content ?? '')
  for (const pattern of MENTION_WHITELIST) {
    if (pattern.test(text)) return { text: text.replace(pattern, '').trim(), matched: true }
  }
  return { text: text.trim(), matched: false, mentionLike: /^(@|<@)/.test(text) }
}

/**
 * 收集事件 d 负载上的全部附件（#36）：官方 `attachments` 段优先（图片/文件，逐项白名单归一），
 * 当官方段未给出图片时回落文档假设形状的 `extra` 图片段（真机证据不足，保持过渡支持）。
 * 任一段形状未知一律丢弃，绝不伪装成文本或控制字段。
 * @param {object} d - 消息事件负载
 * @returns {Array<{ kind: 'image'|'file', image?: object, file?: object }>}
 */
function collectMessageAttachments(d) {
  const list = parseQqAttachments(d)
  if (!list.some((item) => item.kind === 'image')) {
    const legacy = parseQQImageMessage(d)
    if (legacy !== null) list.unshift(legacy)
  }
  return list
}

/** 纯附件（无正文）占位正文：有图片用图片占位，否则用文件占位（投递链会剥离）。 */
const placeholderOf = (attachments) =>
  (attachments.some((item) => item.kind === 'image') ? IMAGE_PLACEHOLDER_TEXT : FILE_PLACEHOLDER_TEXT)

/**
 * 信封的附件字段：规范形状 `attachments: []`；同时保留首图 `image` 单项字段（过渡期消费者
 * 仍读它，见 conversation.mjs::collectAttachments），无正文时附 `kind` 供下游识别纯附件消息。
 */
function attachmentFields(attachments, withoutText) {
  if (attachments.length === 0) return {}
  const firstImage = attachments.find((item) => item.kind === 'image')
  return {
    attachments,
    ...(firstImage === undefined ? {} : { image: firstImage.image }),
    ...(withoutText ? { kind: attachments[0].kind } : {}),
  }
}

/**
 * 审批按钮负载（v0.8.4）：直接复用契约协议 `ap:<decision>:<approvalKey>:<token>`
 * （buildApprovalAction/parseApprovalAction，与 telegram/feishu callback_data 完全
 * 同构，复用同一套 HMAC token 核销）。key+token 在卡片发送时写死进按钮——点击回传
 * 按显式 key 精确命中并验签，杜绝「最近待决」隐式匹配在多行并存/僵尸行/并行竞速下
 * 的目标劫持（2026-08-23 事故的病根）。
 */
/** 解析按钮回调数据；非契约格式 → null（调用方静默忽略）。见 parseApprovalAction。 */

/**
 * 创建 QQ 官方机器人入站通道（统一契约；v0.8.4 buttons=true——审批优先按钮卡片，
 * 发送失败自动降级文本编号回复）。
 * @param {object} options
 * @param {{ appId: string, appSecret: string, apiBase?: string, intents?: number,
 *           notifyUsers?: string[], notifyGroups?: string[], timeoutMs?: number }} options.config
 * @param {ReturnType<typeof import('./bus.mjs').createInboundBus>} options.bus
 * @param {string[]} [options.fallbackTargets] - 未配置 notifyUsers/Groups 时的推送目标（全局白名单回落，按单聊用户处理）
 * @param {object} [options.logger]
 * @param {typeof fetch} [options.fetchImpl] - fetch 注入（测试用）
 * @param {typeof WebSocket} [options.webSocketImpl] - WebSocket 构造器注入（测试用；默认 globalThis.WebSocket）
 * @param {number} [options.reconnectBaseMs=1000] - 重连退避基数
 * @param {number} [options.reconnectCapMs=30000] - 重连退避上限
 * @param {number} [options.handshakeTimeoutMs=10000] - WS open/HELLO/READY 各阶段截止时间
 * @param {object} [options.strings] - stringsOf(lang) 全文案表（读 `qq` 节，跨节复用
 *   approval.fallbackText；缺省回落 zh——须先在 strings.mjs 落 `qq` 节）
 */
export function createQqInbound(options = {}) {
  const { config, bus, fallbackTargets = [], logger = null, identity = null } = options
  const STRINGS = options.strings ?? stringsOf()
  const t = STRINGS.qq
  // 防御性兜底：绕过 resolveQqInboundConfig 直接构造时也保证 apiBase 可用
  const apiBase = (String(config?.apiBase ?? '').trim() || DEFAULT_API_BASE).replace(/\/+$/, '')
  const fetchImpl = options.fetchImpl ?? globalThis.fetch?.bind(globalThis)
  const WebSocketImpl = options.webSocketImpl ?? globalThis.WebSocket
  const reconnectBaseMs = Math.max(1, Number(options.reconnectBaseMs) || 1000)
  const reconnectCapMs = Math.max(reconnectBaseMs, Number(options.reconnectCapMs) || 30000)
  const handshakeTimeoutMs = Math.max(10, Number(options.handshakeTimeoutMs) || 10000)
  // G-07：close 4008 的固定等待窗（默认 60s；测试注入缩短，不参与指数退避）
  const close4008WaitMs = Math.max(0, Number(options.close4008WaitMs) || CLOSE_4008_WAIT_MS)
  const ackThreshold = Number(options.maxMissedAcks)
  const maxMissedAcks = Number.isFinite(ackThreshold) && ackThreshold >= 1 ? Math.min(10, Math.floor(ackThreshold)) : 2

  // v0.14 统一网关模式（transport==='gateway'）：事件源换成 noti-gateway SSE
  // （/stream?types=qq_event，HMAC v1 签名），发送走 /n/{source} raw_qq 透传——
  // payload 即 QQ 消息 API 的 body（msg_type/content/markdown/keyboard/msg_seq/msg_id），
  // scene（user/group）由网关 target 配置决定。直连模式（transport==='direct'）行为不变。
  const GATEWAY = String(config?.transport ?? '') === 'gateway'
  const gwUrl = String(config?.gatewayUrl ?? '').replace(/\/+$/, '')
  const gwSecret = String(config?.gatewaySecret ?? '')
  const gwKeyId = String(config?.gatewayKeyId ?? '') || gwSourceFallback(config)
  const gwSource = String(config?.gatewaySource ?? '') || 'dsh'
  // SSE 载荷里带的 bot appid（网关广播 qq_event 时附上）；直连模式恒为空。
  let sseBotId = ''
  /** 信封 accountId：直连=appId；网关=SSE bot 字段（回落 gatewaySource）。
   *  绑定/配对按 userId 索引，accountId 仅作账号标注——单账号部署语义等价。 */
  const accountIdOf = () => String(config?.appId ?? '') || sseBotId || gwSource
  function gwSourceFallback(cfg) {
    return String(cfg?.gatewaySource ?? '') || 'dsh'
  }

  // 统一日志出口：故障(warn/error)双写可见；连接生命周期(info)只进 logger，
  // 不再让「已就绪 / 断线恢复 / 会话轮转」每次重连都刷控制台。
  const log = createLog({ logger, tag: '[dsh-notifier-gate/inbound:qq]' })
  const warn = (message) => log.warn(message)
  const info = (message) => log.info(message)
  const debug = (message) => log.debug(message)
  const evictionWarn = createThrottledWarn(warn, { intervalMs: 1000 })
  const onEvict = (key) => evictionWarn((count) => `目标类型学习表达上限：淘汰 ${count} 个旧目标（最近淘汰 ${String(key).slice(0, 32)}）`)

  // 连接生命周期计数（日志降噪用）：4009/op7 这类"平台侧正常会话轮转"在稳态下会周期性
  // 出现，只让首次出声，之后降为 debug，避免日志被「已就绪/闭码」刷成看起来像故障。
  let sessionRotationCount = 0
  let readyCount = 0

  // #31 入站 transport 有限超时：config.timeoutMs 此前仅解析从未注入——换 token/发消息/
  // 回执/卡片全走裸 fetchImpl（无 signal），宿主线程或网络卡死时这些调用可无限挂起。
  // 注入 AbortController+signal（对齐 telegram-bot.mjs 先例与 _shared.mjs postJson 超时
  // 语义）；超时 AbortError 由调用方 catch：token 失败进 token 管理器既有传播，发送失败
  // 走既有 warn+降级，不盲目重试（结果未知时重试会重复投递，见 _shared.mjs G-50 同根）。
  // 数值归一（clamp [1000,60000]）由 resolveQqInboundConfig 负责，此处只兜底缺省值。
  const requestTimeoutMs = Number(config?.timeoutMs) || 10000
  function fetchWithTimeout(url, init = {}) {
    if (fetchImpl === undefined) return Promise.reject(new Error('当前运行时无 fetch，QQ inbound 不可用'))
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), requestTimeoutMs)
    return Promise.resolve(fetchImpl(url, { ...init, signal: controller.signal }))
      .then(
        (response) => { clearTimeout(timer); return response },
        (error) => { clearTimeout(timer); throw error },
      )
  }

  // ---------- 统一网关模式：HMAC 签名 + raw_qq 发送 + SSE 事件源 ----------

  /** HMAC v1 签名头（canonical = METHOD\nPATH\nTS\nNONCE\nSHA256(body)——网关接入文档 §3）。
   *  签名 PATH 不含查询串（?wait=1 等只出现在 URL）。 */
  function gatewaySignHeaders(method, path, bodyRaw) {
    const ts = String(Math.floor(Date.now() / 1000))
    const nonce = randomBytes(8).toString('hex')
    const bodyHex = createHash('sha256').update(bodyRaw, 'utf8').digest('hex')
    const canonical = [method, path, ts, nonce, bodyHex].join('\n')
    const sig = 'v1=' + createHmac('sha256', gwSecret).update(canonical, 'utf8').digest('hex')
    return {
      'x-noti-signature': sig,
      'x-noti-timestamp': ts,
      'x-noti-nonce': nonce,
      'x-noti-key-id': gwKeyId,
    }
  }

  /** 从网关同步回执（{"ok":true,"results":{...,"detail":"msg_id=..."}}）提取 QQ msg_id。 */
  function msgIdOfGatewayResult(result) {
    try {
      for (const value of Object.values(result?.results ?? {})) {
        const m = /msg_id=([A-Za-z0-9_-]+)/.exec(String(value?.detail ?? ''))
        if (m !== null && m[1] !== '') return m[1]
      }
    } catch { /* 回执形状未知不影响发送成功语义 */ }
    return null
  }

  /** 经统一网关发送 raw_qq 透传载荷（body = QQ 消息 API body；成功返回网关回执对象）。 */
  async function gwSendRawQq(body) {
    if (fetchImpl === undefined) throw new Error('当前运行时无 fetch，QQ inbound 不可用')
    const payload = JSON.stringify({ raw_qq: body })
    const signPath = `/n/${encodeURIComponent(gwSource)}`
    const response = await fetchWithTimeout(`${gwUrl}${signPath}?wait=1`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...gatewaySignHeaders('POST', signPath, payload) },
      body: payload,
    })
    const result = await response.json().catch(() => null)
    if (!response.ok || (result !== null && typeof result === 'object' ? result.ok !== true : true)) {
      const detail = result === null ? '(响应非 JSON)' : JSON.stringify(result).slice(0, 300)
      throw new Error(`网关 raw_qq 发送失败（HTTP ${response.status}）: ${detail}`)
    }
    return result ?? {}
  }

  // token 管理器（换 token → 缓存 → 提前刷新 → 失效作废），与出站 qq-bot 同一套逻辑
  const tokens = createTokenManager(async () => {
    if (fetchImpl === undefined) throw new Error('当前运行时无 fetch，QQ inbound 不可用')
    const response = await fetchWithTimeout(TOKEN_URL, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ appId: config.appId, clientSecret: config.appSecret }),
    })
    const payload = await response.json().catch(() => null)
    if (typeof payload?.access_token !== 'string' || payload.access_token === '') {
      throw new Error(`换取 access_token 失败（HTTP ${response.status}）：检查 appId/appSecret`)
    }
    // G-55：与出站 qq-bot 同一归一——expires_in 非法（非有限/≤0）不再 || 7200 掩盖
    return { token: payload.access_token, expiresInMs: normalizeTtlMs(Number(payload.expires_in) * 1000, 'expires_in') }
  })
  const rateGate = createRateGate(1050) // 60qpm ≈ 1 条/秒

  // 运行态
  let running = false
  let startPromise = null
  let stopRequested = false
  let ws = null
  let sseAbort = null // 统一网关模式：SSE 读循环的 AbortController（stop 时中止）
  let heartbeatTimer = null
  let heartbeatIntervalMs = null
  let heartbeatArmed = false
  let lastSeq = null
  let sessionId = null
  let awaitingAck = false
  let missedAcks = 0
  let reconnectAttempts = 0
  let reconnectTimer = null
  let handshakeTimer = null
  // 发送侧运行态：目标类型学习表（事件来时记下 chatId 是单聊还是群）+ 每目标 msg_seq
  const targetKinds = new Map() // chatId -> 'user' | 'group'
  const msgSeqs = new Map() // chatId -> 递增 seq

  function targetKindOf(chatId) {
    const learned = targetKinds.get(String(chatId))
    if (learned !== undefined) return learned
    return (config.notifyGroups ?? []).includes(String(chatId)) ? 'group' : 'user'
  }

  function scheduleReconnect({ resume = false, delayMs = null } = {}) {
    if (stopRequested) return
    if (reconnectTimer !== null) return
    // G-07：4008（连接被限速）服务端给出的是硬性等待窗，不用指数退避——按计划固定 60s
    const delay = delayMs !== null ? Math.max(0, delayMs)
      : Math.min(reconnectBaseMs * 2 ** reconnectAttempts, reconnectCapMs)
    reconnectAttempts += 1
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      if (!resume || sessionId === null) {
        sessionId = null
        lastSeq = null
      }
      connect().catch((error) => {
        warn(`重连失败: ${error instanceof Error ? error.message : String(error)}`)
        scheduleReconnect()
      })
    }, delay)
  }

  function cleanupSocket() {
    if (handshakeTimer !== null) { clearTimeout(handshakeTimer); handshakeTimer = null }
    if (heartbeatTimer !== null) { clearTimeout(heartbeatTimer); heartbeatTimer = null }
    // 连接级心跳运行态复位：断开/重连/cleanup 后不得继承上一连接的脏 arming/ACK/miss 状态。
    // heartbeatIntervalMs 保留——重连后同一会话的节奏由新连接 HELLO 重新下发（仅作兜底用）。
    heartbeatArmed = false
    awaitingAck = false
    missedAcks = 0
    if (ws !== null) {
      try { ws.removeAllListeners?.() } catch { /* fake/运行时差异 */ }
      try { ws.close() } catch { /* 已关闭 */ }
      ws = null
    }
    if (sseAbort !== null) {
      try { sseAbort.abort() } catch { /* 已中止 */ }
      sseAbort = null
    }
  }

  function armHandshakeDeadline(phase, conn) {
    if (handshakeTimer !== null) clearTimeout(handshakeTimer)
    handshakeTimer = setTimeout(() => {
      handshakeTimer = null
      if (stopRequested || ws !== conn) return
      warn(`QQ 网关 ${phase} 超时（${handshakeTimeoutMs}ms），主动断开并重连`)
      cleanupSocket()
      scheduleReconnect({ resume: sessionId !== null })
    }, handshakeTimeoutMs)
  }

  function clearHandshakeDeadline() {
    if (handshakeTimer !== null) { clearTimeout(handshakeTimer); handshakeTimer = null }
  }

  /** G-07：关闭码分支表（对齐官方 SDK reconnect 语义，dsh-im qqbot-connector 同源）：
   *  - 4004 认证失败：token 已被平台吊销——作废 token 缓存（此前全文件无一处
   *    tokens.invalidate()，带着死凭证无限重连直到 TTL 自然过期）+ 弃会话重 IDENTIFY
   *  - 4008 连接被限速：服务端硬性等待窗，固定等（不走指数退避）；会话仍有效走 RESUME
   *  - 4006 seq 非法 / 4007 shard 无效 / 4009 会话超时：会话不可恢复，弃之重 IDENTIFY
   *  - 其余（1000 正常关闭 / 1006 异常断开 / 无码）：现行为——RESUME 优先 + 指数退避 */
  function scheduleReconnectForClose(code) {
    if (code === 4004) {
      warn('QQ 网关认证失败（close 4004）：作废 access_token 缓存重取，弃会话重新 IDENTIFY')
      tokens.invalidate()
      sessionId = null
      lastSeq = null
      scheduleReconnect({ resume: false })
      return
    }
    if (code === 4008) {
      warn(`QQ 网关连接被限速（close 4008）：按服务端等待窗固定 ${Math.round(close4008WaitMs / 1000)}s 后 RESUME（不走指数退避）`)
      scheduleReconnect({ resume: true, delayMs: close4008WaitMs })
      return
    }
    if (code === 4006 || code === 4007 || code === 4009) {
      // 4009 是 QQ 平台侧的常规会话轮转（同账号多端登录/服务端换节点都会触发），
      // 重连成功后一切正常。若每次都用 warn 打印「闭码 + 已就绪」两条，稳态下会
      // 刷成日志噪音，看起来像插件在反复出错。这里改为：
      //  - 只记一行，带累计次数；首次 warn 之后降为 debug（仍可在详细日志里查到）
      //  - 「已就绪」那条同样只在首次连接时出声
      // 真正需要关注的 4004/4008 仍然无条件 warn。
      sessionRotationCount += 1
      const note = `QQ 网关闭码 ${code}：会话不可恢复，弃会话重新 IDENTIFY（第 ${sessionRotationCount} 次会话轮转，属正常重连）`
      if (sessionRotationCount === 1) warn(note)
      else debug(note)
      sessionId = null
      lastSeq = null
      scheduleReconnect({ resume: false })
      return
    }
    scheduleReconnect({ resume: true })
  }

  async function fetchGatewayUrl() {
    const token = await tokens.get()
    const response = await fetchWithTimeout(`${apiBase}/gateway`, {
      headers: { authorization: `QQBot ${token}` },
    })
    const payload = await response.json().catch(() => null)
    if (typeof payload?.url !== 'string' || payload.url === '') {
      throw new Error(`获取 WS 网关地址失败（HTTP ${response.status}）`)
    }
    return payload.url
  }

  function sendFrame(payload) {
    if (ws === null || ws.readyState !== 1) return false
    try { ws.send(JSON.stringify(payload)) ; return true } catch { return false }
  }

  /** Issue #23：HELLO 只记录本连接的 heartbeat_interval，不发送、也不启动心跳。
   *  真机 A/B 证据：QQ 网关只对 READY/RESUMED 之后发出的心跳回 OP_HEARTBEAT_ACK，
   *  鉴权完成前起搏只会白耗且永远收不到 ACK，必然走到阈值判死死循环。故此函数
   *  只落盘 interval 并复位连接级心跳运行态，真正的起搏交给 armHeartbeat()（READY/RESUMED 触发）。 */
  function recordHeartbeatInterval(intervalMs) {
    heartbeatIntervalMs = intervalMs
    heartbeatArmed = false
    awaitingAck = false
    missedAcks = 0
  }

  /** Issue #23：READY/RESUMED 后才幂等启动心跳，并立即发送第一拍（携带当时最新 lastSeq）。
   *  幂等：重复 READY/RESUMED 不得创建多个定时器或重复首拍。
   *  单拍丢 ACK 且未达阈值时：记一次 miss、告警、仍发送下一拍（恢复路径不断）。 */
  function armHeartbeat() {
    if (heartbeatArmed) return
    if (heartbeatIntervalMs === null) return
    heartbeatArmed = true
    awaitingAck = false
    missedAcks = 0
    const beat = () => {
      if (heartbeatTimer === null) return // 连接已清理，本闭包随旧定时器废弃
      if (awaitingAck) {
        missedAcks += 1
        if (missedAcks < maxMissedAcks) {
          warn(`心跳 ACK 已连续丢失 ${missedAcks}/${maxMissedAcks} 拍（下一拍仍无 ACK 才断线重连）`)
        } else {
          warn(`心跳 ACK 已连续丢失 ${missedAcks}/${maxMissedAcks} 拍，判死主动断开重连`)
          cleanupSocket()
          scheduleReconnect({ resume: true })
          return // 达到阈值：本拍不再发送（清 socket 后经 RESUME 优先重连）
        }
      }
      if (!sendFrame({ op: OP_HEARTBEAT, d: lastSeq })) return
      awaitingAck = true
    }
    if (heartbeatTimer !== null) clearTimeout(heartbeatTimer)
    heartbeatTimer = setInterval(beat, Math.max(50, heartbeatIntervalMs))
    beat()
  }

  function handleDispatch(t, d) {
    if (t === 'READY') {
      clearHandshakeDeadline()
      reconnectAttempts = 0
      sessionId = String(d?.session_id ?? '') || null
      readyCount += 1
      // 稳态下会话轮转会反复 READY：首次出声，之后降 debug（避免日志刷屏）
      // 就绪属正常生命周期：info（只进 logger）；重复轮转降 debug。
      if (readyCount === 1) info(`QQ 网关已就绪（session ${sessionId ?? '?'}）`)
      else debug(`QQ 网关已就绪（session ${sessionId ?? '?'}，第 ${readyCount} 次）`)
      armHeartbeat() // Issue #23：鉴权完成（READY）后才幂等启动心跳，立即发首拍
      return
    }
    if (t === 'RESUMED') {
      clearHandshakeDeadline()
      reconnectAttempts = 0
      info('QQ 网关断线恢复（RESUME 成功，事件不丢）')
      armHeartbeat() // Issue #23：RESUMED 后同样起搏（幂等，避免重复 READY/RESUMED 建多定时器）
      return
    }
    try {
      if (t === 'C2C_MESSAGE_CREATE') {
        const userId = String(d?.author?.user_openid ?? '')
        const messageId = String(d?.id ?? '')
        const text = String(d?.content ?? '').trim()
        // #36：官方 `attachments` 段为规范来源（图片/文件），`extra` 图片段为其过渡兜底。
        // 两者都只保留已知字段；malformed/未知形状的项直接丢弃，绝不变成文本或控制数据。
        const attachments = collectMessageAttachments(d)
        if (messageId === '' || userId === '' || (text === '' && attachments.length === 0)) return
        setBounded(targetKinds, userId, 'user', CHAT_STATE_MAX, onEvict)
        // v0.7：accept 返回值消费——拒绝/命令回执不再已读不回。
        // msg_id 必带（R5 审查 R5-3-P2-3：C2C 不带 msg_id 走主动消息额度，真机大概率被
        // 平台 4xx 拒掉——mock fetch 不校验被动回复权限，单测测不出；带 msg_id 走被动回复）
        const envelope = {
          channel: 'qq', accountId: accountIdOf(), userId, chatId: userId, messageId,
          chatType: 'private',
          text: text || placeholderOf(attachments),
          ...attachmentFields(attachments, text === ''),
        }
        const result = bus.accept(envelope)
        if (result?.reply !== undefined) {
          postMessage(userId, String(result.reply), messageId).catch((error) => {
            warn(`回执发送失败: ${error instanceof Error ? error.message : String(error)}`) // 回执失败不致命
          })
        }
        return
      }
      if (t === 'GROUP_AT_MESSAGE_CREATE') {
        const userId = String(d?.author?.member_openid ?? '')
        const chatId = String(d?.group_openid ?? '')
        const messageId = String(d?.id ?? '')
        // G-40：白名单未命中但形似提及 → 保留原文 + debug 出声（@ 残片会污染 agent 语境
        // 与 /pair 参数；平台改格式时靠日志发现而非静默漏剥）。头部截 32 字符便于采样。
        const mention = stripMention(d?.content)
        if (mention.mentionLike === true) {
          debug(`群消息 @ 形态未命中白名单，已保留原文（QQ @ 占位真机样本不足，出现即需采样登记）: ${JSON.stringify(String(d?.content ?? '').slice(0, 32))}`)
        }
        const text = mention.text
        // #36：群 @ 事件与 C2C 同源（官方 `attachments` 段），同样只收已知字段。
        const attachments = collectMessageAttachments(d)
        if (messageId === '' || userId === '' || chatId === '' || (text === '' && attachments.length === 0)) return
        setBounded(targetKinds, chatId, 'group', CHAT_STATE_MAX, onEvict)
        // v0.7：群聊拒绝回执发回群（含「请私聊发送 /pair」引导）
        const result = bus.accept({
          channel: 'qq', accountId: accountIdOf(), userId, chatId, messageId, chatType: 'group',
          text: text || placeholderOf(attachments),
          ...attachmentFields(attachments, text === ''),
        })
        if (result?.reply !== undefined) {
          postMessage(chatId, String(result.reply), messageId).catch((error) => {
            warn(`回执发送失败: ${error instanceof Error ? error.message : String(error)}`) // 回执失败不致命
          })
        }
        return
      }
      if (t === 'INTERACTION_CREATE') {
        // v0.8.4 按钮回调（type=11 消息按钮）：先异步 ACK（PUT /interactions，3s 窗口，
        // 失败只影响客户端转圈不致命），再解析 apv 载荷送 bus——显式 key 裁决，
        // 不做任何隐式匹配；非本插件按钮静默忽略。
        const type = Number(d?.type ?? d?.data?.type ?? 0)
        if (type !== 11) return
        const interactionId = String(d?.id ?? '')
        const buttonData = String(d?.data?.resolved?.button_data ?? '')
        const userId = String(d?.group_member_openid ?? d?.user_openid ?? '')
        const groupOpenId = String(d?.group_openid ?? '')
        const userOpenId = String(d?.user_openid ?? '')
        const chatId = String(groupOpenId || userOpenId)
        if (interactionId === '' || userId === '' || chatId === '') return
        void ackInteraction(interactionId).catch((error) => {
          warn(`互动 ACK 失败: ${error instanceof Error ? error.message : String(error)}`)
        })
        setBounded(targetKinds, chatId, chatId === userId ? 'user' : 'group', CHAT_STATE_MAX, onEvict)
        const chatType = groupOpenId !== '' ? 'group' : 'private'
        const parsed = parseApprovalAction(buttonData)
        const question = parseQuestionAction(buttonData)
        if (question !== null) {
          const result = bus.accept({ channel: 'qq', accountId: accountIdOf(), userId, chatId, chatType, messageId: interactionId,
            text: `[提问按钮:${question.optIdx}] ${question.qKey}`,
            questionAction: question })
          if (result?.reply !== undefined) postMessage(chatId, String(result.reply), interactionId).catch(() => {})
          return
        }
        if (parsed === null) return
        const result = bus.accept({
          channel: 'qq',
          accountId: accountIdOf(),
          userId,
          chatId,
          chatType,
          messageId: interactionId,
          text: `[审批按钮:${parsed.decision}] ${parsed.approvalKey}`,
          approvalAction: { decision: parsed.decision, approvalKey: parsed.approvalKey, token: parsed.token },
        })
        if (result?.reply !== undefined) {
          postMessage(chatId, String(result.reply), interactionId).catch((error) => {
            warn(`按钮回执发送失败: ${error instanceof Error ? error.message : String(error)}`)
          })
        }
        return
      }
    } catch (error) {
      warn(`事件处理异常: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  /** 发送 IDENTIFY（新会话）或 RESUME（断线恢复）；token 换取失败交给重连兜底。 */
  function sendAuth({ resume = false } = {}) {
    tokens.get().then((token) => {
      if (resume && sessionId !== null) {
        sendFrame({ op: OP_RESUME, d: { token: `QQBot ${token}`, session_id: sessionId, seq: lastSeq } })
        return
      }
      sendFrame({
        op: OP_IDENTIFY,
        d: {
          token: `QQBot ${token}`,
          intents: config.intents ?? (INTENT_GROUP_AND_C2C | INTENT_INTERACTION),
          shard: [0, 1],
          properties: { $os: 'dsh-notifier', $browser: 'dsh-notifier', $device: 'dsh-notifier' },
        },
      })
    }).catch((error) => {
      warn(`${resume ? 'RESUME' : 'IDENTIFY'} 失败（token 换取异常）: ${error instanceof Error ? error.message : String(error)}`)
      cleanupSocket()
      scheduleReconnect()
    })
  }

  function handleFrame(raw) {
    let frame
    try { frame = JSON.parse(raw) } catch { return }
    if (typeof frame?.s === 'number') lastSeq = frame.s
    if (frame.op === OP_HELLO) {
      const intervalMs = Number(frame.d?.heartbeat_interval) || 30000
      // Issue #23：HELLO 只记录本连接 heartbeat_interval 并发送鉴权，不启动心跳。
      // 心跳须待 READY/RESUMED 鉴权完成后由 armHeartbeat() 幂等启动（真机 A/B 证据：
      // 网关只对鉴权完成后的心跳回 OP_HEARTBEAT_ACK，提前起搏永远收不到 ACK 而死循环）。
      recordHeartbeatInterval(intervalMs)
      if (ws !== null) armHandshakeDeadline('READY/RESUMED', ws)
      sendAuth({ resume: sessionId !== null })
      return
    }
    if (frame.op === OP_HEARTBEAT_ACK) {
      awaitingAck = false
      missedAcks = 0
      return
    }
    if (frame.op === OP_DISPATCH) {
      handleDispatch(frame.t, frame.d)
      return
    }
    if (frame.op === OP_RECONNECT) {
      // op7 是平台常规的"换节点/主动要求重连"指令，稳态下会周期性出现，
      // 不属于故障——降为 debug，避免与 4009 一起刷屏。
      debug('服务端要求重连（op7）')
      cleanupSocket()
      scheduleReconnect({ resume: true })
      return
    }
    if (frame.op === OP_INVALID_SESSION) {
      // G-21：按官方 SDK 语义看 d 标志——d:true 会话仍可恢复（保留 session 走 RESUME，
      // 事件续传不丢）；d:false 会话已死才弃之重 IDENTIFY。旧实现一律弃会话：
      // 每次可恢复失效都多付一次 IDENTIFY 握手 + 丢失续传窗。
      const resumable = frame.d === true
      warn(`会话失效（op9）：${resumable ? '可恢复，保留 session 走 RESUME' : '不可恢复，弃会话重新 IDENTIFY'}`)
      if (!resumable) {
        sessionId = null
        lastSeq = null
      }
      cleanupSocket()
      scheduleReconnect({ resume: resumable })
    }
  }

  /** 统一网关模式事件源：SSE 订阅 /stream?types=qq_event（HMAC 签名），阻塞读循环。
   *  断流/异常抛出，由 start() 的重连环按指数退避重连。事件负载
   *  {"event","bot","data"} → handleDispatch(event, data)（与 QQ WS dispatch 同构）；
   *  bot 字段记录为信封 accountId；交互回应由网关自动完成（本侧 ackInteraction 空转）。 */
  async function connectSse() {
    if (fetchImpl === undefined) throw new Error('当前运行时无 fetch，QQ inbound 不可用')
    const controller = new AbortController()
    sseAbort = controller
    let response
    try {
      response = await fetchImpl(`${gwUrl}/stream?types=qq_event`, {
        headers: { accept: 'text/event-stream', ...gatewaySignHeaders('GET', '/stream', '') },
        signal: controller.signal,
      })
    } catch (error) {
      if (stopRequested || controller.signal.aborted) return
      throw error
    }
    if (response.status !== 200 || !response.body) {
      throw new Error(`网关 SSE 订阅失败（HTTP ${response.status}）`)
    }
    info('统一网关 SSE 已连接（types=qq_event）')
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let eventType = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx).replace(/\r$/, '')
        buffer = buffer.slice(idx + 1)
        if (line === '' || line.startsWith(':')) { continue } // 空行/keepalive 注释
        if (line.startsWith('event: ')) { eventType = line.slice(7).trim(); continue }
        if (line.startsWith('data: ') && eventType !== '') {
          let parsed = null
          try { parsed = JSON.parse(line.slice(6)) } catch { parsed = null }
          if (parsed !== null && typeof parsed === 'object') {
            if (typeof parsed.bot === 'string' && parsed.bot !== '') sseBotId = parsed.bot
            try { handleDispatch(String(parsed.event ?? eventType), parsed.data ?? {}) } catch (error) {
              warn(`事件处理异常: ${error instanceof Error ? error.message : String(error)}`)
            }
          }
        }
      }
    }
    if (!stopRequested) throw new Error('网关 SSE 流结束')
  }

  async function connect() {
    if (GATEWAY) return connectSse()
    const url = await fetchGatewayUrl()
    if (stopRequested) return
    if (WebSocketImpl === undefined) throw new Error('当前运行时无 WebSocket（需要 Node 22+）')
    ws = new WebSocketImpl(url)
    const conn = ws
    armHandshakeDeadline('open', conn)
    ws.addEventListener('open', () => {
      if (ws !== conn) return
      armHandshakeDeadline('HELLO', conn)
    })
    ws.addEventListener('message', (event) => {
      // Issue #23：连接级隔离——旧连接（已被 cleanupSocket 置 ws=null 或换成新连接）的
      // 迟到帧（含 op11 ACK）不得触碰新连接的心跳运行态或 reset 新连接的 awaitingAck。
      if (ws !== conn) return
      try { handleFrame(typeof event.data === 'string' ? event.data : String(event.data)) } catch (error) {
        warn(`帧处理异常: ${error instanceof Error ? error.message : String(error)}`)
      }
    })
    ws.addEventListener('close', (event) => {
      cleanupSocket()
      // G-07：关闭码带语义——4004 刷 token / 4008 固定窗 / 4006/4007/4009 弃会话
      scheduleReconnectForClose(Number(event?.code))
    })
    ws.addEventListener('error', () => { /* close 会跟着来，重连在 close 里统一调度 */ })
  }

  /** 发文本：超长按码点分段逐条发送（每段独立 msg_seq，服务端按 msg_id+msg_seq 去重）。
   *  G-22 同根修复：旧 slice(0, 2000) 是 UTF-16 码元语义，第 2000 码元恰落在星体平面
   *  字符（emoji/生僻字）中间时产生孤立代理项（JSON 载荷非法，平台拒收或乱码）。
   *  分段点优先落在句末标点：硬切会把一句切成两条消息（后半截以残句开头，用户需自行
   *  拼接）；无标点的长段落（纯 emoji、大段代码）退化为等长硬切，与历史行为一致。
   *  G-22 配额：被动回复（携带 msg_id）分段数超平台配额时 warn 出声但不阻塞（超限部分
   *  平台静默丢弃，先让丢弃可见——见 QQ_PASSIVE_REPLY_QUOTA 注释）。
   *  任一段失败即抛错（已发段不撤回，与 iLink 分块语义一致）。 */
  async function postMessage(chatId, content, msgId = undefined) {
    if (fetchImpl === undefined) return null
    const target = String(chatId)
    const kind = targetKindOf(target)
    const chunks = splitByCodePoints(String(content ?? ''), QQ_TEXT_MAX_CODEPOINTS, { preferSentenceBoundary: true })
    const pieces = chunks.length > 0 ? chunks : ['']
    if (msgId !== undefined) {
      const quota = kind === 'group' ? QQ_PASSIVE_REPLY_QUOTA.group : QQ_PASSIVE_REPLY_QUOTA.user
      if (pieces.length > quota) {
        warn(`被动回复分段 ${pieces.length} 条超过${kind === 'group' ? '群' : 'c2c'}配额 ${quota} 条：超限部分平台将静默丢弃（本侧不硬阻塞，已照发）: ${target}`)
      }
    }
    let lastId = null
    for (const piece of pieces) {
      await rateGate.gate()
      const seq = (msgSeqs.get(target) ?? 0) + 1
      setBounded(msgSeqs, target, seq, CHAT_STATE_MAX, onEvict)
      const body = { content: piece, msg_type: 0, msg_seq: seq }
      if (msgId !== undefined) body.msg_id = msgId
      if (GATEWAY) {
        // 统一网关模式：raw_qq 透传（scene 由网关 target 决定，本侧只管消息体）
        const result = await gwSendRawQq(body)
        lastId = msgIdOfGatewayResult(result) ?? `qq:${target}:${seq}`
        continue
      }
      const token = await tokens.get()
      const url = kind === 'group'
        ? `${apiBase}/v2/groups/${target}/messages`
        : `${apiBase}/v2/users/${target}/messages`
      const response = await fetchWithTimeout(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `QQBot ${token}` },
        body: JSON.stringify(body),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || (typeof payload?.code === 'string' && payload.code !== '')) {
        throw new Error(`QQ 发送失败（HTTP ${response.status}${payload?.code ? ` code ${payload.code}` : ''}: ${payload?.message ?? ''}）`)
      }
      lastId = typeof payload?.id === 'string' && payload.id !== '' ? payload.id : `qq:${target}:${seq}`
    }
    return lastId
  }

  /** 互动事件回执（PUT /interactions/{id}，50QPS）：3 秒窗口内告知平台已受理，
   *  否则用户端按钮一直 loading。失败由调用方 catch（不致命）。
   *  统一网关模式下不执行——网关 qqws 收到 INTERACTION_CREATE 即自动回应，
   *  本侧再 PUT 属重复回应（且直连 PUT 需要 QQ 凭证，网关模式不持凭证）。 */
  async function ackInteraction(interactionId) {
    if (GATEWAY) return
    if (fetchImpl === undefined) return
    const token = await tokens.get()
    await fetchWithTimeout(`${apiBase}/interactions/${encodeURIComponent(interactionId)}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json', authorization: `QQBot ${token}` },
      body: JSON.stringify({ code: 0 }),
    })
  }

  /** 发送 markdown + 内嵌键盘（审批按钮卡片，msg_type=2）。失败抛错，调用方降级文本。
   *  统一网关模式：raw_qq 透传整张卡片（keyboard 与 markdown 同体，网关转发到 target）。 */
  async function postMarkdownWithKeyboard(chatId, markdownContent, keyboard) {
    if (fetchImpl === undefined) return null
    await rateGate.gate()
    const target = String(chatId)
    const seq = (msgSeqs.get(target) ?? 0) + 1
    setBounded(msgSeqs, target, seq, CHAT_STATE_MAX, onEvict)
    // Markdown+键盘是单张卡片：超长只按码点截断（取首块），不拆多卡——键盘必须与卡片
    // 同体，拆卡会重复按钮/permission；码点截断同样不产生孤立代理项（G-22 同根）。
    const markdownText = splitByCodePoints(String(markdownContent ?? ''), QQ_MARKDOWN_MAX_CODEPOINTS)[0] ?? ''
    const body = { msg_type: 2, msg_seq: seq, markdown: { content: markdownText }, keyboard }
    if (GATEWAY) {
      const result = await gwSendRawQq(body)
      return msgIdOfGatewayResult(result) ?? `qq-kb:${target}:${seq}`
    }
    const kind = targetKindOf(target)
    const url = kind === 'group'
      ? `${apiBase}/v2/groups/${target}/messages`
      : `${apiBase}/v2/users/${target}/messages`
    const token = await tokens.get()
    const response = await fetchWithTimeout(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `QQBot ${token}` },
      body: JSON.stringify(body),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || (typeof payload?.code === 'string' && payload.code !== '')) {
      throw new Error(`QQ 按钮卡片发送失败（HTTP ${response.status}${payload?.code ? ` code ${payload.code}` : ''}: ${payload?.message ?? ''}）`)
    }
    return typeof payload?.id === 'string' && payload.id !== '' ? payload.id : `qq-kb:${target}:${seq}`
  }

  return {
    channel: 'qq',
    accountId: accountIdOf(),
    // v0.8.4：按钮化落地（发送失败自动降级文本，capabilities 仅影响文案分流）
    capabilities: { buttons: true },

    /** 启动网关连接（幂等；失败中文 warn 后允许再次 start 重试）。
     *  统一网关模式：connectSse() 阻塞在读循环，断流后在本 promise 内按指数退避
     *  重连（与直连模式 close→scheduleReconnect 语义对齐）；连接存活超过退避上限
     *  视为已恢复，重置退避计数。 */
    start() {
      if (running || startPromise !== null) return
      running = true
      stopRequested = false
      startPromise = (async () => {
        if (GATEWAY) {
          for (;;) {
            if (stopRequested) return
            const connectedAt = Date.now()
            try {
              await connectSse()
              if (stopRequested) return
              warn('统一网关 SSE 流结束')
            } catch (error) {
              if (stopRequested) return
              warn(`统一网关 SSE 断开: ${error instanceof Error ? error.message : String(error)}`)
            }
            if (Date.now() - connectedAt > reconnectCapMs) reconnectAttempts = 0
            const delay = Math.min(reconnectBaseMs * 2 ** reconnectAttempts, reconnectCapMs)
            reconnectAttempts += 1
            await new Promise((resolve) => { reconnectTimer = setTimeout(resolve, delay) })
            reconnectTimer = null
          }
        }
        try {
          await connect()
        } catch (error) {
          running = false
          startPromise = null
          const reason = error instanceof Error ? error.message : String(error)
          warn(`QQ inbound 启动失败（本通道不可用，不影响其他通道）: ${reason}`)
        }
      })()
    },

    /** 停止并清理全部定时器/连接（幂等）。
     *  先 cleanupSocket（中止 SSE 读循环/关闭 WS）再 await startPromise——v0.14 修复
     *  死锁：网关模式的 startPromise 要等读循环退出，而读循环退出靠本侧 abort。 */
    async stop() {
      stopRequested = true
      running = false
      if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null }
      cleanupSocket()
      try { await startPromise } catch { /* 启动失败不影响停止 */ }
      // Issue #23：stop 后复位全部连接级心跳运行态，保证 restart 能重新正常握手与起搏。
      heartbeatArmed = false
      heartbeatIntervalMs = null
      awaitingAck = false
      missedAcks = 0
      startPromise = null
    },

    /** 审批推送目标（v0.7 三级解析）：绑定成员 → notifyUsers → 全局回落（仅绑定表整体空）；
     *  notifyGroups 是渠道属性（群通知）不是身份属性，无条件并入——绑定表接管用户
     *  目标不等于群通知就此消失（v0.6 行为保留）。 */
    notifyTargets() {
      return resolveNotifyTargets({
        identity,
        channel: 'qq',
        configTargets: (Array.isArray(config.notifyUsers) ? config.notifyUsers : []).map(String),
        fallbackTargets,
        extraTargets: (Array.isArray(config.notifyGroups) ? config.notifyGroups : []).map(String),
      })
    },

    /** 推审批通知（v0.8.4）：优先 markdown+内嵌键盘——两颗回调按钮（type 1）action.data
     *  携带契约协议「ap:<decision>:<approvalKey>:<token>」，click_limit=1 防重复，
     *  permission: { type: 2 }（对齐腾讯 dsh-qqbot approval-renderer.ts，无 specify_user_ids
     *  ——#26 移除未经证实的 provider 字段；权限已在 Control Core 的 token/account/user/chat
     *  + pending + 一次性裁决完成）。发送失败自动降级文本编号回复（无感切换）。 */
    async sendApprovalCard({ chatId, title, content, approvalKey, token }) {
      if (typeof approvalKey === 'string' && approvalKey !== '' && typeof token === 'string' && token !== '') {
        try {
          const isUserTarget = targetKindOf(chatId) === 'user'
          if (!isUserTarget) throw new Error('群聊审批仅提供文本回退，禁止可见按钮')
          const button = (id, label, visitedLabel, style, decision) => ({
            id,
            render_data: { label: clampButtonLabel(label), visited_label: clampButtonLabel(visitedLabel), style },
            action: {
              // type 必须 1（回调按钮：点击产生 INTERACTION_CREATE 推送到本网关）。
              // type 2 是「指令按钮」——客户端会把 data 当文本消息自动发出，不产生
              // 回调事件（2026-08-23 实测踩坑：官方 overview 示例的 type:2 是指令语义）。
              type: 1,
              permission: { type: 2 },
              click_limit: 1,
              data: buildApprovalAction(decision, approvalKey, token),
            },
          })
          const keyboard = {
            content: {
              rows: [
                { buttons: [
                  button('btn_approve', t.approveLabel, t.approveVisited, 1, 'allowed-once'),
                  button('btn_reject', t.rejectLabel, t.rejectVisited, 2, 'rejected'),
                ] },
              ],
            },
          }
          const markdown = `${title}\n${content}\n\n${t.markdownFooter(isUserTarget)}`
          const messageId = await postMarkdownWithKeyboard(chatId, markdown, keyboard)
          if (messageId !== null) return { messageId }
        } catch (error) {
          warn(`按钮卡片发送失败，本次降级文本审批: ${error instanceof Error ? error.message : String(error)}`)
        }
      }
      try {
        const text = STRINGS.approval.fallbackText(title, content)
        const messageId = await postMessage(chatId, text)
        return messageId !== null ? { messageId } : null
      } catch (error) {
        warn(`审批通知发送失败: ${error instanceof Error ? error.message : String(error)}`)
        return null
      }
    },

    async sendQuestionCard({ chatId, title, content, qKey, token, options = [] }) {
      try {
        const isUserTarget = targetKindOf(chatId) === 'user'
        if (!isUserTarget) return null // 群聊不展示可操作提问卡，避免成员间信息/权限泄漏
        // 按钮标签只放编号「1」「2」…：正文已经用 `**1.** 选项内容` 列了清单，
        // 按钮再塞一遍长文本既挤（15 码点上限）又要和正文两处对读。
        // 编号即下标 +1，点击回调仍按 optIdx 精确结算。
        //
        // 布局：每个选项一个按钮，**两个一行（两排两列）**——一列一个大按钮在手机上
        // 会把卡片拉得很长，两列更好点也更好看。奇数个时最后一个独占一行。
        const optionButtons = options.map((label, index) => ({
          id: `q_${index}`,
          render_data: {
            label: clampButtonLabel(String(index + 1)),
            visited_label: clampButtonLabel(String(index + 1)),
            style: 0,
          },
          action: { type: 1, permission: { type: 2 }, click_limit: 1, data: buildQuestionAction(qKey, String(index), token) },
        }))
        const rows = []
        for (let i = 0; i < optionButtons.length; i += 2) rows.push({ buttons: optionButtons.slice(i, i + 2) })
        // 「自定义回答」单独一行、占满整行：它不是选项之一，混进编号格里容易误点。
        rows.push({
          buttons: [{
            id: 'q_custom',
            render_data: { label: clampButtonLabel(t.customAnswerLabel), visited_label: clampButtonLabel(t.selectedVisited), style: 0 },
            action: { type: 1, permission: { type: 2 }, click_limit: 1, data: buildQuestionAction(qKey, 'c', token) },
          }],
        })
        const messageId = await postMarkdownWithKeyboard(chatId, `${title}
${content}`, { content: { rows } })
        return messageId === null ? null : { messageId }
      } catch (error) {
        warn(`提问卡片发送失败，降级编号: ${error instanceof Error ? error.message : String(error)}`)
        return null
      }
    },

    /** 消息不可编辑：以回执文本补一条结果（尽力而为）。 */
    async editResolved(target, text) {
      if (target?.chatId === undefined || String(target.chatId) === '') return
      try {
        await postMessage(target.chatId, t.resultLine(text))
      } catch (error) {
        warn(`审批结果回执失败: ${error instanceof Error ? error.message : String(error)}`)
      }
    },

    /** 发普通文本（命令回执 / 被动回复）。 */
    async sendText(chatId, text, msgId = undefined) {
      try {
        return (await postMessage(chatId, text, msgId)) !== null
      } catch (error) {
        warn(`回执发送失败: ${error instanceof Error ? error.message : String(error)}`)
        return false
      }
    },
  }
}
