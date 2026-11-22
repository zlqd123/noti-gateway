// dsh-notifier index.mjs
// cordis 插件入口：组装配置解析、adapter 注册表、两条触发线（事件自动推送 + notify 工具）。
// 空配置绝不弄崩启动：任何渠道解析问题只 warn + 跳过（学 dsh-email）。

import { chmodSync, mkdirSync, unlinkSync, writeFileSync } from 'node:fs'
import { CHANNEL_TYPES, resolveConfig } from './config.mjs'
import { composeOutboundChannels } from './assembly/outbound.mjs'
import { resolveInboundSignals } from './assembly/inbound-signals.mjs'
import { createNotifier } from './notify.mjs'
import { createEventListener } from './event-listener.mjs'
import { registerNotifyTool, registerNotifyTestTool } from './tool-register.mjs'
import { createLedger, yesterdayWindow } from './ledger.mjs'
// 阶段 4/5：inbound 回传栈（远程审批 + 会话路由）
import { createStore, defaultStateDir } from './inbound/store.mjs'
import { createTokenVault } from './inbound/tokens.mjs'
import { createIdentity } from './inbound/identity.mjs'
import { createPairing } from './inbound/pairing.mjs'
import { createInboundBus } from './inbound/bus.mjs'
import { createInboundChannelRegistry } from './assembly/inbound-channels.mjs'
import { disposeAll } from './assembly/lifecycle.mjs'
import { registerApprovalHandler } from './approval/router.mjs'
import { createQuestionBridge, registerAskUserTool } from './questions/router.mjs'
import { createNativeQuestionBridge } from './host/native-questions.mjs'
import { registerRemoteCommands } from './inbound/conversation.mjs'
// v0.5：动作闭环（通知按钮 → 内置处置动作）
import { createActionDispatcher } from './actions.mjs'
import { createControlEntry } from './control/entry.mjs'
// v0.6：开放事件源（ctx.notifier 服务注入 + dsh-notifier/sent 事件）
import { createPublicFacade, composeOnSend, deepFreeze, redactAuditRecord } from './public.mjs'
// v0.3.2：路由引擎（双向解析链 + 会话台账，src/routing/*.mjs）
import { createAgentRouter } from './routing/agent-router.mjs'
import { createSessionRegistry } from './routing/session-registry.mjs'
// v0.10 移动任务选择（歧义前置）：多活跃任务无绑定先下发选择卡；待决状态经 store 持久化
import { createTaskSelection } from './routing/task-selection.mjs'
// 精简版：已移除自建 Web 管理台（admin/*）与 Native Control Surface（control-surface/*）。
// 项目/会话静音改由 DSH 原生设置页的 volatile muteRules 配置承载（见 config-schema.mjs）。
// 仅保留启动期出站配置迁移（已迁至 assembly/，与 admin 解耦）。
import { createOutboundSource } from './runtime/outbound-source.mjs'
import { createRuntimeChannelManager } from './runtime/channel-manager.mjs'
import { migrateCanonicalChannelConfig } from './assembly/channel-config-migration.mjs'
// lang 文案表：入站回执 / 晨报标题等手机可见文案取词（未知 lang 已在 resolveConfig 归一回落 zh）
import { stringsOf } from './strings.mjs'
import { createLog } from './log.mjs'

export const name = 'dsh-notifier-qq'
// 注意：**不**把 sessionTitle 写进 inject。cordis 的 inject 是硬依赖，宿主版本没有
// 该服务时整个插件会拒绝加载（通知全哑——v0.13 userQuestions 踩过同一个坑）。
// 会话标题改为运行时用 ctx.get('sessionTitle', false) 防御式探测（见 event-listener），
// 拿不到就少一行标题，其余能力照常。
export const inject = ['tools', 'agents', 'connection']

/** 返回已解析配置（供测试与其它插件复用）。 */
export function apply(ctx, config = {}) {
  // 配置唯一来源 = 本插件自己的 cordis 配置行（profile 的 cordis.patch.yml 里
  // `- id: dsh-notifier-gate` 的 config，或 bundle 自带 patch 的默认行）。
  // 不再读取、也不再借用原 dsh-notifier 插件的任何配置——本插件与原插件完全解耦，
  // 凭证改在本插件自己的配置行里维护（见 README「配置」一节）。
  config = config ?? {}
  const resolved = resolveConfig(config)
  const strings = stringsOf(resolved.lang)
  const logger = ctx?.logger
  // 统一日志出口（src/log.mjs）：故障(error/warn)照旧双写可控可见，常规生命周期(info)
  // 默认只进 logger 不进控制台——终端不再被启动装配/会话轮转刷屏。
  const log = createLog({ logger, tag: '[dsh-notifier-gate]' })
  const warn = (message) => log.warn(message)
  const info = (message) => log.info(message)
  const debug = (message) => log.debug(message)

  // v0.6 服务注入（spike 验证 2026-08-16，DSH 0.1.0-rc.6）：宿主为 cordis 强制契约，
  // 直接 ctx.notifier = facade 会被拦截（cannot set property "notifier" without provide），
  // 必须 ctx.provide('notifier', facade)（返回注销器）。无 provide（测试桩 / 非 cordis 宿主）
  // 回退直接赋值 + 引用比对清除（不误伤他人后注册的同名服务，审查 S4）。
  const registerNotifierService = (facade, disposers) => {
    if (typeof ctx?.provide === 'function') {
      try {
        const unprovide = ctx.provide('notifier', facade)
        if (typeof unprovide === 'function') disposers.push(unprovide)
      } catch (error) {
        warn(`notifier 服务注册失败: ${error instanceof Error ? error.message : String(error)}`)
      }
      return
    }
    try { ctx.notifier = facade } catch { warn('notifier 服务注册失败（宿主拦截属性赋值）') }
    disposers.push(() => {
      try { if (ctx.notifier === facade) ctx.notifier = undefined } catch { /* 清除失败不致命 */ }
    })
  }

  // v0.6 sent 事件发射（设计稿 §3）：emit 失败绝不影响账本/hub/推送主链路；宿主无 ctx.emit
  // 时 warn 一次后静默（可观测，审查 R6）。public.emit:false = 整链不挂（零开销家训）。
  const publicEmit = resolved.public?.emit !== false
  let emitWarned = false
  const emitSend = publicEmit
    ? (record) => {
        if (typeof ctx?.emit !== 'function') {
          // 可选链会静默吞掉缺失——违背 R6 可观测性：缺 emit 必须让用户看得见（warn 一次）
          if (!emitWarned) {
            emitWarned = true
            warn('宿主不支持 ctx.emit，dsh-notifier/sent 事件不可用（后续静默）')
          }
          return
        }
        try {
          ctx.emit('dsh-notifier/sent', deepFreeze(redactAuditRecord(record)))
        } catch {
          if (!emitWarned) {
            emitWarned = true
            warn('dsh-notifier/sent 发射失败（宿主可能不支持 ctx.emit），后续失败静默')
          }
        }
      }
    : null

  if (!resolved.enabled) {
    // v0.6（spike 裁定）：禁用时仍必须提供 no-op stub 服务——消费插件以 inject:['notifier']
    // 声明依赖，服务缺失会阻塞宿主启动（真机验证：pending → 启动 abort）。stub 让消费方
    // 拿到「push 返回 skipped:(disabled)」而非整个宿主起不来。
    info('已禁用（enabled: false），不注册事件监听与工具；notifier 服务以 no-op 形态照常提供')
    const stubDisposers = []
    registerNotifierService(createPublicFacade({
      notifier: null,
      config: resolved.public,
      logger,
      onDispose: (dispose) => stubDisposers.push(dispose),
    }), stubDisposers)
    ctx.effect(() => () => {
      for (const dispose of stubDisposers) {
        try { dispose()?.catch?.(() => {}) } catch { /* 卸载失败不致命 */ }
      }
    })
    return
  }

  // 加载期仅提示：每个被跳过的渠道一条 warn，绝不弄崩启动
  for (const entry of resolved.skipped) {
    info(`渠道 "${entry.type}" 跳过: ${entry.reason}`)
  }
  // 配置来源可观测：本插件只读自己的配置行，这里回报「配置行里写了什么」，
  // 让「装了但没配」与「配了但解析失败」在日志里可区分（绝不打印凭证值）。
  {
    const channelTypes = (Array.isArray(config.channels) ? config.channels : [])
      .map((row) => (row !== null && typeof row === 'object' && row.enabled !== false
        ? String(row.type ?? '').trim()
        : ''))
      .filter((type) => type !== '')
    if (channelTypes.length === 0) {
      warn('本插件配置行里没有启用任何渠道（channels 为空或全被禁用）：事件推送与 notify 工具将无操作；请在 profile 的 cordis.patch.yml 里为 `- id: dsh-notifier-gate` 写 channels')
    } else {
      info(`配置来源：本插件配置行（channels: ${channelTypes.join('、')}；不再读取 dsh-notifier 插件的配置）`)
    }
  }

  // 阶段 6：通知账本（可选晨报）。digest.enabled 开启后每次广播落账 JSONL，
  // 启动时对「昨日」窗口汇总推送一次摘要（同日重启不重发；账本失败绝不影响推送）。
  const digestRaw = (resolved.digest !== null && typeof resolved.digest === 'object') ? resolved.digest : {}
  const ledgerEnabled = digestRaw.enabled === true
  // Commit20：remoteLog.enabled 也要求账本存在——/log 的唯一数据源是 ledger.recent()。
  // 账本创建因此不与「晨报」耦合：remoteLog 单独开启时也建账本（否则 /log 恒不可用），
  // 但**晨报推送仍只由 digest.enabled 决定**（下方 if (ledgerEnabled && ...) 守卫），
  // 打开 /log 不会顺带开始发晨报。
  const remoteLogEnabled = resolved.remoteLog?.enabled === true
  let ledger = null
  if (ledgerEnabled || remoteLogEnabled) {
    const inboundRawForDir = (resolved.inbound !== null && typeof resolved.inbound === 'object') ? resolved.inbound : {}
    const ledgerDir = typeof inboundRawForDir.stateDir === 'string' && inboundRawForDir.stateDir.trim() !== ''
      ? inboundRawForDir.stateDir.trim()
      : defaultStateDir()
    ledger = createLedger({ dir: ledgerDir, maxEntries: digestRaw.maxEntries })
  }

  // 阶段 4/5：inbound 回传栈。白名单（inbound.allowUsers）为空 = 整栈不启动（默认全拒）。
  // inboundRaw / approvalRaw / store 已随 v0.3.3 出站凭证回退前移到 notifier 之前。
  const inboundRaw = resolved.inbound ?? {}
  const approvalRaw = resolved.approval ?? {}
  // v0.3.1：state store 提前创建（只读加载，无写副作用）——qq/feishu/dingtalk 的
  // 扫码凭证回退在 resolve 阶段就要读 store；必须先于下方各通道的 resolve 块
  // （TDZ：声明前引用会 ReferenceError，v0.3.1 首版曾把创建放在 resolve 之后，已修）。
  // v0.3.2：进一步前移到事件监听/工具注册之前——路由引擎（router/registry）也以它为持久层。
  // v0.3.3：再前移到 notifier 之前——出站凭证 state 回退（admin.enabled 时）要在
  // createNotifier 前合并完成（§5「YAML 只做 bootstrap，运行时可变状态写 state」）。
  const stateDir = typeof inboundRaw.stateDir === 'string' && inboundRaw.stateDir.trim() !== ''
    ? inboundRaw.stateDir.trim()
    : defaultStateDir()
  const store = createStore(`${stateDir}/state.json`)

  // 持久身份（配对绑定 / 会话路由 / 待决提问）只存在本插件自己的 state 目录，
  // 不读取、不借用原 dsh-notifier 插件的 state.json——两个插件完全独立。
  // 从旧 dsh-notifier 迁来的用户如需要保留 owner 绑定，可用管理台重新配对，
  // 或手工把 `inbound:bindings` 等键复制进本插件 state（见 README 迁移一节）。
  // v0.8.7 引导码文件交付（LEAK-2）：码面写本机 0600 文件，stderr 只印路径——
  // 日志聚合（journald/Loki/ELK）不再承载 owner 级凭证。
  const BOOTSTRAP_CODE_FILE = `${stateDir}/bootstrap-paircode.txt`

  // v0.3.3 出站凭证 state 回退（设计稿 §5）+ 连通性测试 rawConfig 来源：维护批 3 阶段 1
  // 抽到 src/assembly/outbound.mjs composeOutboundChannels/accountOf（纯移动，行为零变；
  // 详注见模块头）。admin 关闭时零执行——存量用户行为逐字节不变（§6 兼容红线）。
  const adminEnabled = resolved.admin?.enabled === true
  const configMigration = migrateCanonicalChannelConfig({
    store,
    channelTypes: CHANNEL_TYPES,
    adminEnabled,
    warn,
  })
  // Once v0.13 has taken the migration decision, runtime config is
  // canonical-only.  A failed/deferred migration remains fail-closed rather
  // than silently reviving an old overlay on the next restart.
  if (configMigration.ok !== true) {
    warn('出站通道配置迁移未完成，运行期不会回退读取 legacy state')
  }
  const yamlRowOf = new Map()
  for (const row of (Array.isArray(config.channels) ? config.channels : [])) {
    if (row === null || typeof row !== 'object' || row.enabled === false) continue // 显式禁用是用户意图，不回退
    const type = typeof row.type === 'string' ? row.type.trim() : ''
    if (type !== '' && !yamlRowOf.has(type)) yamlRowOf.set(type, row)
  }
  const overlay = composeOutboundChannels({
    channels: resolved.channels,
    yamlRows: yamlRowOf,
    store,
    adminEnabled,
    warn,
    allowLegacy: false,
  })
  const outboundSource = createRuntimeChannelManager({
    source: createOutboundSource(overlay.channels),
    initial: overlay.channels,
  })
  resolved.channels = outboundSource.snapshot()

  // 精简版：自建 Web 管理台与 Native Control Surface 已移除。
  // onSend 只保留「账本 + sent 事件」两挂（emitSend 内含 emit 开关判定），
  // 不再有 SSE hub / surface health/activity/revision 旁路。
  const onSend = composeOnSend([
    ledger === null ? null : (record) => ledger.append(record),
    emitSend,
  ])

  const notifier = createNotifier(ctx, outboundSource, { segment: resolved.segment, routing: resolved.routing, onSend })

  const disposers = []

  // v0.6 公共面装配（设计稿 §2.5）：notifier 之后创建 facade 并注册服务。public.enabled:false
  // → stub 形态（push 返回 skipped:(disabled)），服务照常提供（消费插件的启动依赖不能断）。
  // sink = 限流拦截的直落点：限流记录照进账本 + 照发 sent 事件（静音不等于没发生，§3.3）。
  const publicFacade = createPublicFacade({
    notifier: resolved.public?.enabled !== false ? notifier : null,
    config: resolved.public,
    logger,
    onSend,
    onDispose: (dispose) => disposers.push(dispose),
  })
  registerNotifierService(publicFacade, disposers)

  // v0.6.3 state 瘦身（审查 R2 P1-4）：dedup:*/ap:*/act:* 历史上只增不删（bus 每条
  // 入站消息落一个 dedup 键、审批/动作核销后账本行永留），长跑进程 state.json 单调
  // 膨胀且全量重写随之变慢。定期清扫：dedup 窗口 24h（留 1h 余量防时钟回拨），已决
  // 审批/动作保留 24h 供审计，超期即删（首启 + 每 6h；sweepPrefix 走脏键合并写，
  // 与 CLI/他进程并发写互不覆盖）。pending 行仅在超过活 waiter 合法寿命后清扫，
  // observe 审批与无法分类的旧行保留。
  // v0.6.4：dedup 清扫线联动 bus 窗口（窗口可配时硬编码 25h 会误清未过期键或漏清）;
  // bus 在白名单块才创建（可能不创建），sweep 注册在前——用外层惰性引用兜住。
  let sweepBusRef = null
  {
    const approvalTimeoutMs = Math.max(1000, Number(approvalRaw.timeoutMs) || 120000)
    const questionTimeoutMs = Math.max(1000, Number(resolved.questions?.timeoutMs) || 300000)
    const orphanHorizonMs = Math.max(2 * 60 * 60 * 1000, approvalTimeoutMs * 2, questionTimeoutMs * 2)
    const sweepOnce = () => {
      try {
        const windowMs = sweepBusRef?.dedupWindowMs ?? 24 * 60 * 60 * 1000
        const horizon = Date.now() - windowMs - 60 * 60 * 1000 // 窗口 + 1h 时钟回拨余量
        store.sweepPrefix('dedup:', (_key, seenAt) => typeof seenAt !== 'number' || seenAt < horizon)
        const resolvedHorizon = Date.now() - 24 * 60 * 60 * 1000
        const expiredRow = (_key, row) => row?.status === 'resolved'
          && typeof row.resolvedAt === 'number' && row.resolvedAt < resolvedHorizon
        const orphanPending = (_key, row) => row?.status === 'pending'
          && typeof row.createdAt === 'number' && row.createdAt < Date.now() - orphanHorizonMs
        const apExpired = (_key, row) => expiredRow(_key, row)
          || (orphanPending(_key, row) && row?.mode === 'answer')
        store.sweepPrefix('ap:', apExpired)
        store.sweepPrefix('act:', (_key, row) => expiredRow(_key, row) || orphanPending(_key, row))
        store.sweepPrefix('aq:', (_key, row) => expiredRow(_key, row) || orphanPending(_key, row))
      } catch { /* 清扫失败不致命，下轮再试 */ }
    }
    sweepOnce()
    const sweepTimer = setInterval(sweepOnce, 6 * 60 * 60 * 1000)
    sweepTimer.unref?.()
    disposers.push(() => clearInterval(sweepTimer))
  }

  // v0.3.2 路由引擎装配（设计稿 §7）：store 之后、inbound 白名单块之前创建，
  // 注入四条触发线（事件推送 / notify 工具 / 审批 / 会话路由）。
  // route 原值直取（config.route 为对象时；sessionTtlHours 由 registry 自行归一，缺省 24h）。
  // 未配置任何 route:* 的存量用户：解析链全程回落全局渠道池，行为零感知（§6 兼容红线）。
  const routeRaw = (config.route !== null && typeof config.route === 'object') ? config.route : {}
  const registry = createSessionRegistry({ ctx, store, ttlHours: routeRaw.sessionTtlHours, logger })
  const router = createAgentRouter({
    store,
    agentsList: () => { try { return ctx.agents.list() } catch { return [] } },
  })
  try {
    const migrated = registry.migrateLegacyBinds()
    if (migrated > 0) info(`route:sessions 迁移：为旧 bind 绑定补建 ${migrated} 条会话记录`)
  } catch { /* 迁移失败静默：绝不弄崩启动 */ }
  disposers.push(() => registry.dispose())
  // v0.10 任务选择状态机（歧义前置）：候选惰性过滤为「仍活跃会话」，待决经 store 持久化
  // （taskselect:* 键域，重启不丢）。dispose 只清内存态（盘上待决由 TTL 惰性回收）。
  const taskSelection = createTaskSelection({
    store,
    isActive: ({ sessionId }) => { try { return registry.isActive(sessionId) === true } catch { return false } },
    logger,
  })
  // v0.5 动作闭环的装配时序（架构审查修正，设计稿 §6）：eventListener 装配早于
  // inbound 白名单块（vault/store/通道在其后才创建），直传实例不可行——用惰性
  // getter（先例 = 下方 registerNotifyTool 的 channelTypes: () => ...）。
  // 未配置任何 inbound（白名单空）→ actions 永不创建 → getter 恒 null → 通知文本
  // hint「回复 /stop 取消」仍全通道可达，动作卡片自然缺席——兼容红线自洽。
  let actionsRef = null
  let interactiveRaw = []
  let busRef = null
  let questionsBridge = null
  let nativeBridge = null
  // v0.10 提交7：宿主事件 registrar 快照（管理台 /host 的 events.received 视图）。惰性读取，装配前为 null。
  let hostEventsRegistrar = null
  const questionsForChannels = {
    decide: (payload) => questionsBridge?.decide(payload) ?? { ok: false, message: strings.index.questionsNotReady },
  }
  disposers.push(createEventListener(ctx, notifier, resolved, {
    router,
    registry,
    // 设置页 volatile 静音规则（config.muteRules 是带 .get() 的 ref；普通对象亦可）。
    muteRules: config.muteRules ?? null,
    bus: () => busRef,
    actions: () => actionsRef,
    interactive: () => interactiveRaw,
    onHostEvents: (registrar) => { hostEventsRegistrar = registrar }, // 诊断快照外泄（提交7）
  }))
  const disposeTool = registerNotifyTool(ctx, notifier, {
    rateLimitPerMinute: resolved.toolRateLimitPerMinute,
    router,
    channelTypes: () => outboundSource.types(),
  })
  if (disposeTool != null) disposers.push(disposeTool)
  const disposeTestTool = registerNotifyTestTool(ctx, notifier, { rateLimitPerMinute: resolved.toolRateLimitPerMinute, strings })
  if (disposeTestTool != null) disposers.push(disposeTestTool)

  // 启动期晨报：昨日有记录且今天还没发过 → 推一次摘要（passive 级，走正常路由）。
  // Commit20：守卫必须看 **digest.enabled**（ledgerEnabled），而非「账本存在」——账本现在
  // 也可能仅因 remoteLog.enabled 而建（/log 数据源），此时绝不该顺带发晨报。
  if (ledgerEnabled && ledger !== null) {
    try {
      const window = yesterdayWindow()
      if (ledger.lastDigestDate() !== window.dateStr) {
        const summary = ledger.summarize(window.fromMs, window.toMs, { fromLabel: window.fromLabel, toLabel: window.toLabel })
        if (summary.counts.total > 0) {
          notifier.notifyAll({ title: strings.digest.title, content: ledger.compose(summary, strings), level: 'passive' })
            .catch(() => { /* 摘要推送失败不影响启动 */ })
          ledger.markDigestDone(window.dateStr)
        }
      }
    } catch { /* 晨报任何异常静默：账本绝不拖累启动 */ }
  }

  // 阶段 4：inbound 通道 resolve/启用信号（六个通道的显式配置/store 凭证即启用判定 +
  // tg 便捷回退 + wxpusher 密径持久化）。维护批 3 阶段 3 抽到
  // src/assembly/inbound-signals.mjs resolveInboundSignals（原样搬移，行为零变；
  // 详注随模块走）。inboundRaw / approvalRaw / store 已随 v0.3.2 路由装配前移到 notifier 之后创建。
  const {
    allowUsers, // 空 = 整栈不启动，默认全拒
    approvalWanted,
    qqResolved, qqOk,
  } = resolveInboundSignals({ inboundRaw, approvalRaw, resolved, store, adminEnabled, warn })

  // v0.7 身份层（计划书 §3.1/§3.2）：绑定表一等公民。YAML allowUsers 启动播撒为
  // 绑定记录（origin:'migrated'，幂等、只增不减——删减权收归管理台单一入口）；
  // 绑定表与 YAML 均空 = 引导态：六通道凭证就绪即启动，仅开放注册面（/pair 等）。
  // identity/pairing 提升到外层作用域：admin（成员页/配对码）与 inbound 共用同一实例。
  // 配对审计晚绑定：管理台在 inbound 之后装配，先入内存队（有界），装配后转发 admin-audit.jsonl；
  // admin 未启用则排队丢弃（审计文件属管理台，不存在静默丢审记的口径问题）。
  let pairingAuditSink = null
  const pairingAuditBacklog = []

  // v0.8.7 引导码文件写入辅助（方案A）：码面写本机 0600 文件，不流经 warn/stderr。
  const writeBootstrapCodeFile = (code) => {
    try {
      mkdirSync(stateDir, { recursive: true })
      // 写前先删：已存在的 symlink 会被 writeFileSync 跟随写穿到目标（本地提权面）
      try { unlinkSync(BOOTSTRAP_CODE_FILE) } catch { /* 不存在即已达目的 */ }
      writeFileSync(BOOTSTRAP_CODE_FILE, `${code}\n`, { encoding: 'utf8', mode: 0o600 })
      // mode 只在新建时生效，既有文件（如 umask 异常）补一刀
      try { chmodSync(BOOTSTRAP_CODE_FILE, 0o600) } catch { /* 权限收紧失败不致命，下方仍有码文件 */ }
      return true
    } catch (error) {
      // 失败只报路径与原因，绝不回退把码面印进日志（LEAK-2 的修复点就在这）
      warn(`引导码文件写入失败: ${error instanceof Error ? error.message : String(error)}（引导码无法文件交付，请使用管理台铸码）`)
      return false
    }
  }
  const clearBootstrapCodeFile = () => {
    try { unlinkSync(BOOTSTRAP_CODE_FILE) } catch { /* 不存在即已达目的（幂等） */ }
  }
  const identity = createIdentity({ store, logger })
  // Control Core：所有远程控制回调共用一个入口；personal 默认只允许已配对私聊，
  // converse/group control 必须由显式 policy 开启。
  // Session control overlays are read through the registry's defensive
  // copy-on-read API.  The resolver is intentionally fail-closed at the
  // integration boundary: a missing session, malformed store row, or registry
  // exception returns null, preserving the static policy and all existing
  // source-binding checks in Control Core.  The overlay itself can never add
  // channel/account/user/chat/session fields (session-arbiter owns that shape).
  const control = createControlEntry({
    policy: inboundRaw.control ?? {},
    identity,
    logger,
    policyForSession: (sessionId) => {
      try { return registry?.getControl?.(sessionId) ?? null } catch { return null }
    },
  })
  disposers.push(() => control.dispose())
  const pairing = createPairing({
    store,
    logger,
    onAudit: (event, detail) => {
      // v0.8.7 (A2)：bootstrap 码进终态即删码文件（核销/过期/撤销含 re-mint 替换旧码）。
      if (detail?.origin === 'bootstrap' && (event === 'redeem' || event === 'expire' || event === 'revoke')) {
        clearBootstrapCodeFile()
      }
      try {
        if (pairingAuditSink !== null) pairingAuditSink(`pairing:${event}`, detail)
        else if (pairingAuditBacklog.length < 200) pairingAuditBacklog.push([`pairing:${event}`, detail])
      } catch { /* 审计失败不致命 */ }
    },
  })
  // wechat 不进迁移通道表：其凭证 resolve 在 inbound 块内才做（首启播撒到死通道会产生
  // 永不生效的成员行；一次性迁移下首启正是唯一播撒机会——宁可少播，/pair 补齐）
  const enabledInboundChannels = [
    qqOk ? 'qq' : '',
  ].filter((name) => name !== '')
  try {
    const migrated = identity.migrate(allowUsers, enabledInboundChannels)
    // info 助手带 console 回落（R5 审查 R5-2-P3-6：裸 logger?.info?. 在无 info 通道的宿主零可见）
    if (migrated.added > 0) info(`白名单迁移：${migrated.added} 条绑定落盘（渠道：${enabledInboundChannels.join('/') || '无'}）`)
    else if (migrated.skipped === true) info('白名单迁移：已标记完成（YAML 不再播撒，增删以管理台为准）')
  } catch (error) {
    warn(`身份绑定迁移失败（继续以既有绑定表运行）: ${error instanceof Error ? error.message : String(error)}`)
  }
  const guidedBoot = identity.isEmpty() && allowUsers.length === 0
  // v0.8.7 (A2)：非引导态清理陈旧引导码文件（重启后引导态已结束，旧码面不该残留）。
  if (!guidedBoot) clearBootstrapCodeFile()

  // v0.7 启动门（修审查 #1）：通道凭证就绪即启动——白名单不再拦启动；空名单进入引导态
  // （业务面照旧全拒，仅注册面开放，红线不降级）。v0.6 兼容分支：无通道凭证但
  // approval 已配且名单非空时照旧注册（裁决无人应答超时回落桌面，行为与 0.6 一致）；
  // 名单与绑定表全空且无通道 → 不启动（引导提示）。
  const anyChannelReady = qqOk
  const inboundReady = anyChannelReady
    || (approvalWanted && (allowUsers.length > 0 || identity.size() > 0))
  if (inboundReady) {
    // v0.8.7 引导码文件交付（LEAK-2）：码面写本机 0600 文件，stderr 只印路径+ID——
    // 日志聚合不再承载 owner 级凭证。绑定表非空后不再铸造。
    const showBootstrap = (minted) => {
      if (minted?.ok !== true) {
        if (minted?.reason === 'storage-failed') {
          // 配对码未持久化时绝不展示码面；仍给出与文件交付失败一致的可操作指引。
          warn('引导码文件写入失败（配对码未持久化，请勿使用未落盘码面）')
          warn('【引导配对码】文件写入失败，请使用管理台铸码')
        }
        return
      }
      const minutes = Math.max(1, Math.round((minted.expiresAt - Date.now()) / 60000))
      if (writeBootstrapCodeFile(minted.code)) {
        warn(`【引导配对码】已写入 ${BOOTSTRAP_CODE_FILE}（${minutes} 分钟内有效，仅本机用户可读）\n  在任意已启用通道私聊机器人发送：/pair <配对码>\n  查看配对码：cat ${BOOTSTRAP_CODE_FILE}`)
      } else {
        warn(`【引导配对码】文件写入失败，请使用管理台铸码（${minutes} 分钟内有效）`)
      }
    }
    if (guidedBoot) {
      try {
        showBootstrap(pairing.mint({ origin: 'bootstrap', mintedBy: 'system:boot' }))
      } catch (error) {
        warn(`bootstrap 引导码铸造失败（注册面仍可用，管理台可补铸）: ${error instanceof Error ? error.message : String(error)}`)
      }
    }

    const vault = createTokenVault({
      secret: typeof inboundRaw.tokenSecret === 'string' && inboundRaw.tokenSecret !== ''
        ? inboundRaw.tokenSecret
        : undefined,
    })
    const bus = createInboundBus({
      allowUsers,
      identity,
      pairing,
      store,
      vault,
      logger,
      strings, // lang 文案表：身份命令回执（/pair /whoami /unpair）经 bus 传入 commandHandler
      // 引导码过期后首个 /pair 触发重铸（自愈：用户迟到不必重启宿主），stderr 再展示
      onBootstrapRemint: showBootstrap,
    })
    // v0.6.4（审查 R2-P2-5）：停机时总线整体收场——在途 waiter 以 null 结束（= 超时
    // 回退桌面语义）、消息处理器全摘；dedup 清扫线联动其窗口。
    sweepBusRef = bus
    busRef = bus
    disposers.push(() => bus.dispose())

    // v0.5 动作分发器：vault/store 之后创建（无环），telegram/feishu 按钮回调消费。
    // 内置白名单仅 turn/cancel——权限面与 /stop 命令完全等价（永无任意代码执行）。
    const actions = createActionDispatcher({ vault, store, logger, control }, strings)
    actions.register('turn/cancel', ({ payload }) => {
      const sessionId = typeof payload?.sessionId === 'string' ? payload.sessionId : ''
      if (sessionId === '') return { ok: false, message: strings.stop.invalidSession }
      let agent = null
      try { agent = ctx.agents.get(sessionId) } catch { return { ok: false, message: strings.stop.queryFailed } }
      if (agent === undefined || agent === null) {
        return { ok: false, message: strings.stop.notFound }
      }
      try {
        agent.cancel({ kind: 'user' }) // Host P0-B：structured AgentCancelCause（远程手机用户 = {kind:'user'}）
        return { ok: true, message: strings.stop.stopped }
      } catch {
        return { ok: false, message: strings.stop.cancelFailed }
      }
    })
    actionsRef = actions
    disposers.push(() => actions.dispose())

    // v0.3.0 多通道装配：registry 只负责 transport 实例、回执目标与停机；
    // Control Core、审批、提问和会话语义仍由各自模块持有。
    const channelRegistry = createInboundChannelRegistry({
      qqOk,
      qqResolved,
      bus,
      vault,
      store,
      identity,
      actions,
      questions: questionsForChannels,
      control,
      strings, // lang 文案表：透传给各渠道适配器（回执/卡片文案随 lang）
      allowUsers,
      guidedBoot,
      logger,
      warn,
    })
    const { interactiveInstances, replyTargets } = channelRegistry
    disposers.push(() => channelRegistry.dispose())

    // v0.6.1：路由注册同样逐个守护——审批/会话路由炸了只丢对应能力，
    // 不能拖垮整块 inbound 栈（interactiveRaw 赋值移进 try 之前保持语义）。
    let disposeApproval = () => {}
    try {
      disposeApproval = registerApprovalHandler({
        ctx,
        notifier,
        bus,
        vault,
        store,
        identity, // CRACK-003 编号回复归属校验：owner 才能代决非本人卡片
        control,
        interactive: interactiveInstances,
        approvalConfig: approvalRaw,
        router, // v0.3.2 审批分流：request.agent 可解析时只发绑定通道（quiet 对审批不生效）
        redaction: resolved.redaction, // S-05：审批推送 reason 按 minimal/extended 决定是否打码
        logger,
      }, strings)
      disposers.push(disposeApproval)
    } catch (error) {
      warn(`approval 路由装配失败，已跳过（inbound 通道不受影响）: ${error instanceof Error ? error.message : String(error)}`)
    }

    // v0.5：通道全部挂载后才暴露交互实例列表（eventListener 的 pushActionCard 每次
    // 经 normalizeInbound 防御归一，这里的赋值只发生在装配期一次）
    interactiveRaw = interactiveInstances

    // v0.8 远程提问桥（ask_user 工具 + aq: 账本 + 编号回复兜底）。桥体在审批路由
    // 之后创建并 attach——bus.onMessage 的插入序即消费优先级：'1'/'2' 在有待决
    // 审批时由审批先消费，提问编号（含 1,3 多选）随后接管；questions.enabled=false
    // 整体关闭（不注册工具、不挂编号处理器，行为与 v0.7 逐字节一致）。
    if (resolved.questions.enabled) {
      try {
        questionsBridge = createQuestionBridge({
          bus,
          vault,
          store,
          notifier,
          identity, // CRACK-004 hint 兜底编号回复归属闸：仅该渠道 owner 可代答，缺失 fail-closed
          control,
          interactive: () => interactiveRaw, // 惰性 getter：桥体每次裁决取最新实例表
          logger,
          config: resolved.questions,
        }, strings)
        const disposeAskTool = registerAskUserTool(ctx, questionsBridge, {
          rateLimitPerMinute: resolved.questions.rateLimitPerMinute,
          defaultTimeoutMs: resolved.questions.timeoutMs,
        })
        if (disposeAskTool !== null) disposers.push(disposeAskTool)
        questionsBridge.attach()
        disposers.push(() => questionsBridge.dispose())
        // v0.10 宿主原生提问桥（任务书 3.2）：经宿主公开 seam 桥接原生 ask_user_question；
        // 当前 DSH rc.1 的正式 seam 是 `user-questions/request` waterfall（registerProvider
        // 仅 future/legacy feature probe）；seam 缺失/被占用时安全降级（nativeBridge.capabilities()
        // 反映降级，管理台据此展示）。绝不伪造原生桥。
        // #27：(a) 服务读取改为防御式（ctx.get 非抛错，见 host/capability.mjs），探测不再
        // 炸装配；(b) attach 不再依赖静态 inject 声明（cordis 4.0.2 required-inject 会在
        // 宿主缺 userQuestions 服务时让整插件拒绝加载、通知全哑），改用 ctx.inject 运行时
        // 可选依赖——服务就绪才激活，宿主无该服务时桥静默 unsupported、其余能力照常。
        // canDeliver：入站交互通道空表时拦截器不截流（GUI-only 行为与未装插件一致）。
        nativeBridge = createNativeQuestionBridge({
          ctx,
          questionBridge: questionsBridge,
          logger,
          canDeliver: () => Array.isArray(interactiveRaw) && interactiveRaw.length > 0,
        })
        disposers.push(() => nativeBridge.dispose())
        const reportNativeBridge = () => {
          const caps = nativeBridge.capabilities()
          info(`宿主原生提问桥 ${caps.attached ? `已 attach（${caps.mode}）` : `未 attach（seam=${caps.seam}${caps.error !== null ? `, error=${caps.error}` : ''}，降级 unsupported）`}`)
        }
        if (typeof ctx?.inject === 'function') {
          try {
            // 子插件回调第一参数 = 可选依赖子上下文：waterfall 监听器注册在它上面，
            // 随 userQuestions 服务的 fiber 生命周期自动撤销/重放（服务替换后自动重挂）。
            ctx.inject(['userQuestions'], (subCtx) => { nativeBridge.attach(subCtx); reportNativeBridge() })
          } catch (error) {
            warn(`ctx.inject 可选依赖包装失败（回落直连 attach）: ${error instanceof Error ? error.message : String(error)}`)
            nativeBridge.attach()
            reportNativeBridge()
          }
        } else {
          nativeBridge.attach()
          reportNativeBridge()
        }
        info(`远程提问已启用：ask_user 工具（限流 ${resolved.questions.rateLimitPerMinute} 次/分钟，超时 ${Math.round(resolved.questions.timeoutMs / 1000)}s 不代答）；QQ 编号回复兜底`)
      } catch (error) {
        warn(`questions 桥装配失败，已跳过（其余能力不受影响）: ${error instanceof Error ? error.message : String(error)}`)
      }
    }

    // 阶段 5（2026-06 收窄）：远程命令路由——仅 /stop（取消当前 turn）与 /quiet //unquiet
    // （会话出站推送静默/恢复）。会话路由（文本 → agent 投递）已整体移除：专注通知 +
    // 卡片裁决，非命令消息一律静默。卡片编号裁决（审批/提权/提问/动作）由 approval/* 与
    // questions/router 各自的 bus 高优先级处理器完成，不经此模块。
    const replyViaChannel = async (channel, chatId, text) => {
      const target = replyTargets.get(channel)
      if (target !== undefined) {
        await target.sendText(chatId, text)
        return
      }
      const known = [...replyTargets.keys()].join('、')
      warn(`回执无可用通道：${channel}（已启用回执通道：${known !== '' ? known : '无'}）`)
    }
    try {
      const disposeRemoteCommands = registerRemoteCommands({
        ctx,
        bus,
        store,
        reply: replyViaChannel,
        router, // 入站解析链（bind > 通道默认 > 单 agent > 最近活跃）：/stop 定位目标会话
        registry, // 会话台账（活跃信号、/quiet 目标匹配数据源）
        control,
        logger,
      }, strings)
      disposers.push(disposeRemoteCommands)
    } catch (error) {
      warn(`远程命令路由装配失败，已跳过（通知与审批不受影响）: ${error instanceof Error ? error.message : String(error)}`)
    }
  } else if (approvalWanted) {
    // v0.7：无任何入站通道凭证时不启动（无回传通道可承载裁决）；有凭证即进入引导态。
    warn('approval 已配置但没有任何入站通道凭证：远程审批未启动。请先配置任一通道（如 inbound.telegram.botToken 或扫码落盘凭证），启动后经 /pair 配对即可使用')
  }


  ctx.effect(() => () => {
    // 聚合可 await 的清理（事件监听的 flush、通道和管理台 stop 都在此收敛）。
    return disposeAll(disposers)
  })

  if (outboundSource.types().length === 0) {
    info(`未配置任何可用渠道（已跳过 ${resolved.skipped.length} 个配置项），事件推送与 notify 工具将无操作；请在 profile 的 cordis.patch.yml 里为本插件配置 channels`)
  } else {
    // 只报道网关形态，避免「已启用渠道 qq-bot」让人误以为还在直连 QQ 官方 API
    info(`出站就绪：${outboundSource.types().join('、')}（一律经 noti-gateway 投递）`)
  }

  return resolved
}

export { resolveConfig, createNotifier, createEventListener, registerNotifyTool }
export { maskChannelConfig, CHANNEL_TYPES } from './config.mjs'
export { NotifyError, ERROR_CODES } from './adapters/_shared.mjs'
// 阶段 4/5：inbound 回传栈（供测试与其它插件复用）
export { createStore, defaultStateDir } from './inbound/store.mjs'
export { createTokenVault } from './inbound/tokens.mjs'
export { createInboundBus } from './inbound/bus.mjs'
export { createQqInbound, resolveQqInboundConfig } from './inbound/qq-gw.mjs'
export { registerApprovalHandler } from './approval/router.mjs'
export { createEscalationChain } from './approval/escalation.mjs'
export { createQuestionBridge, registerAskUserTool } from './questions/router.mjs'
export { createNativeQuestionBridge } from './host/native-questions.mjs'
export { registerRemoteCommands } from './inbound/conversation.mjs'
export { segmentText, countCodepoints, sendSegmented } from './inbound/segment.mjs'
// v0.6：开放事件源（供测试与其它插件复用）
export { PUBLIC_API_VERSION, createPublicFacade, composeOnSend, deepFreeze } from './public.mjs'
// 阶段 6：账本 / 健康自检 / 限流（供测试与其它插件复用）
export { createLedger, yesterdayWindow, classifyTitle, composeDigest } from './ledger.mjs'
export { runChannelTest, TEST_MESSAGE } from './health.mjs'
export { createRateLimiter } from './tool-register.mjs'
// v0.3.2：路由引擎（双向解析链 + 会话台账；供测试、CLI 与其它插件复用）
export { createAgentRouter } from './routing/agent-router.mjs'
export { createSessionRegistry, workspaceOf } from './routing/session-registry.mjs'
