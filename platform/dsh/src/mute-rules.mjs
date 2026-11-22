/**
 * 静音规则解析：把插件设置页的 volatile `muteRules` 覆盖到出站解析结果上。
 *
 * 规则值（见 config-schema.mjs）：
 *  - true     = 完全静音（quiet）
 *  - false    = 显式放行（忽略上层 workspace 的静音）
 *  - string[] = 渠道白名单（仅发到列出的类型）
 *
 * 键优先级：精确 sessionId > workspace 名。
 * 未命中任何键时返回 null（调用方保持原路由结果不变）。
 *
 * @param {object|null} muteRulesRef - volatile ref（带 .get()）或普通对象；为空返回 null。
 * @param {string} sessionId - 会话 id。
 * @param {string} workspace - 工作区名。
 * @returns {null|{quiet?: boolean, channelTypes?: string[], source: string}}
 */
export function resolveMuteRule(muteRulesRef, sessionId, workspace) {
  // volatile ref 每次调用都重新 .get()，保证设置页改完立即生效（不解构、不缓存）。
  let rules = null
  try {
    rules = typeof muteRulesRef?.get === 'function' ? muteRulesRef.get() : muteRulesRef
  } catch {
    return null
  }
  if (rules === null || typeof rules !== 'object' || Array.isArray(rules)) return null

  const sid = String(sessionId ?? '')
  const ws = String(workspace ?? '')
  // 精确会话优先，其次 workspace。命中第一个合法规则即止。
  const candidates = [
    ['session', sid],
    ['workspace', ws],
  ]
  for (const [source, key] of candidates) {
    if (key === '') continue
    if (!Object.prototype.hasOwnProperty.call(rules, key)) continue
    const rule = rules[key]
    return normalizeRule(rule, source)
  }
  return null
}

function normalizeRule(rule, source) {
  if (rule === true) return { quiet: true, source }
  if (rule === false) return { quiet: false, source }
  if (Array.isArray(rule)) {
    const types = rule.filter((t) => typeof t === 'string' && t.trim() !== '').map((t) => t.trim())
    return { channelTypes: types, source }
  }
  return null
}
