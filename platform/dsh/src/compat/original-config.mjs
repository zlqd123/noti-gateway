// dsh-notifier-qq src/compat/original-config.mjs
// 读取「原 dsh-notifier 插件」的配置与状态，供本副本直接复用。
//
// 为什么需要：本副本是原插件的 QQ 专项裁剪版，用户的 QQ 凭证、审批绑定、会话路由
// 记录都在原插件名下。让用户为了装副本再抄一遍凭证与重做配对是不必要的负担，所以
// 装配期按下面的优先级把原插件的值「借」过来（只读，绝不写原插件的任何文件）。
//
// 凭证来源优先级（高 → 低）：
//   1. 本插件自己的 cordis 配置行（用户显式写给 dsh-notifier-qq 的，最高优先）
//   2. 本插件 state 目录里的 state.json（本插件自己跑过后的持久态）
//   3. 原插件在同一 profile 的 cordis.patch.yml 配置行（dsh-notifier 行）
//   4. 原插件 state 目录的 state.json（原插件的持久态）
//
// 为什么是「借」而不是「迁移」：原插件可能仍在用（用户没有卸载它）。单向只读借用
// 不会破坏原插件的状态；本副本写入永远落在自己的 state 目录。

import { existsSync, readFileSync } from 'node:fs'
import { basename, dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseMiniYaml } from './mini-yaml.mjs'

/** 原插件的包名与默认 state 目录名（两者同名，分开列出以便未来漂移）。 */
export const ORIGINAL_PACKAGE = 'dsh-notifier'
export const ORIGINAL_STATE_DIRNAME = 'dsh-notifier'
/** 原插件在 profile patch 里的 loader entry id（= 包名）。 */
export const ORIGINAL_ENTRY_ID = 'dsh-notifier'

/** 解析 $DSH_HOME（宿主约定），回退 ~/.dsh。 */
export function dshHome() {
  const home = process.env.DSH_HOME
  if (typeof home === 'string' && home.trim() !== '') return home.trim()
  const userHome = process.env.HOME ?? process.env.USERPROFILE
  return typeof userHome === 'string' && userHome !== '' ? join(userHome, '.dsh') : null
}

/**
 * 从本模块自身的安装位置反推 profile 目录。
 *
 * 为什么需要这条回退：宿主进程并不保证把 DSH_PROFILE_DIR / DSH_PROFILE 导出给插件
 * 的运行时（真机 2026-10-24 复现：插件在 17:44 启动的 dsh web 进程里，process.env
 * 没有这两个变量，于是 readOriginalConfig() 返回 profileDir=null、借不到任何配置，
 * channels 为空 → 「已启用通道」空 → 所有通知被静默丢弃）。而插件的真实位置是确定的：
 *   <profile>/node_modules/dsh-notifier-qq/src/compat/original-config.mjs
 * 往上四层就是 <profile>。位置事实比环境变量可靠，故用它兜底。
 *
 * @returns {string|null} profile 目录绝对路径；无法推断时 null
 */
export function profileDirFromModule() {
  try {
    const here = fileURLToPath(import.meta.url) // .../node_modules/dsh-notifier-qq/src/compat/original-config.mjs
    const dir = dirname(dirname(dirname(dirname(here)))) // -> .../node_modules 的父级 = <profile>
    if (basename(dir) === 'node_modules') {
      // 兜底：若层级比预期浅一层，再退一级
      const parent = dirname(dir)
      return existsSync(join(parent, 'cordis.patch.yml')) || existsSync(join(parent, 'package.json')) ? parent : null
    }
    return dir
  } catch {
    return null
  }
}

/**
 * 解析当前 profile 目录，按可靠性排序：
 *   1. $DSH_PROFILE_DIR（宿主显式给出，最权威）
 *   2. 从插件自身安装位置反推（位置事实，宿主缺 env 时仍成立）
 *   3. $DSH_HOME/profiles/$DSH_PROFILE
 */
export function profileDir() {
  const explicit = process.env.DSH_PROFILE_DIR
  if (typeof explicit === 'string' && explicit.trim() !== '') return explicit.trim()

  const fromModule = profileDirFromModule()
  if (fromModule !== null) {
    // 只接受「确实像 profile 目录」的推断结果（含 cordis.patch.yml 或 package.json）
    if (existsSync(join(fromModule, 'cordis.patch.yml')) || existsSync(join(fromModule, 'package.json'))) {
      return fromModule
    }
  }

  const home = dshHome()
  const profile = process.env.DSH_PROFILE
  if (home === null || typeof profile !== 'string' || profile.trim() === '') return null
  return join(home, 'profiles', profile.trim())
}

/** 安全读文本：不存在/读失败返回 null（绝不抛）。 */
function readTextOrNull(file) {
  try {
    if (!existsSync(file)) return null
    return readFileSync(file, 'utf8')
  } catch { return null }
}

/** 安全读 JSON 对象：损坏/形状不对返回 null（绝不抛）。 */
export function readJsonObjectOrNull(file) {
  const text = readTextOrNull(file)
  if (text === null || text.trim() === '') return null
  try {
    const parsed = JSON.parse(text)
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch { return null }
}

/**
 * 解析 profile 的 cordis.patch.yml，取出指定 loader entry id 的 config。
 *
 * 只接受 `- id: <entryId>` 形态的 patch 行（profile patch 的标准写法）。
 * 同一 id 出现多次时按 YAML 顺序后者覆盖前者的 config 键（与 loader 的
 * 「后写的 patch 覆盖前写的」语义一致）；`disabled: true` 的行不取 config。
 *
 * @param {string} profilePatchPath
 * @param {string} entryId
 * @returns {{ config: object|null, found: boolean, disabled: boolean }}
 */
export function readEntryConfigFromProfilePatch(profilePatchPath, entryId) {
  const text = readTextOrNull(profilePatchPath)
  if (text === null) return { config: null, found: false, disabled: false }
  let rows
  try {
    rows = parseMiniYaml(text)
  } catch { return { config: null, found: false, disabled: false } }
  if (!Array.isArray(rows)) return { config: null, found: false, disabled: false }

  let merged = null
  let found = false
  let disabled = false
  for (const row of rows) {
    if (row === null || typeof row !== 'object' || Array.isArray(row)) continue
    if (row.id !== entryId) continue
    found = true
    if (row.disabled === true) { disabled = true; continue }
    // 用户后来把行重新启用时，disabled 标记应作废
    disabled = false
    const config = row.config
    if (config === null || typeof config !== 'object' || Array.isArray(config)) continue
    merged = { ...(merged ?? {}), ...config }
  }
  return { config: merged, found, disabled }
}

/** 从原插件 channels[] 行里挑出 qq-bot 行（返回去掉 type 的 config 副本）。 */
export function qqBotRowOf(config) {
  const rows = Array.isArray(config?.channels) ? config.channels : []
  for (const row of rows) {
    if (row === null || typeof row !== 'object' || Array.isArray(row)) continue
    if (String(row.type ?? '').trim() !== 'qq-bot') continue
    if (row.enabled === false) return null // 显式禁用是用户意志，不借用
    const { type: _drop, ...rest } = row
    return rest
  }
  return null
}

/**
 * 汇总「原插件配置」的快照（只读）。
 * @param {{ stateDir?: string|null }} [options] - 本插件的 state 目录（用于定位同 profile 的兄弟目录）
 * @returns {{
 *   profileDir: string|null,
 *   patchPath: string|null,
 *   outbound: object|null,        // 原插件 qq-bot 出站配置（channels[] 里那一行）
 *   inboundQq: object|null,       // 原插件 inbound.qq
 *   approval: object|null,        // 原插件 approval 块
 *   events: object|null,          // 原插件 events 块
 *   routing: object|null,         // 原插件 routing 块
 *   segment: object|null,         // 原插件 segment 块
 *   debounceMs: number|undefined,
 *   summaryMaxChars: number|undefined,
 *   titlePrefix: string|undefined,
 *   redaction: string|undefined,
 *   lang: string|undefined,
 *   originalStateDir: string|null,
 *   originalState: object|null,   // 原插件 state.json（只读快照）
 *   entryFound: boolean,          // profile patch 里是否有原插件行
 *   entryDisabled: boolean,
 * }}
 */
export function readOriginalConfig(options = {}) {
  const dir = profileDir()
  const patchPath = dir === null ? null : join(dir, 'cordis.patch.yml')
  const { config, found, disabled } = patchPath === null
    ? { config: null, found: false, disabled: false }
    : readEntryConfigFromProfilePatch(patchPath, ORIGINAL_ENTRY_ID)

  const home = dshHome()
  const originalStateDir = home === null ? null : join(home, ORIGINAL_STATE_DIRNAME)
  const originalState = originalStateDir === null
    ? null
    : readJsonObjectOrNull(join(originalStateDir, 'state.json'))

  const inboundQq = (config?.inbound !== null && typeof config?.inbound === 'object'
    && config.inbound.qq !== null && typeof config.inbound.qq === 'object')
    ? { ...config.inbound.qq }
    : null

  return {
    profileDir: dir,
    patchPath,
    outbound: qqBotRowOf(config),
    inboundQq,
    approval: (config?.approval !== null && typeof config?.approval === 'object') ? { ...config.approval } : null,
    events: (config?.events !== null && typeof config?.events === 'object') ? { ...config.events } : null,
    routing: (config?.routing !== null && typeof config?.routing === 'object') ? { ...config.routing } : null,
    segment: (config?.segment !== null && typeof config?.segment === 'object') ? { ...config.segment } : null,
    debounceMs: typeof config?.debounceMs === 'number' ? config.debounceMs : undefined,
    summaryMaxChars: typeof config?.summaryMaxChars === 'number' ? config.summaryMaxChars : undefined,
    titlePrefix: typeof config?.titlePrefix === 'string' ? config.titlePrefix : undefined,
    redaction: typeof config?.redaction === 'string' ? config.redaction : undefined,
    lang: typeof config?.lang === 'string' ? config.lang : undefined,
    originalStateDir,
    originalState,
    entryFound: found,
    entryDisabled: disabled,
  }
}

/**
 * 把原插件 state.json 里属于「身份/路由」的持久键搬进本插件的 store（幂等、只增不减）。
 *
 * 只搬四类（其余键属于原插件的内部实现细节，不猜不搬）：
 *   - inbound:bindings  （配对绑定：谁是 owner）
 *   - inbound:pairing   （配对码账本：保历史，供管理台展示）
 *   - route:sessions    （会话路由：同一会话仍投到同一目标）
 *   - aq:*              （待决远程提问：重启后仍可编号回复）
 *
 * 写策略：目标键「已存在则不动」（用户在本副本里的后续操作永远优先），
 * 因此重复调用等价于首次调用。
 *
 * @param {object} store - 本插件的 store（createStore 返回）
 * @param {object} sourceState - 原插件 state.json 快照
 * @param {(msg: string) => void} [warn]
 * @returns {{ copied: string[], kept: string[] }}
 */
export function seedIdentityFromOriginalState(store, sourceState, warn = () => {}) {
  const copied = []
  const kept = []
  if (sourceState === null || typeof sourceState !== 'object') return { copied, kept }
  const DIRECT_KEYS = ['inbound:bindings', 'inbound:pairing', 'route:sessions']
  const prefixKeys = Object.keys(sourceState).filter((key) => key.startsWith('aq:'))
  for (const key of [...DIRECT_KEYS, ...prefixKeys]) {
    if (!Object.prototype.hasOwnProperty.call(sourceState, key)) continue
    let existing
    try { existing = store.get(key) } catch { existing = undefined }
    if (existing !== undefined && existing !== null) { kept.push(key); continue }
    try {
      store.set(key, sourceState[key])
      copied.push(key)
    } catch (error) {
      warn(`原插件状态键 ${key} 借用失败（继续运行）: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
  return { copied, kept }
}

/** 把相对路径解析为绝对路径（用于测试与自定义 stateDir）。 */
export function absoluteOf(path) {
  if (typeof path !== 'string' || path.trim() === '') return null
  return isAbsolute(path) ? path : resolve(path)
}
