// dsh-notifier host/native-questions.mjs
// v0.10 宿主原生提问桥（任务书 3.2 Issue #3/#5）：把宿主 `ctx.userQuestions` 的
// `ask_user_question` 桥接到现有 `aq:` 账本与 Control Core，使原生提问、Web、手机
// 共用同一个首达结算闭环。
//
// 接缝选择（按宿主公开能力探测，单一能力检查、无并行双路径）：
//  - 当前正式 seam（DSH 0.1.7-rc.1 实测形态）：`user-questions/request` waterfall
//    拦截器——`UserQuestionService.ask()` 内部即 `ctx.waterfall('user-questions/request',
//    request, noAnswerer)`（rc.1 的 UserQuestionService **没有** registerProvider()），
//    web GUI answerer 亦经该 waterfall 挂载。
//    拦截器 `{ prepend: true, global: true }` 注册：prepend 保证先于 GUI answerer
//    收到请求（双面同时活跃），global 绕过 scopeTarget 作用域过滤（同 #28 教训）。
//  - `registerProvider`：**仅 future/legacy feature probe**（若未来宿主提供才走），
//    不是当前 seam，也不作为能力宣称依据。
//  - `ask_user_question` 工具是另一层：官方 tool package 注入 tools + userQuestions
//    后注册，属 preset/TUI composition；「ctx.userQuestions 存在」不推出「当前 Agent
//    看得到官方 ask_user_question」。故本插件保留自有 `ask_user` fallback，且不读
//    ToolRuntime 私有 registry。
//
// 职责边界（任务书建议的窄接口）：
//  - capabilities()  支持状态与模式，不含 sessionId/正文/token/凭证
//  - attach()        接入宿主原生问题源（provider 或 waterfall）
//  - pending()       当前待决原生问题（脱敏，供管理台）
//  - settle()        经 Control Core 首达结算（委托现有 questionBridge.adminSettle）
//  - snapshot()      管理台诊断
//  - dispose()       完整撤销（无监听、无迟到回调落账）
//
// 已知限制（宿主侧，见 docs/memory/risks.md）：waterfall 被本拦截器抢答后，宿主
// `ask()` 不 abort signal，GUI 远端卡片不会自动收起（orphan card）——修复属 DSH
// core（ask() settle 时 abort 链接 signal），插件侧如实降级并文档化。
//
// 红线：只使用公开 seam。不读私有字段、不覆盖未公开 singleton、不依赖插件加载顺序、
// 不 monkey patch 宿主内部方法。seam 不可用或已被占用时安全降级到 `unsupported`，
// 保留插件自有 `ask_user` fallback，绝不伪造「已桥接」。

import { detectQuestionsMode, readUserQuestions } from './capability.mjs'

const isRecord = (value) => typeof value === 'object' && value !== null

/**
 * 把原生 AskUserQuestionOption 归一为 aq 桥的选项标签；description 拼进上下文避免丢信息。
 * @returns {{ labels: string[], detail: string }}
 */
function normalizeOptions(options) {
  const labels = []
  const descriptions = []
  for (const option of Array.isArray(options) ? options : []) {
    if (option === null || typeof option !== 'object') continue
    const label = String(option.label ?? '')
    if (label !== '') labels.push(label)
    const description = String(option.description ?? '')
    if (description !== '') descriptions.push(description)
  }
  return { labels, detail: descriptions.join('\n') }
}

/**
 * 创建宿主原生提问桥。
 * @param {object} deps
 * @param {object} deps.ctx - cordis 上下文（含可选 ctx.userQuestions seam）
 * @param {ReturnType<typeof import('../questions/router.mjs').createQuestionBridge>} deps.questionBridge
 *   - 已装配的远程提问桥，承载 aq: 账本、推送、首达采纳与 askQuestions 循环
 * @param {object} [deps.logger]
 */
export function createNativeQuestionBridge(deps = {}) {
  const { ctx, questionBridge } = deps
  const logger = deps.logger ?? null
  const warn = (message) => {
    try { logger?.warn?.('[dsh-notifier-gate/native-questions]', message) } catch { /* 日志失败绝不致命 */ }
  }

  let provider = null
  let disposeProvider = null
  let disposed = false
  let attachError = null // 注册失败码（如 DUPLICATE_PROVIDER），capabilities/snapshot 用
  let attachMode = null // 'provider' | 'waterfall' | null（capabilities.mode 用）

  /** deps.canDeliver：入站交互通道是否就绪（空表 = 无手机面，拦截器不截流）。 */
  const canDeliver = typeof deps.canDeliver === 'function' ? deps.canDeliver : () => true

  /**
   * 宿主经 ctx.userQuestions.ask() 调用的 provider.ask(request) 入口：
   * 把原生请求映射为 aq 桥 payload → await 首达结算 → 映射回 AskUserQuestionAnswer。
   * 任何一步异常只返回空答案（绝不让宿主提问被静默吞掉，也不编造答案）。
   * @param {object} [execOptions] - 透传给 askQuestions 的 execContext 附加项
   *   （waterfall 拦截器用它传 onOpen 精确引用回告；provider 路径不传）。
   */
  async function hostAsk(request, execOptions = null) {
    const questions = Array.isArray(request?.questions) ? request.questions : []
    if (questions.length === 0) return { answers: [] }
    const normalized = questions.map((question) => {
      const options = normalizeOptions(question?.options)
      return {
        id: String(question?.id ?? ''),
        question: String(question?.question ?? ''),
        options,
        multiSelect: question?.multiSelect === true,
        detail: [
          String(question?.detail ?? ''),
          options.detail,
        ].filter((part) => part !== '').join('\n'),
      }
    })
    const payload = {
      questions: normalized.map((entry) => ({
        // id 透传给 questionBridge：GUI 先答回灌（settleFromWeb）按 id 把答案批次
        // 对位到在途问题行（索引兜底），否则多问请求会错位。
        id: entry.id,
        question: entry.question,
        options: entry.options.labels.map((label) => ({ label })),
        multiSelect: entry.multiSelect,
      })),
      context: [String(request?.context ?? ''), ...normalized.map((entry) => entry.detail)]
        .filter((part) => part !== '').join('\n'),
    }
    // ask_user 经本 seam 出题时会带上 timeoutMs（原生 ask_user_question 请求没有该字段）：
    // 透传给 questionBridge，让 QQ 卡片作答时限与 askTimed 前台等待共用同一时限；
    // 缺失时由 questionBridge 默认策略兜底（宿主 signal 取消见 review 收口）。
    const requestTimeoutMs = Number(request?.timeoutMs)
    if (Number.isFinite(requestTimeoutMs) && requestTimeoutMs > 0) {
      payload.timeoutMs = requestTimeoutMs
    }
    let outcome
    try {
      outcome = await questionBridge.askQuestions(payload, { agent: request?.agent, ...(execOptions ?? {}) })
    } catch (error) {
      warn(`原生提问桥问询异常（返回未作答，绝不让宿主被吞）: ${error instanceof Error ? error.message : String(error)}`)
      return { answers: normalized.map((entry) => ({ id: entry.id, selected: [] })) }
    }
    const results = Array.isArray(outcome?.results) ? outcome.results : []
    const answers = normalized.map((entry, index) => {
      const result = results[index]
      if (result === undefined || result === null || result.answered !== true) {
        // 未作答/超时/跳过/终止/错误 → selected 空（保留「跳过项」语义，宿主自行处理）
        return { id: entry.id, selected: [] }
      }
      // 自定义文本作答（aq-text）→ custom；选项作答 → selected label 数组。
      if (typeof result.via === 'string' && result.via.endsWith(':text')) {
        const custom = Array.isArray(result.answers) ? String(result.answers[0] ?? '') : ''
        return { id: entry.id, selected: [], custom }
      }
      const selected = Array.isArray(result.answers) ? result.answers.map(String) : []
      return { id: entry.id, selected }
    })
    return { answers }
  }

  /** 支持状态与模式（无敏感数据）。 */
  function capabilities() {
    const seam = detectQuestionsMode(ctx)
    return {
      seam,
      mode: provider !== null && disposeProvider !== null
        ? (attachMode === 'waterfall' ? 'waterfall' : seam)
        : 'unsupported',
      attached: provider !== null && disposeProvider !== null,
      error: attachError,
    }
  }

  /**
   * `user-questions/request` waterfall 拦截器——**DSH rc.1 当前正式 seam**
   * （后续宿主若提供 registerProvider 才走 provider 路径，见模块头口径）。
   * 双面并发 + 首达结算：立即把问题推上 aq 桥（手机/管理台可见），与下游 GUI
   * answerer（next()）race，先答先算：
   *  - 手机/管理台先答 → 本侧答案上交宿主（下游成为输家，catch 兜底防未处理拒绝；
   *    GUI 卡片能否自动收起属宿主 ask() signal 语义，见模块头「已知限制」）。
   *  - GUI 先答 → 下游答案上交宿主；abort 拦截器范围取消信号（reason 携带 Web 答案
   *    批次）——askQuestions 侧清延迟推卡（Stage 0→1 pushTimer）、停升级，并把在途
   *    问题按「先答先算」跨端结算（settleFromWeb：answered via web / skipped）：已推
   *    QQ 卡片改「✅ 已作答：…（来源 web）」/「⏭ 已跳过」，绝不误标「⏹ 已终止」
   *    （2026-11-10 多端同步回归修复）；多问请求的后续问题不再开（router 循环头
   *    aborted 检查）。绝不迟推卡。
   *  - 下游先 reject（GUI answerer 异常 / headless 宿主无 answerer 的 NO_PROVIDER）
   *    → 不判死整场 race：下游转为永不 settle，等手机/管理台作答兜底。
   *  - 空请求 / 无入站交互通道（canDeliver 假）→ 原样透传 next()，GUI-only 宿主
   *    行为与未装本插件时完全一致。
   * next() 至多调用一次（nextCalled 哨兵）；拦截器任何异常透传下游或返回空答案，
   * 绝不让宿主提问被静默吞掉。
   */
  /** 造一份「全部未选」的答案批次（宿主据此按自身语义解释为未作答）。 */
  const EMPTY_ANSWERS = (request) => ({
    answers: (Array.isArray(request?.questions) ? request.questions : [])
      .map((question) => ({ id: String(question?.id ?? ''), selected: [] })),
  })

  function waterfallHandler(request, next) {
    let nextCalled = false
    const downstreamOnce = () => {
      nextCalled = true
      return Promise.resolve().then(() => next())
    }
    try {
      const questions = Array.isArray(request?.questions) ? request.questions : []
      if (questions.length === 0 || canDeliver() !== true) return downstreamOnce()
      // 拦截器范围取消信号：GUI 先答时 abort（收尾语义全部在 askQuestions 侧落地）。
      const controller = new AbortController()
      // 本侧原始结果（hostAsk 全捕获，保证必然 settle）：真答案或超时空答案。
      const mobileAnswerRaw = Promise.resolve().then(() => hostAsk(request, { signal: controller.signal }))
        .catch(() => EMPTY_ANSWERS(request))
      const isAnswered = (result) => Array.isArray(result?.answers)
        && result.answers.some((entry) => (Array.isArray(entry?.selected) && entry.selected.length > 0)
          || (typeof entry?.custom === 'string' && entry.custom !== ''))
      // 关键：本侧「没答出来」（超时/未作答/异常 → 全空 selected 且无 custom）时
      // **不得**抢走 race。若把它当赢家返回给宿主，宿主据此认为"用户没选"，
      // 而此刻 Web 卡片还在等人作答——真机事故（2026-11-02 计划审批）：
      // 用户后点的 Approve 落在孤儿卡上，答案无人消费，表现为「点了没生效，窗口消失」。
      // 这里把「空答案」转成永不 settle：GUI 活着时让下游继续跑完；
      // 只有下游确认死亡（downstreamDied$）才走 died 分支闭环宿主。
      const telegramAnswer = mobileAnswerRaw.then((result) => (isAnswered(result) ? result : new Promise(() => {})))
      const downstream = downstreamOnce()
      // 输家卫生（两向）：原始 downstream 的 rejection 在此消费，绝不未处理；
      // race 内的派生 promise 对 rejection 转「永不 settle」——下游失败（GUI 异常 /
      // headless NO_PROVIDER）不判死提问，手机/管理台仍可作答。
      // 下游死亡信号：GUI answerer 走到了 reject（headless 无 answerer 的 NO_PROVIDER、
      // GUI 异常、请求被取消）。此时 Web 面已确认死亡，手机侧的超时空答案才可以
      // 闭环宿主——否则宿主 ask() 会无限挂起。
      let downstreamDiedNotify = () => {}
      const downstreamDied$ = new Promise((resolve) => { downstreamDiedNotify = resolve })
      downstream.catch(() => { downstreamDiedNotify(); })
      let downstreamWon = false
      // 防泄漏硬顶（不再是「替用户决定」的兜底）：上面已保证 GUI 活着时绝不注入空答案，
      // 但宿主 crash-收尾/信号中断等极端路径仍可能让 race 双方都悬置。30 分钟硬顶只是
      // 内存安全阀；到达时若 GUI 仍活着，同样只是退出 race、把控制权交回宿主当前语境，
      // 不主动产生"用户没选"的答案——空答案由宿主自身语义处理。
      // 真机事故复盘（2026-11-02 计划审批）：旧的 5 分钟定时器与插件内部提问超时等长，
      // 到点抢答空答案 → 宿主认为"keep planning"，而 Web 卡片还活着 → 用户点击落在
      // 孤儿卡上，答案无人消费，表现为「点了没生效，窗口消失」。
      const LEASE_MS = Math.max(1000, Number(deps.unansweredFallbackMs) || 30 * 60_000)
      const lease = new Promise((resolve) => {
        const timer = setTimeout(() => resolve('lease-expired'), LEASE_MS)
        if (typeof timer?.unref === 'function') timer.unref()
      })
      return Promise.race([
        telegramAnswer,
        downstream.then(
          (value) => { downstreamWon = true; return value },
          () => new Promise(() => { /* 下游失败：悬置，等本侧答案 */ }),
        ),
        downstreamDied$.then(() => 'downstream-died'),
        lease,
      ]).then((winner) => {
        // GUI 拿到答案：正常闭环，并收尾本侧等待。
        if (downstreamWon) {
          // reason 随 abort 传给 askQuestions 的 onAbort：在途问题按 Web 答案批次
          // 跨端结算（answered via web / skipped，卡片改「✅ 已作答：…（来源 web）」），
          // 而不是终止账本行——2026-11-10 多端同步回归修复（此前一律 terminate，
          // QQ 卡片误标「⏹ 已终止：agent 会话已结束」）。
          try {
            controller.abort({ kind: 'downstream-win', answers: Array.isArray(winner?.answers) ? winner.answers : [] })
          } catch { /* 已终结则忽略 */ }
          return winner
        }
        // 下游死亡或租约到点：GUI 已经/将不会再答了，此时手机侧结果（可能已答，
        // 也可能是超时空答案）允许闭环宿主；两侧都没答就给空答案（宿主自行解释）。
        if (winner === 'downstream-died' || winner === 'lease-expired') {
          try { controller.abort() } catch { /* 已终结则忽略 */ }
          // mobileAnswerRaw 必然 settle（真答案或超时空答案），直接等待它闭环宿主。
          return mobileAnswerRaw
        }
        // 本侧先答（真答案）：收尾 GUI 卡片。
        const answered = Array.isArray(winner?.answers)
          && winner.answers.some((entry) => (Array.isArray(entry?.selected) && entry.selected.length > 0)
            || (typeof entry?.custom === 'string' && entry.custom !== ''))
        if (answered) {
          try { controller.abort() } catch { /* 已终结则忽略 */ }
        }
        return winner
      })
    } catch (error) {
      warn(`waterfall 拦截器异常: ${error instanceof Error ? error.message : String(error)}`)
      if (!nextCalled) return downstreamOnce()
      // next 已被调用后仍异常：返回空答案，绝不吞宿主提问。
      return Promise.resolve({ answers: [] })
    }
  }
  /**
   * 接入宿主原生问题源。失败（seam 缺失/被占用）时记录原因并安全降级。
   * @param {object} [subCtx] - 可选依赖子上下文（ctx.inject 回调的第一参数）：
   *   waterfall 监听器注册在 subCtx 上，随依赖服务的 fiber 生命周期自动撤销；
   *   服务被替换/重建后子插件重放时，attach 重挂到新 subCtx（旧监听已随旧
   *   fiber 释放，先撤本地句柄再重注册）。缺省回落插件根 ctx（旧宿主/直连路径）。
   * @returns {boolean} 是否成功 attach
   */
  function attach(subCtx = null) {
    if (disposed) return false
    // 重放安全：服务重建后 ctx.inject 回调再次进入时，旧监听已随旧 fiber 释放，
    // 本地句柄作废——先撤再重挂，绝不重复注册。
    if (disposeProvider !== null) {
      try { disposeProvider() } catch { /* 旧句柄已失效（fiber 已释放），忽略 */ }
      disposeProvider = null
      provider = null
    }
    // #27：防御读（ctx.get 非抛错优先，代理直读兜底）——探测绝不能炸装配。
    const userQuestions = readUserQuestions(ctx)
    if (!isRecord(userQuestions)) {
      attachError = 'no_userQuestions'
      return false
    }
    if (typeof userQuestions.registerProvider === 'function') {
      provider = { ask: hostAsk }
      try {
        disposeProvider = userQuestions.registerProvider(provider)
      } catch (error) {
        attachError = (error && (error.code ?? error.name)) || 'register_failed'
        provider = null
        disposeProvider = null
        warn(`原生提问 provider 注册失败（降级 unsupported）: ${attachError}`)
        return false
      }
      attachMode = 'provider'
      attachError = null // 重放成功：清掉上一次失败残留的错误码
      return true
    }
    // DSH rc.1 当前形态（dsh-user-questions）：只有 ask()，无 registerProvider。
    // 走 waterfall 当前 seam；无 ctx.on 的宿主/桩保持原降级码。
    const registerCtx = isRecord(subCtx) && typeof subCtx.on === 'function' ? subCtx : ctx
    if (typeof registerCtx?.on !== 'function') {
      attachError = 'no_register_provider'
      return false
    }
    provider = { ask: hostAsk }
    try {
      disposeProvider = registerCtx.on('user-questions/request', waterfallHandler, { prepend: true, global: true })
    } catch (error) {
      attachError = (error && (error.code ?? error.name)) || 'waterfall_register_failed'
      provider = null
      disposeProvider = null
      warn(`waterfall 提问拦截器注册失败（降级 unsupported）: ${attachError}`)
      return false
    }
    attachMode = 'waterfall'
    attachError = null // 重放成功：清掉上一次失败残留的错误码
    return true
  }

  /** 当前待决原生问题（脱敏快照，标记来源 native；委托 questionBridge）。 */
  function pending() {
    if (typeof questionBridge?.adminPending !== 'function') return []
    try {
      return questionBridge.adminPending().map((row) => ({ ...row, source: 'native' }))
    } catch {
      return []
    }
  }

  /** 经 Control Core 首达结算后回宿主（委托 questionBridge.adminSettle）。 */
  function settle(input = {}) {
    if (typeof questionBridge?.adminSettle !== 'function') {
      return { ok: false, handled: false, reason: 'no_settle', message: '原生桥未装配结算入口' }
    }
    try {
      return questionBridge.adminSettle(input)
    } catch (error) {
      return { ok: false, handled: false, reason: 'settle_failed', message: error instanceof Error ? error.message : String(error) }
    }
  }

  /** 管理台诊断快照（无敏感数据）。 */
  function snapshot() {
    return capabilities()
  }

  /** 完整撤销 provider（幂等；撤销失败不致命）。 */
  function dispose() {
    disposed = true
    try { disposeProvider?.() } catch { /* 反注册失败不致命 */ }
    disposeProvider = null
    provider = null
  }

  return { capabilities, attach, pending, settle, snapshot, dispose }
}