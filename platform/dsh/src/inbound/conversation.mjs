// dsh-notifier inbound/conversation.mjs
// 远程命令路由器（2026-06 收窄：会话路由已整体移除，插件专注通知 + 卡片裁决）。
// 保留内容：
//  - /stop（无参）——通知文本「回复 /stop 取消」的全通道兜底入口，取消目标会话当前 turn；
//  - /quiet /unquiet <workspace|sid>——静默/恢复某会话的出站推送（v0.5 特性 C）；
//  - 未知命令回执（防回执黑洞）；普通消息与附件一律静默——不需要对话，也不回执。
// 已移除：文本/图片/附件 → agent 会话投递（followup/inject/steer）、合并窗、任务歧义
// 选择卡、/help /status /bind /unbind /agent /route /tasks /sessions /log /use 命令族。
// 卡片裁决（审批/提权/提问/动作的编号回复与按钮）由 approval/*、questions/router.mjs
// 各自的 bus 高优先级处理器完成，不经过本模块（本模块仍在消费链末位兜底）。
// 入站去向解析（resolveTarget 四层链）保留供 /stop 定位目标会话：
//   显式 bind > 通道默认 agent > 唯一 agent 兜底 > 最近活跃。router 缺省时回落 bind > latest。
// 军规：任何命令异常只回执用户，绝不弄崩宿主。

import { stringsOf } from '../strings.mjs'
import { createLog } from '../log.mjs'
import { workspaceOf } from '../routing/session-registry.mjs'
import { chatScopeOf } from '../control/session-arbiter.mjs'
import { bindingKey as identityBindingKey } from './identity.mjs'
import { MESSAGE_PRIORITY } from './bus.mjs'

/** zh 兜底（strings 未注入时的回落）：单一事实源 = stringsOf().conversation（无内联副本）。 */
const ZH_FALLBACK = stringsOf().conversation

/**
 * 注册远程命令路由器。
 * @param {object} deps
 * @param {object} deps.ctx - cordis 上下文（ctx.agents / ctx.on）
 * @param {ReturnType<typeof import('./bus.mjs').createInboundBus>} deps.bus
 * @param {import('./store.mjs').store} deps.store - 绑定关系持久化（bind:<channel>:<userId> → sessionId，只读）
 * @param {(channel: string, chatId: string, text: string) => void} [deps.reply] - 回执通道（命令反馈）
 * @param {object} [deps.logger]
 * @param {ReturnType<typeof import('../routing/agent-router.mjs').createAgentRouter>} [deps.router]
 *   - 入站解析链（bind > 通道默认 > 单 agent > 最近活跃）：/stop 定位目标会话；缺省回落旧行为
 * @param {ReturnType<typeof import('../routing/session-registry.mjs').createSessionRegistry>} [deps.registry]
 *   - 会话台账（活跃信号、/quiet 目标匹配数据源）；缺省时命令族降级提示
 * @param {ReturnType<typeof import('../control/entry.mjs').createControlEntry>} [deps.control]
 *   - /stop 经 Control Core 会话策略裁决（stop 闸 / 群聊禁用 / 配对）；缺省直接执行
 * @param {object} [strings] - stringsOf(lang) 全文案表（本函数读 conversation 节；
 *   缺省回落 ZH_FALLBACK）。装配层注入 stringsOf(config.lang) 全表。
 * @returns {() => void} 反注册函数
 */
export function registerRemoteCommands(deps, strings) {
  const { ctx, bus, store } = deps
  const t = strings?.conversation ?? ZH_FALLBACK
  const reply = typeof deps.reply === 'function' ? deps.reply : () => {}
  const router = deps.router ?? null
  const registry = deps.registry ?? null
  const control = deps.control ?? null
  const log = createLog({ logger: deps.logger, tag: '[dsh-notifier-gate/commands]' })
  const warn = (message) => log.warn(message)
  const info = (message) => log.info(message)
  // Host P0-A：异步链 fire-and-forget 统一兜底（总线 handler 同步、无法 await）。
  // 任何未捕获 rejection 只记 warn，绝不弄崩宿主/总线。
  const fireAsync = (promise) => {
    if (promise !== null && typeof promise?.catch === 'function') {
      promise.catch((error) => warn(`远程命令异步异常: ${error instanceof Error ? error.message : String(error)}`))
    }
  }

  // 最近活跃的根 agent：未显式绑定时 /stop 的默认目标
  let latestSessionId = null
  const disposers = []

  const agentsOf = () => {
    try { return typeof ctx?.agents?.list === 'function' ? ctx.agents.list() : [] } catch { return [] }
  }
  const agentOf = (sessionId) => {
    try { return typeof ctx?.agents?.get === 'function' ? ctx.agents.get(sessionId) : undefined } catch { return undefined }
  }
  // G-49：会话绑定持久化键。分量归一收敛到 identity.bindingKey（trim + channel 小写），
  // 与 agent-router resolveInbound L1 的读键同源——' user ' 与 'user' 写读同键永不裂。
  const bindingKey = (envelope) => `bind:${identityBindingKey(envelope.channel, envelope.userId)}`

  // ---- 命令族支撑（军规：registry/router 任何缺失或抛错一律降级，绝不弄崩命令主线）----

  /** registry 方法防御壳：缺实例 / 缺方法 / 抛错 → undefined（调用方各自兜底）。 */
  const registryCall = (method, ...args) => {
    try {
      if (registry === null || typeof registry[method] !== 'function') return undefined
      return registry[method](...args)
    } catch (error) {
      warn(`registry.${method} 调用失败（已降级）: ${error instanceof Error ? error.message : String(error)}`)
      return undefined
    }
  }
  /** router 方法防御壳：同上。 */
  const routerCall = (method, ...args) => {
    try {
      if (router === null || typeof router[method] !== 'function') return undefined
      return router[method](...args)
    } catch (error) {
      warn(`router.${method} 调用失败（已降级）: ${error instanceof Error ? error.message : String(error)}`)
      return undefined
    }
  }

  /** 会话的 workspace 名：registry 台账快照优先，缺档回落宿主 agent 的 cwd 末段。 */
  const workspaceOfSid = (sessionId) => {
    const record = registryCall('getSession', sessionId)
    if (typeof record?.workspace === 'string' && record.workspace !== '') return record.workspace
    const agent = agentOf(sessionId)
    return agent !== undefined ? workspaceOf(agent) : ''
  }

  /**
   * 活跃会话快照（/quiet 目标匹配的数据源）。registry 注入时用台账
   * （activeSessions 已按 lastActiveAt 降序）；缺省降级宿主 ctx.agents.list()
   * + workspaceOf(agent)（无活跃信号，排序退化为宿主列表顺序）。
   * @returns {{ infos: Array<{id: string, workspace: string}>, activitySorted: boolean }}
   */
  const activeSessionInfos = () => {
    if (registry !== null) {
      const ids = registryCall('activeSessions')
      if (Array.isArray(ids)) {
        return {
          infos: ids
            .filter((id) => typeof id === 'string' && id !== '')
            .map((id) => ({ id, workspace: workspaceOfSid(id) })),
          activitySorted: true,
        }
      }
    }
    return { infos: agentsOf().map((agent) => ({ id: agent.id, workspace: workspaceOf(agent) })), activitySorted: false }
  }

  /** 候选中取最近活跃者（§0.5-4「投最近活跃」）：台账有序直取首位，无信号时取末位启发式。 */
  const pickLatest = (infos, activitySorted) => {
    if (infos.length === 0) return null
    if (activitySorted) return infos[0].id
    const best = registryCall('latestActiveOf', infos.map((info) => info.id))
    if (typeof best === 'string' && best !== '') return best
    return infos[infos.length - 1].id // 无活跃信号：取列表末位（最近创建）启发式
  }

  /**
   * 入站去向解析（v0.3.2 §3 四层链，现仅供 /stop 定位目标会话）。router 注入时走完整链
   * （L1 bind 读同一 store 键，行为与旧 boundSession 等价）；未注入时回落 v0.3.1 旧行为。
   * 解析异常回落旧链（绝不弄崩命令主线）。
   * @returns {{ sessionId: string|null, source: string, ambiguous: boolean, candidates?: string[] }}
   */
  const resolveTarget = (envelope) => {
    if (router !== null) {
      try {
        return router.resolveInbound(envelope.channel, String(envelope.userId ?? ''), { latestSessionId })
      } catch (error) {
        warn(`入站路由解析失败，回落默认链: ${error instanceof Error ? error.message : String(error)}`)
      }
    }
    const bound = store.get(bindingKey(envelope))
    if (typeof bound === 'string' && bound !== '') return { sessionId: bound, source: 'bind', ambiguous: false }
    return { sessionId: latestSessionId, source: 'latest', ambiguous: false }
  }
  // 旧名兼容（/stop 沿用）
  const boundSession = (envelope) => resolveTarget(envelope).sessionId

  /**
   * 目标智能匹配（§0.5-5 解析顺序，/quiet|/unquiet 共用）：
   * workspace 名精确匹配（该 workspace 活跃会话取最近活跃者）> sessionId 精确 >
   * sid 前缀（≥4 位：唯一命中 / 多命中列候选 / 零命中提示）。
   * @param {string} needle - 用户输入的目标串。
   * @returns {{ sid: string, matchedBy: string } | { sid: null, message: string }}
   */
  const matchSessionByNeedle = (needle) => {
    const { infos, activitySorted } = activeSessionInfos()
    const ofWorkspace = infos.filter((info) => info.workspace !== '' && info.workspace === needle)
    if (ofWorkspace.length > 0) {
      const sid = pickLatest(ofWorkspace, activitySorted) // 同 workspace 多活跃会话 → 最近活跃者（§0.5-4）
      if (sid !== null) return { sid, matchedBy: t.matchedByWorkspace(needle) }
    }
    if (infos.some((info) => info.id === needle)) return { sid: needle, matchedBy: t.matchedBySessionId }
    if (needle.length >= 4) {
      const hits = infos.filter((info) => info.id.startsWith(needle)).map((info) => info.id)
      if (hits.length === 1) return { sid: hits[0], matchedBy: t.matchedBySidPrefix }
      if (hits.length > 1) {
        return {
          sid: null,
          message: [
            t.prefixAmbiguous(needle, hits.length),
            ...hits.map((id) => `  ${id}`),
            t.prefixHint,
          ].join('\n'),
        }
      }
    }
    return { sid: null, message: t.noMatch(needle) }
  }

  /**
   * 远程命令：仅保留通知生命周期所需的 /stop、/quiet /unquiet 与未知命令回执。
   * （/help /status /bind /unbind /agent /route /tasks /sessions /log /use 已随会话路由移除。）
   * @returns {boolean} 是否已处理（仅供调用方参考；未知命令同样已回执）
   */
  function handleCommand(envelope, text) {
    const [rawCmd, ...args] = text.slice(1).trim().split(/\s+/)
    const cmd = rawCmd.toLowerCase()
    const say = (message) => reply(envelope.channel, envelope.chatId, message)
    if (cmd === 'stop' && args.length === 0) {
      // G-04：/stop 是无参命令——只有裸 '/stop' 命中取消。带附言的 '/stop 一下别急'
      // 不再命中（防误杀长任务），落到函数尾部的未知命令路径：回执「未识别的命令」。
      const bound = boundSession(envelope)
      const agent = bound !== null ? agentOf(bound) : undefined
      if (agent === undefined) { say(t.stopNone); return true }
      try {
        agent.cancel({ kind: 'user' }) // Host P0-B：structured AgentCancelCause（远程手机用户 = {kind:'user'}）
        say(t.stopRequested(bound))
      } catch (error) {
        warn(`/stop 失败: ${error instanceof Error ? error.message : String(error)}`)
        say(t.stopFailed)
      }
      return true
    }
    // ---- v0.5 特性 C：/quiet /unquiet（设计稿 §4，目标解析复用智能匹配）----
    if (cmd === 'quiet' || cmd === 'unquiet') {
      const quiet = cmd === 'quiet'
      if (router === null) {
        say(t.quietUnavailable)
        return true
      }
      const target = args[0]
      if (typeof target !== 'string' || target.trim() === '') {
        say(t.quietUsage(cmd))
        return true
      }
      const matched = matchSessionByNeedle(target.trim())
      if (matched.sid === null) { say(matched.message); return true }
      const ok = routerCall('setSessionOutbound', matched.sid, { quiet })
      if (ok !== true) {
        say(t.quietWriteFailed(cmd))
        return true
      }
      const workspace = workspaceOfSid(matched.sid)
      const label = workspace === '' ? t.unknownWorkspace : workspace
      say([
        t.quietReceipt(quiet ? t.quietMarkMuted : t.quietMarkResumed, label, matched.sid, matched.matchedBy),
        quiet ? t.quietHint : '',
      ].filter((line) => line !== '').join('\n'))
      return true
    }
    // G-04：未知命令回执（防回执黑洞）。会话路由已移除：不再按普通文本投递，回执后即静默。
    say(t.unknownCommand(cmd))
    return true
  }

  // Control Core gate for remote commands. Notifications never pass here.
  // 仅裸 '/stop' 经会话策略裁决（session policy：stop 闸 / 群聊禁用 / 配对 fail-closed）；
  // /quiet、/unquiet 与未知命令直接处理；普通消息不经此路（见 bus 入口：非命令消息静默）。
  function route(envelope, text) {
    const trimmed = String(text ?? '').trim()
    // G-04：仅裸 '/stop' 归类为 stop 控制命令。'/stop 等等' 不命中取消（防误杀长任务），
    // 走未知命令回执。
    if (trimmed !== '/stop') {
      handleCommand(envelope, trimmed)
      return
    }
    if (control === null) { handleCommand(envelope, trimmed); return }
    // QQ group/ambiguous envelopes must not fall through to the control path
    // when no session is resolved; consume with a receipt instead.
    if (String(envelope.channel ?? '').toLowerCase() === 'qq' && chatScopeOf(envelope) !== 'private') {
      reply(envelope.channel, envelope.chatId, chatScopeOf(envelope) === 'group'
        ? t.groupControlDenied
        : t.controlSourceUnverified)
      return
    }
    const target = resolveTarget(envelope)
    if (target.sessionId === null) { handleCommand(envelope, trimmed); return }
    const receipt = control.handle({
      eventId: String(envelope.messageId ?? ''),
      command: 'stop',
      channel: envelope.channel,
      // v0.8.7：绝不把 channel 当 accountId 兜底（硬性规则）。accountId 缺失时这里得到空串，
      // normalizeControlEvent 会以 missing_accountId fail-closed 拒绝——来源必须真实存在。
      accountId: String(envelope.accountId ?? ''),
      userId: String(envelope.userId ?? ''),
      chatId: String(envelope.chatId ?? ''),
      chatType: envelope.chatType,
      sessionId: String(target.sessionId),
      policyVersion: '1',
      pending: { status: 'pending', sessionId: String(target.sessionId), createdAt: Date.now() - 1, expiresAt: Date.now() + 10 * 60 * 1000 },
      settle: () => { handleCommand(envelope, trimmed); return true },
    })
    if (receipt.status === 'accepted') return
    if (receipt.reason === 'group_chat_disabled') reply(envelope.channel, envelope.chatId, t.groupControlDenied)
    else if (receipt.reason === 'not_paired') reply(envelope.channel, envelope.chatId, t.notPaired)
    else reply(envelope.channel, envelope.chatId, t.controlRejected)
  }

  // G-31：本模块是消费链末位兜底（priority 100）——审批/提问的卡片编号回复在前层已被消费。
  // 专注通知：仅处理「/」开头的远程命令；普通消息与附件一律静默（不需要对话，也不回执）。
  const disposeMessage = bus.onMessage((envelope) => {
    const rawText = String(envelope.text ?? '').trim()
    if (rawText === '' || !rawText.startsWith('/')) return
    fireAsync(Promise.resolve().then(() => route(envelope, rawText)))
  }, { priority: MESSAGE_PRIORITY.conversation })

  // 追踪最近活跃 agent（/stop 的默认目标）；agent 退出时清理目标引用。
  // v0.7.3（#4）：DSH 的 agent/created | agent/disposed 事件签名是 (payload: { agent })，
  // 监听器收到的是载荷对象而非 agent 本身——旧代码 agent?.id 恒 undefined，
  // latestSessionId 永不赋值，未绑定用户 /stop 全部走到「没有可取消的会话」。
  // 此处解包 payload.agent（兼容直接传 agent 的旧宿主）。
  const payloadAgent = (arg) => {
    const agent = arg?.agent ?? arg
    return (agent !== null && typeof agent === 'object' && agent.id !== undefined) ? agent : null
  }
  // 只追踪根 agent：后台 subagent 同样触发 agent/created，若不滤掉会把默认目标
  // 劫持到 subagent 会话。宿主暴露 ctx.agents.roots() 时用它判定；老宿主无此 API
  // 则退化为全量追踪（与修复前行为一致，仅解包修复生效）。
  const rootIds = () => {
    try {
      const roots = ctx?.agents?.roots?.()
      return (roots !== null && typeof roots === 'object') ? roots : null
    } catch { return null }
  }
  const trackAgent = (payload) => {
    const agent = payloadAgent(payload)
    if (agent === null) return
    const roots = rootIds()
    if (roots !== null) {
      const ids = (Array.isArray(roots) ? roots : Object.values(roots)).map((a) => a?.id)
      if (!ids.includes(agent.id)) return // subagent：不劫持默认目标
    }
    latestSessionId = agent.id
  }
  try {
    disposers.push(ctx.on('agent/created', trackAgent))
  } catch { /* 宿主无此事件：默认目标不可用，仍可经绑定解析 */ }
  try {
    disposers.push(ctx.on('agent/disposed', (payload) => {
      const agent = payloadAgent(payload)
      if (agent !== null && agent.id === latestSessionId) latestSessionId = null
      // store 里的绑定在 agent 重启（同 id resume）后仍然有效，不清绑定。
    }))
  } catch { /* 同上 */ }

  return () => {
    disposeMessage?.()
    for (const dispose of disposers) {
      try { dispose?.() } catch { /* 反注册失败不致命 */ }
    }
  }
}
