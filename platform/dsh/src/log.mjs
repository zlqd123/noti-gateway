// dsh-notifier-gate src/log.mjs
// 统一日志出口（控制台降噪的核心）。
//
// 为什么需要它：本插件曾在 9 个模块里各自实现「logger + stderr 双写」的 warn()。
// 那次双写是为了救一次真机事故（2026-08-16：宿主 logger 不落 stdout，inbound 全死
// 却零可见）。代价是**每条 warn 都进控制台**——而大量 warn 其实是常规生命周期
// （启动装配、会话轮转、事件订阅注册），稳态下把终端刷成一片红，真正的故障反而被埋掉。
//
// 现在的口径（三级）：
//   error  真正的故障，必须让用户看见      → logger.error + stderr
//   warn   异常但已自愈/可忽略的降级         → logger.warn + stderr（默认开）
//   info   常规生命周期（启动/就绪/订阅）    → 仅 logger.info（**不进控制台**）
//   debug  高频诊断（心跳、轮转、@采样）     → 仅 logger.debug
//
// 环境变量 `DSH_NOTIFIER_VERBOSE=1` 把 warn 之外的一切也打到控制台——排障时用，
// 平时不需要。"宁可测试多几行，不可部署黑盒"的家训保留在「故障一定可见」上，
// 而不是「所有消息都可见」。

/** 控制台是否打开（stderr 双写）。默认开；verbose 时同样开（此时本来就要看细节）。 */
const CONSOLE_ENABLED = process.env.DSH_NOTIFIER_VERBOSE !== '0'

/** 是否把常规生命周期也打到控制台（默认关）。 */
const VERBOSE = process.env.DSH_NOTIFIER_VERBOSE === '1'

/**
 * 创建一个带前缀的日志器。
 * @param {{ logger?: object|null, tag?: string, prefix?: string }} deps
 *   - logger：宿主 cordis logger（可能缺方法，全部可选链）
 *   - tag：logger 的命名空间标签，如 '[dsh-notifier-gate/inbound:qq]'
 *   - prefix：控制台输出前缀；缺省与 tag 相同
 * @returns {{ error: (m: string) => void, warn: (m: string) => void, info: (m: string) => void, debug: (m: string) => void }}
 */
export function createLog({ logger = null, tag = '[dsh-notifier-gate]', prefix = null } = {}) {
  const consolePrefix = prefix ?? tag
  const viaLogger = (level, message) => {
    try {
      const fn = logger?.[level]
      if (typeof fn === 'function') fn.call(logger, tag, message)
    } catch { /* 日志失败绝不致命 */ }
  }
  const viaConsole = (level, message) => {
    try {
      const fn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.info
      fn.call(console, consolePrefix, message)
    } catch { /* 控制台不可用（极少数宿主）不致命 */ }
  }
  return {
    /** 真正的故障：两条通道都出。 */
    error(message) {
      viaLogger('error', message)
      if (CONSOLE_ENABLED) viaConsole('error', message)
    },
    /** 降级/可忽略异常：两条通道都出（默认），保持「故障一定可见」。 */
    warn(message) {
      viaLogger('warn', message)
      if (CONSOLE_ENABLED) viaConsole('warn', message)
    },
    /** 常规生命周期：默认只进 logger，**不进控制台**。 */
    info(message) {
      viaLogger('info', message)
      if (VERBOSE) viaConsole('info', message)
    },
    /** 高频诊断：只进 logger.debug。 */
    debug(message) {
      viaLogger('debug', message)
    },
  }
}
