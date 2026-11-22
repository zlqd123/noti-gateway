// dsh-notifier-gate inbound/channels-registry.mjs
// 入站渠道枚举的单一事实来源。
//
// 本副本为 QQ 专项裁剪版：入站通道全集只有 qq 一个。原六通道清单
// （telegram/feishu/qq/wxpusher/wechat/dingtalk）在裁剪时一并收敛。
//
// 军规：任何消费方不得内联清单字面量，一律从这里引；新增渠道只改本文件。
// 冻结数组 + 冻结 Set 双形态导出（消费方按用法取用，避免各自 new Set）。

/** 入站通道全集（冻结；顺序 = 管理台与能力矩阵的展示序）。 */
export const INBOUND_CHANNELS = Object.freeze([
  'qq',
])

/** 同一清单的 Set 形态（冻结；has() 查询用）。 */
export const INBOUND_CHANNEL_SET = new Set(INBOUND_CHANNELS)
