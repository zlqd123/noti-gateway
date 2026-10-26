// dsh-notifier-gate adapter: qq-bot（QQ 官方机器人）
// v0.14 起优先走统一通知网关（noti-gateway）：配置 gatewayUrl/gatewayToken 后，
// 纯文本/Markdown 推送经网关汇入统一配额池（落盘+重试+死信+多工具共享）；
// 网关不可达/失败自动回退下方直连路径（需 appId/appSecret/targetId 齐全）。
// 直连为两步式：POST getAppAccessToken（appId+clientSecret）→ POST /v2/groups|users/<id>/messages。
// 鉴权头 `Authorization: QQBot <access_token>`；msg_seq 每条递增（服务端按 seq 去重）。
// Bot 维度 60qpm ≈ 1 条/秒：内置限速门；单群主动消息 1000 条/日（2026-06-22 官方文档确认开放）。
// 换 token 流程移植自 all-pusher-api（Apache-2.0）src/QQBot.ts，改写为零依赖 fetch。
// 注意：群/单聊「主动消息」需在 QQ 开放平台为机器人开启相应场景权限。

import { postJson, str, num, NotifyError, ERROR_CODES } from './_shared.mjs'
import { createTokenManager, createRateGate, normalizeTtlMs } from './_tokens.mjs'
import { splitByCodePoints } from '../inbound/segment.mjs'

export const type = 'qq-bot'

const TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
const DEFAULT_API_BASE = 'https://api.sgroup.qq.com'
// 出站单条上限按 Unicode 码点计：文本 2000 / Markdown 3000（官方限制，与 qq-gw 同源）。
// 码点语义经 splitByCodePoints 保证——码元切片会把星体平面字符切成孤立代理项（G-22 同根）。
const QQ_TEXT_MAX_CODEPOINTS = 2000
const QQ_MARKDOWN_MAX_CODEPOINTS = 3000

/** 校验并归一化配置；缺失抛中文指引。配置了 gatewayUrl 即网关模式（直连凭据降级为可选兜底）。 */
export function resolve(cfg = {}) {
  const gatewayUrl = str(cfg.gatewayUrl).replace(/\/+$/, '')
  const gatewayToken = str(cfg.gatewayToken)
  const gatewaySource = str(cfg.gatewaySource) || 'dsh'
  const appId = str(cfg.appId)
  const appSecret = str(cfg.appSecret)
  const targetType = str(cfg.targetType) === 'user' ? 'user' : 'group'
  const targetId = str(targetType === 'user' ? cfg.userId : cfg.groupId)
  const missing = []
  if (gatewayUrl !== '') {
    // 网关模式：必需项只有 gatewayToken；直连凭据可选（网关故障时兜底）
    if (gatewayToken === '') missing.push('gatewayToken（noti-gateway 的 auth.token）')
  } else {
    if (appId === '') missing.push('appId（QQ 开放平台 q.qq.com → 机器人开发设置 → 开发者 ID/密钥）')
    if (appSecret === '') missing.push('appSecret（同一页面 AppSecret，secret 不落日志）')
    if (targetId === '') missing.push(targetType === 'user' ? 'userId（单聊目标用户的 openid）' : 'groupId（群 open id，机器人入群后在事件里获取）')
  }
  if (missing.length > 0) {
    throw new NotifyError(`qq-bot 未配置：${missing.join('、')} 未填写`, ERROR_CODES.NOT_CONFIGURED)
  }
  return {
    gatewayUrl,
    gatewayToken,
    gatewaySource,
    appId,
    appSecret,
    targetType,
    targetId,
    apiBase: (str(cfg.apiBase) || DEFAULT_API_BASE).replace(/\/+$/, ''),
    timeoutMs: num(cfg.timeoutMs, 10000, 1000, 60000),
    rateMs: num(cfg.rateMs, 1050, 0, 60000),
    // #33：默认发 markdown（msg_type=2 + markdown.content），仅显式 `markdown: false` 回退纯文本。
    // 默认开启是所有者对「新能力默认关」fail-closed 惯例的明确例外（taskbook v0.11 已拍板）。
    markdown: cfg.markdown !== false,
  }
}

/**
 * 每个 resolved 配置对象对应的运行态（token 管理器 / 限速门 / msg_seq 计数器）。
 *
 * 为什么用 WeakMap 而不是挂在 resolved 上（本副本的核心修复）：
 * v0.12 起出站配置的唯一运行时真值是 OutboundSource 的深冻结快照
 * （runtime/outbound-source.mjs 的 freezeDeep），notify/notifyAll 直接把
 * `entry.config`（已冻结）传给 adapter.send()。原实现在 send 里写
 * `resolved._tokenManager ??= ...`，严格模式下必抛
 * `TypeError: Cannot add property _tokenManager, object is not extensible`，
 * 于是 QQ 主动消息在发出第一个 HTTP 请求之前就失败——而管理台「测试通道」走的是
 * health.mjs 里 adapter.resolve() 新建的可变对象，所以测试永远通过、真机推送永远失败。
 *
 * WeakMap 把可变运行态放到冻结对象之外的侧表：既保住「快照不可被消费方篡改」的
 * 防篡改契约，又让 token 缓存/限速/msg_seq 在多次 send 之间正确复用（这正是不能
 * 每次 send 现造一份配置的原因——每发一条都重新换 token 会在 60qpm 限速下迅速触顶）。
 * 以 resolved 对象本身为键，配置被替换后旧键自然被 GC 回收。
 */
const RUNSTATE = new WeakMap()

/** 换 token 端点：与 apiBase 同源（apiBase 指向自建网关/本地联调时，token 端点也要跟着走）。 */
function resolveTokenUrl(resolved) {
  const base = str(resolved?.apiBase)
  if (base === '' || base.replace(/\/+$/, '') === DEFAULT_API_BASE) return TOKEN_URL
  return `${base.replace(/\/+$/, '')}/app/getAppAccessToken`
}

/** 取（必要时创建）某个 resolved 配置的运行态。 */
function runStateOf(resolved) {
  let state = RUNSTATE.get(resolved)
  if (state === undefined) {
    state = { tokenManager: undefined, rateGate: undefined, msgSeq: 0 }
    RUNSTATE.set(resolved, state)
  }
  return state
}

/** 经统一网关（noti-gateway）投递纯推送：汇入统一配额池（落盘+重试+死信）。
 *  成功返回 true；失败打 stderr 日志并返回 false，由调用方回退直连。 */
async function sendViaGateway(resolved, msg) {
  const text = msg.title.length > 0 ? `${msg.title}\n${msg.content}` : msg.content
  const lines = text.split('\n')
  const title = (lines[0] ?? '').trim().slice(0, 100) || '(无标题)'
  const body = lines.slice(1).join('\n').trim()
  try {
    await postJson(`${resolved.gatewayUrl}/n/${encodeURIComponent(resolved.gatewaySource)}`, {
      title,
      body,
    }, {
      headers: { authorization: `Bearer ${resolved.gatewayToken}`, 'x-source': resolved.gatewaySource },
      timeoutMs: resolved.timeoutMs,
      channel: 'qq-bot',
    })
    return true // postJson 非 2xx/网络异常会抛 NotifyError，能走到这里即投递成功（202 入队/200 同步）
  } catch (error) {
    try {
      console.error(`[dsh-notifier-gate:qq-bot] 网关投递失败，回退直连: ${error instanceof Error ? error.message : String(error)}`)
    } catch { /* stderr 不可用不致命 */ }
    return false
  }
}

/** 发送主动消息（默认 markdown：msg_type=2 + markdown.content；markdown:false 时为 msg_type=0 纯文本）。
 *  网关配置时优先经网关（见 sendViaGateway）；以下为直连路径。
 *  超长按码点分段逐条发送（每段独立 msg_seq，服务端按 msg_seq+内容去重）；任一段失败即抛错。
 *  2xx 即成功；4xx 带 {code,message} 抛中文指引。 */
export async function send(resolved, msg) {
  if (resolved.gatewayUrl !== '' && resolved.gatewayToken !== '') {
    if (await sendViaGateway(resolved, msg)) return
  }
  if (resolved.appId === '' || resolved.appSecret === '' || resolved.targetId === '') {
    throw new NotifyError(
      'qq-bot 网关投递失败，且未配置直连兜底凭据（appId/appSecret/targetId 至少一项缺失）',
      ERROR_CODES.NETWORK_ERROR,
    )
  }
  const state = runStateOf(resolved)
  state.tokenManager ??= createTokenManager(async () => {
    const response = await postJson(resolveTokenUrl(resolved), { appId: resolved.appId, clientSecret: resolved.appSecret }, {
      timeoutMs: resolved.timeoutMs, channel: 'qq-bot',
    })
    let payload
    try {
      payload = await response.json()
    } catch {
      throw new NotifyError('qq-bot 换取 access_token 失败：返回非 JSON', ERROR_CODES.API_ERROR)
    }
    if (typeof payload?.access_token !== 'string' || payload.access_token === '') {
      throw new NotifyError(`qq-bot 换取 access_token 失败：检查 appId/appSecret 是否正确（q.qq.com 开发设置页）`, ERROR_CODES.API_ERROR)
    }
    // G-55：expires_in 秒→毫秒后归一（非有限/≤0 抛错，不再 || 7200 掩盖上游损坏）
    return { token: payload.access_token, expiresInMs: normalizeTtlMs(Number(payload.expires_in) * 1000, 'expires_in') }
  })
  state.rateGate ??= createRateGate(resolved.rateMs)

  const token = await state.tokenManager.get()
  const url = resolved.targetType === 'user'
    ? `${resolved.apiBase}/v2/users/${resolved.targetId}/messages`
    : `${resolved.apiBase}/v2/groups/${resolved.targetId}/messages`
  const text = msg.title.length > 0 ? `${msg.title}\n${msg.content}` : msg.content
  // 分段按当前模式的上限（markdown 3000 / 文本 2000），与 qq-gw 同源码点语义。
  const markdown = resolved.markdown !== false
  const chunks = splitByCodePoints(text, markdown ? QQ_MARKDOWN_MAX_CODEPOINTS : QQ_TEXT_MAX_CODEPOINTS)
  const pieces = chunks.length > 0 ? chunks : ['']

  // v0.6.5（审查 R4-3-P3-4）：msg_seq 是服务端去重键。原实现每次尝试自增——重试时
  // seq 变化 = 服务端视为新消息，「第一次超时但实际已投递」的消息会被重复投递。
  // 改为尝试期间 seq 冻结（计数器不动），全部段成功后才推进；下一条内容不同，
  // 即使撞 seq 也不会被服务端误去重（QQ 去重需 seq+内容双匹配）。分段同理：整条
  // 消息重试时每段沿用同一 (seq, 内容) 组合，已送达的前段被服务端幂等去重。
  const baseSeq = state.msgSeq
  let lastSeq = baseSeq
  for (let i = 0; i < pieces.length; i += 1) {
    await state.rateGate.gate()
    const seq = (baseSeq + i + 1) % 1000000
    const response = await postJson(url, markdown
      ? { markdown: { content: pieces[i] }, msg_type: 2, msg_seq: seq }
      : { content: pieces[i], msg_type: 0, msg_seq: seq }, {
      headers: { authorization: `QQBot ${token}` },
      timeoutMs: resolved.timeoutMs,
      channel: 'qq-bot',
    })
    // v2 接口成功返回 2xx JSON {id, timestamp}；错误码在 HTTP 4xx body {code, message}
    // G-56：2xx 但响应非 JSON（网关错误页/空体）不再乐观视为成功——按投递失败抛
    // BAD_UPSTREAM_RESPONSE 并 stderr 出声。采用「解析结果」而非 content-type 头判定：
    // 头可能被网关吞掉，但解析失败是确定的事实。结果未知宁可计 failed，让宿主
    // 重试/告警链路接管（fail-closed，msg_seq 冻结保证重试幂等）。
    let payload = null
    try { payload = await response.json() } catch { payload = null }
    if (payload === null || typeof payload !== 'object') {
      try {
        console.error('[dsh-notifier-qq/adapter:qq-bot] 返回 2xx 但响应非 JSON（预期 {id,timestamp}），按投递失败处理——可能是网关错误页或空响应体')
      } catch { /* stderr 不可用不致命 */ }
      throw new NotifyError('qq-bot 返回格式异常：2xx 但响应非 JSON（预期 {id,timestamp}，可能是网关错误页）', ERROR_CODES.BAD_UPSTREAM_RESPONSE)
    }
    if (typeof payload?.code === 'string' && payload.code !== '') {
      throw new NotifyError(`qq-bot 返回错误 ${payload.code}: ${payload.message ?? '未知错误'}（确认机器人已开启${resolved.targetType === 'user' ? '单聊主动消息' : '群主动消息'}权限）`, ERROR_CODES.API_ERROR)
    }
    lastSeq = seq
  }
  state.msgSeq = lastSeq // 全部段成功才推进：失败/超时重试沿用同一 seq，幂等语义生效
}
