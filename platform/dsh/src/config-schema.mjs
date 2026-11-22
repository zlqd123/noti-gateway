import z from '@deepseek-ai/schemastery'

/**
 * 静音规则值：
 *  - true            = 完全静音该项目/会话（仍写账本，等价于旧 route.agents.quiet）
 *  - false           = 显式不静音（覆盖上层 workspace 静音）
 *  - string[]        = 白名单：仅推送到列出的渠道类型（如 ['qq-bot']）
 */
const MuteRule = z.union([z.boolean(), z.array(z.string())])

/**
 * DSH 原生设置页 Config。
 *
 * 只声明新增的 muteRules（volatile：设置页修改后原地热更新，无需重启插件）。
 * 其余既有字段（enabled / channels / events / approval / inbound / ...）不在 schema 中
 * 声明——schemastery 对 z.object 未声明字段原样保留，故既有配置完全不受影响。
 *
 * muteRules 的键有两种：
 *  - workspace 名（同项目多会话聚合，如 "workspace2"）
 *  - 精确会话 id（如 "session-xxxx"，优先级高于 workspace）
 * 解析优先级：会话 id > workspace；命中即按规则值处理，未命中走原路由链。
 */
export const Config = z.object({
  muteRules: z.dict(MuteRule, z.string()).default({}).volatile(),
})
