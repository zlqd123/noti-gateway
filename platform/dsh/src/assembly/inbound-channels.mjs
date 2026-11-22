// dsh-notifier-gate src/assembly/inbound-channels.mjs
// Inbound channel registry and lifecycle wiring（原六通道版本裁剪为 QQ 单通道）。
//
// This module owns only transport assembly: the optional QQ channel factory is
// started independently, the successful instance is indexed for approval/reply
// routing, and it is stopped defensively. Permission, approval, question, and
// conversation semantics remain in their existing modules.

import { createQqInbound } from '../inbound/qq-gw.mjs'
import { createLog } from '../log.mjs'

// QQ 入站工厂（裸 WS 网关 + REST，零 SDK 依赖）。调用方经 `factories` 覆盖（测试用）。
const DEFAULT_FACTORIES = Object.freeze({
  qq: createQqInbound,
})

/**
 * Start the configured inbound transport and expose the registry used by the
 * control/approval layers.
 *
 * @param {{
 *   qqOk?: boolean, qqResolved?: object|null,
 *   bus: object, vault: object, store: object, identity: object,
 *   actions: object|null, questions: object|null, control: object,
 *   allowUsers?: string[], logger?: object, warn?: (message: string) => void,
 *   factories?: object
 * }} deps
 * @returns {{ interactiveInstances: object[], replyTargets: Map<string, object>,
 *   runtimeOf: (name: string) => { state: string, active: boolean, restartPending: boolean },
 *   runtimeSnapshot: () => Array<{ type: string, state: string, active: boolean, restartPending: boolean }>,
 *   dispose: () => Promise<void> }}
 */
export function createInboundChannelRegistry(deps = {}) {
  const {
    qqOk = false, qqResolved = null,
    bus, vault, store, identity, actions = null, questions = null, control,
    strings = null, // lang 文案表（stringsOf(lang)）：透传给渠道适配器
    allowUsers = [], logger = null,
    warn = () => {}, factories = {},
  } = deps
  const factory = { ...DEFAULT_FACTORIES, ...factories }
  // 启动成功走 info（常规生命周期，不进控制台）；失败仍走调用方注入的 warn。
  const log = createLog({ logger, tag: '[dsh-notifier-gate/inbound]' })
  const info = (message) => log.info(message)
  const interactiveInstances = []
  const replyTargets = new Map()
  // 真实运行时真值——只有 start() 成功的实例才入表。
  const running = new Map()

  const startInboundChannel = (name, boot) => {
    try {
      return boot()
    } catch (error) {
      warn(`inbound:${name} 装配失败，已跳过（其余能力不受影响）: ${error instanceof Error ? error.message : String(error)}`)
      return null
    }
  }

  const attach = (name, create, options, readyMessage) => {
    const instance = startInboundChannel(name, () => {
      const value = create(options)
      try {
        value.start()
      } catch (error) {
        // A factory may allocate a socket before start() reports failure. Give
        // that instance one best-effort stop before the channel is abandoned.
        try { value.stop?.() } catch { /* cleanup is best effort */ }
        throw error
      }
      interactiveInstances.push(value)
      replyTargets.set(name, value)
      running.set(name, value)
      // 启动成功属正常生命周期：info（只进 logger），不占控制台告警位。
      info(readyMessage())
      return value
    })
    return instance
  }

  if (qqOk) {
    attach('qq', factory.qq, {
      config: qqResolved.config, bus, fallbackTargets: allowUsers, identity, logger, strings,
    }, () => 'inbound 已启动：qq WebSocket 网关（文本审批通知 + 编号回复裁决）')
  }

  /**
   * 薄聚合 query——「已配置」是期望态，本函数回答「运行时是否真的在线」。
   * 绝不把 persisted 配置当成 online。
   */
  const runtimeOf = (name) => {
    const instance = running.get(String(name ?? '').trim())
    if (instance === undefined) return { state: 'stopped', active: false, restartPending: true }
    let state = 'online'
    try {
      const raw = typeof instance?.clientState === 'function' ? String(instance.clientState() ?? '').trim() : ''
      if (raw !== '') state = raw
    } catch { state = 'unknown' }
    const active = state === 'online' || state === 'connected'
    return { state, active, restartPending: !active }
  }
  const runtimeSnapshot = () => [...running.keys()].map((name) => ({ type: name, ...runtimeOf(name) }))

  const dispose = async () => {
    const pending = []
    for (const instance of interactiveInstances) {
      try {
        const result = instance.stop?.()
        if (result !== null && typeof result?.then === 'function') pending.push(result)
      } catch { /* 单通道停机失败不影响其它通道 */ }
    }
    running.clear()
    await Promise.allSettled(pending)
  }

  return { interactiveInstances, replyTargets, runtimeOf, runtimeSnapshot, dispose }
}
