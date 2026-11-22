// dsh-notifier-gate src/assembly/inbound-signals.mjs
// 入站通道 resolve/启用信号（原 dsh-notifier 六通道版本裁剪为 QQ 单通道）。
// 纯计算，无副作用；任何解析失败只 warn + 标记 !ok，绝不弄崩装配。

import { resolveEnvRefs } from '../config.mjs'
import { resolveQqInboundConfig } from '../inbound/qq-gw.mjs'
import { accountOf } from './outbound.mjs'

/**
 * 解析 QQ 入站启用信号与 resolved 配置。
 * @param {{ inboundRaw: object, approvalRaw: object, resolved: object, store: object,
 *           adminEnabled: boolean, warn: (msg: string) => void }} deps
 */
export function resolveInboundSignals({ inboundRaw, approvalRaw, resolved, store, adminEnabled, warn }) {
  // 阶段 4：inbound 白名单（allowUsers 为空 = 整栈不启动，默认全拒）。
  const allowUsers = (Array.isArray(inboundRaw.allowUsers) ? inboundRaw.allowUsers : [])
    .map((id) => String(id).trim())
    .filter((id) => id !== '')
  const approvalWanted = approvalRaw.mode === 'answer' || approvalRaw.mode === 'observe'

  // QQ 官方机器人 inbound：显式配置 inbound.qq（可为空对象——空 = 走扫码 CLI 落盘凭证）时启用；
  // 裸协议实现（WS 网关 + REST），无 SDK 依赖。
  // v0.3.1：凭证缺省回落扫码 CLI 落盘的 qq:account（config 显式配置优先）。
  // v0.3.3：admin 开启时，store 存在 qq:account 本身即启用信号。
  // dsh-notifier-qq 扩展：未显式配置 inbound.qq 时，回落本副本的出站 qq-bot 渠道凭证
  // （同一 appId/appSecret 既是出站凭证也是网关凭证），使「只配了出站」的用户无需重复填写。
  const qqExplicit = inboundRaw.qq !== null && typeof inboundRaw.qq === 'object'
  const qqAccount = accountOf(store, 'qq:account')
  const qqOutbound = resolved.channels.find((entry) => entry.type === 'qq-bot')
  const qqWanted = qqExplicit || qqAccount !== null || qqOutbound !== undefined
  const qqRaw = qqExplicit
    ? inboundRaw.qq
    : {
        appId: qqOutbound?.config?.appId ?? '',
        appSecret: qqOutbound?.config?.appSecret ?? '',
      }
  const qqResolved = qqWanted
    ? resolveQqInboundConfig(resolveEnvRefs(qqRaw), {
        credentials: qqAccount ?? (qqOutbound === undefined
          ? undefined
          : { appId: qqOutbound.config.appId, appSecret: qqOutbound.config.appSecret }),
      })
    : null
  if (qqResolved !== null && !qqResolved.ok) warn(`inbound.qq 跳过: ${qqResolved.reason}`)
  const qqOk = qqResolved?.ok === true

  return {
    allowUsers, // 空 = 整栈不启动，默认全拒
    approvalWanted, // approvalRaw.mode ∈ answer/observe
    qqResolved, qqOk,
  }
}
