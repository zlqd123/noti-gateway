// dsh-notifier-qq src/compat/mini-yaml.mjs
// 最小 YAML 子集解析器（零依赖）。
//
// 为什么需要它：本副本要读取「原 dsh-notifier 插件的配置」，而原配置写在 profile 的
// cordis.patch.yml 里。插件不得引入运行时依赖（零依赖军规），宿主也不保证 yaml/js-yaml
// 可从插件位置解析，所以自带一个只覆盖 DSH profile patch 实际用到的语法子集的解析器：
//   - 块映射（`key: value`，缩进决定层级）
//   - 块序列（`- item` / `- key: value` 起始的行内映射）
//   - 引号标量（单/双引号）、裸标量、数字、布尔、null、空值（→ {} 或 []）
//   - `#` 注释（仅行首或空白后的 `#`，引号内的 `#` 不算注释）
//   - 文档起始 `---` 与空行
// 明确不支持（遇到即原样当字符串，不猜测）：块标量 `|` `>`、锚点 `&` `*`、flow 集合
// `[...]` `{...}`、标签 `!!`、多文档 `---` 分隔后的第二份文档。这些在 DSH profile patch
// 里不会出现；若未来出现，调用方应先尝试真解析器（见 original-config.mjs 的分层策略）。

/** 缩进宽度：取行首空格数（tab 视为非法，按 1 空格处理并原样保留语义）。 */
function indentOf(line) {
  let n = 0
  for (const ch of line) {
    if (ch === ' ') n += 1
    else if (ch === '\t') n += 1
    else break
  }
  return n
}

/** 去掉行尾注释：引号感知，只有引号外且前面是行首/空白的 `#` 才算注释。 */
function stripComment(text) {
  let quote = ''
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    if (quote !== '') {
      if (ch === quote) {
        // 双引号内的 \" 不结束字符串
        if (quote === '"' && text[i - 1] === '\\') continue
        quote = ''
      }
      continue
    }
    if (ch === '"' || ch === "'") { quote = ch; continue }
    if (ch === '#' && (i === 0 || text[i - 1] === ' ' || text[i - 1] === '\t')) {
      return text.slice(0, i)
    }
  }
  return text
}

/** 标量归一：去引号、转数字/布尔/null，其余原样（trim）。 */
function parseScalar(raw) {
  const text = raw.trim()
  if (text === '') return { empty: true, value: null }
  if (text === '~' || text === 'null' || text === 'Null' || text === 'NULL') return { empty: false, value: null }
  if (text === 'true' || text === 'True' || text === 'TRUE') return { empty: false, value: true }
  if (text === 'false' || text === 'False' || text === 'FALSE') return { empty: false, value: false }
  // 空 flow 集合：DSH profile patch 里确实会出现（`channels: []`、`chatTemplateArgs: {}`），
  // 必须还原成真正的空数组/空对象，不能当成字符串。
  if (text === '[]') return { empty: false, value: [] }
  if (text === '{}') return { empty: false, value: {} }
  if (text.startsWith('"') && text.endsWith('"') && text.length >= 2) {
    try { return { empty: false, value: JSON.parse(text) } } catch { return { empty: false, value: text.slice(1, -1) } }
  }
  if (text.startsWith("'") && text.endsWith("'") && text.length >= 2) {
    // 单引号内 '' 表示一个 '
    return { empty: false, value: text.slice(1, -1).replace(/''/g, "'") }
  }
  // 纯数字（含负数、小数）：仅当整体可解析且不是前导零字符串形态时转数字
  if (/^-?(0|[1-9]\d*)(\.\d+)?$/.test(text)) {
    const num = Number(text)
    if (Number.isFinite(num)) return { empty: false, value: num }
  }
  return { empty: false, value: text }
}

/** 是否是「key:」形态的行（key 后可跟冒号+空格或行尾冒号）。 */
function matchKey(line) {
  const m = /^([^:#][^:]*?):(?:[ \t]|$)/.exec(line)
  if (m === null) return null
  let key = m[1].trim()
  if ((key.startsWith('"') && key.endsWith('"')) || (key.startsWith("'") && key.endsWith("'"))) {
    key = key.slice(1, -1)
  }
  return { key, rest: line.slice(m[0].length) }
}

function isSeqLine(line) {
  return line === '-' || line.startsWith('- ')
}

/**
 * 解析 YAML 文本为 JS 值。
 * @param {string} text
 * @returns {unknown}
 */
export function parseMiniYaml(text) {
  const lines = []
  for (const original of String(text ?? '').split(/\r?\n/)) {
    const noComment = stripComment(original)
    if (noComment.trim() === '') continue
    const trimmed = noComment.trim()
    if (trimmed === '---' || trimmed === '...') continue
    lines.push({ indent: indentOf(noComment), text: noComment.slice(indentOf(noComment)) })
  }
  if (lines.length === 0) return null

  let index = 0

  /** 解析一个「缩进层级为 baseIndent 的块」。isSeqRoot 决定顶层是序列还是映射。 */
  function parseBlock(baseIndent) {
    if (index >= lines.length) return null
    const first = lines[index]
    if (first.indent < baseIndent) return null
    if (isSeqLine(first.text)) return parseSequence(first.indent)
    return parseMapping(first.indent)
  }

  function parseSequence(indent) {
    const out = []
    while (index < lines.length) {
      const line = lines[index]
      if (line.indent !== indent || !isSeqLine(line.text)) break
      index += 1
      const body = line.text === '-' ? '' : line.text.slice(2)
      const bodyTrimmed = body.trim()
      if (bodyTrimmed === '') {
        // `-` 后换行：子块在更深缩进
        const child = parseBlock(indent + 1)
        out.push(child === null ? null : child)
        continue
      }
      const keyed = matchKey(body)
      if (keyed !== null && !bodyTrimmed.startsWith('{') && !bodyTrimmed.startsWith('[')) {
        // `- key: value`：把该行当作「缩进 = indent + 2」的映射首行
        const virtualIndent = indent + (line.text.length - body.length)
        lines.splice(index, 0, { indent: virtualIndent, text: body })
        out.push(parseMapping(virtualIndent))
        continue
      }
      const scalar = parseScalar(body)
      out.push(scalar.empty ? null : scalar.value)
    }
    return out
  }

  function parseMapping(indent) {
    const out = {}
    while (index < lines.length) {
      const line = lines[index]
      if (line.indent !== indent) break
      if (isSeqLine(line.text)) break
      const keyed = matchKey(line.text)
      if (keyed === null) { index += 1; continue }
      const { key, rest } = keyed
      const restTrimmed = rest.trim()
      index += 1
      if (restTrimmed === '') {
        // 值在更深缩进：可能是序列或映射；也可能为空（→ null）
        const next = lines[index]
        if (next !== undefined && next.indent > indent) {
          out[key] = parseBlock(next.indent)
        } else if (next !== undefined && next.indent === indent && isSeqLine(next.text)) {
          // 同缩进的 `-` 序列（YAML 允许序列与 key 同缩进）
          out[key] = parseSequence(indent)
        } else {
          out[key] = null
        }
        continue
      }
      const scalar = parseScalar(rest)
      out[key] = scalar.empty ? null : scalar.value
      // 多行标量续行：`key: 文本` 之后紧跟更深缩进、且不是 `key:` / `- ` 形态的行，
      // 是同一标量的折行续写（YAML plain scalar 自动折行）。按空格折叠成一行——
      // DSH profile patch 里的英文描述常这样折行，丢掉续行会截断文案。
      if (scalar.empty || typeof scalar.value !== 'string') continue
      const foldParts = [scalar.value]
      while (index < lines.length) {
        const cont = lines[index]
        if (cont.indent <= indent) break
        if (isSeqLine(cont.text) || matchKey(cont.text) !== null) break
        foldParts.push(cont.text.trim())
        index += 1
      }
      if (foldParts.length > 1) out[key] = foldParts.join(' ')
    }
    return out
  }

  const result = parseBlock(0)
  return result
}
