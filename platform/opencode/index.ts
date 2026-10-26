/**
 * opencode-qq-notify — QQ 官方机器人(C2C)通知 + 卡片审批插件
 *
 * 节点检测架构复刻自 opencode-notifier (MIT, mohak34):
 *   事件总线订阅 + 事件 id 去重 + idle 抖动合流 + 错误墓碑 + permission 延迟回核 + 多例归属
 * 推送渠道: QQ 开放平台 REST /v2/users/{openid}/messages
 *   - 普通通知: 纯文本主动消息
 *   - 审批: markdown 卡片 + 内嵌键盘(action.type=1 回调按钮 + Modal 二次确认), 失败降级纯文本
 * 审批通道: 插件内 WebSocket 网关监听 INTERACTION_CREATE(按钮回调), 解析后调 ctx.permission.reply
 *
 * 本机 OpenCode v2.0.22 实测事实:
 *   - 事件载体在 event.data (不是 SDK 类型文档的 event.properties)
 *   - 完成节点: session.execution.started(succeeded/failed), data.sessionID 归属
 *   - ctx.session.get 返回 location.directory; 标题异步生成
 *   - 运行时无 "@opencode/plugin" 别名, 零依赖纯默认导出
 *   - QQ token 接口返回 snake_case: access_token / expires_in
 */

import { readFileSync, appendFileSync } from "node:fs"
import { join } from "node:path"
import { homedir } from "node:os"

type QQConfig = {
  appId: string
  clientSecret: string
  /** C2C 目标用户 openid */
  openid: string
  sandbox?: boolean
  events?: { complete?: boolean; error?: boolean; permission?: boolean; question?: boolean }
  /** 秒; 会话从 busy 到 idle 短于此则不发 complete */
  minDuration?: number
  /** 审批通知延迟毫秒: 到点回核仍 pending(autoaccept 没接住)才推 */
  permissionWaitMs?: number
  /** 这些动作不推送(例: ["external_directory"]); 留空 = 全部推送 */
  permissionSilence?: string[]
  /** 静音动作对运行时的自动回复: once=自动批准(默认), reject=自动拒绝; 回复才算消费掉 ask, TUI 角标(感叹号)才消失 */
  permissionSilenceReply?: "once" | "reject"
  /** 仅通知目录包含这些前缀的会话; 空 = 全部 */
  onlyProjects?: string[]
  /** HTTP 代理 http://host:port (Bun fetch 生效); 留空走环境变量 */
  proxy?: string
  /** 卡片失败时也尝试纯文本 */
  cardFallback?: boolean
  /** 是否开启 WebSocket 监听(接收按钮回调/文字指令执行审批) */
  cardInbound?: boolean
  /** 统一通知网关(noti-gateway)：纯文本推送优先走它(统一配额/持久化/重试)，失败回退直连；带按钮的卡片始终直连 */
  gatewayUrl?: string
  gatewayToken?: string
  gatewaySource?: string
  debug?: boolean
}

type SessionInfo = {
  title?: string
  parentID?: string
  directory?: string
  model?: { providerID: string; id: string }
}

const DEFAULTS = {
  sandbox: false,
  events: { complete: true, error: true, permission: true, question: true },
  minDuration: 0,
  permissionWaitMs: 5000,
  permissionSilenceReply: "once",
  cardFallback: true,
  cardInbound: true,
  debug: false,
}
const OPTION_FILE = join(homedir(), ".config", "opencode", "gate-notify.jsonc")
const LOG_FILE = join(homedir(), ".config", "opencode", "gate-notify.log")

// ---------- 状态机常量(借鉴 notifier) ----------
const IDLE_COMPLETE_DELAY_MS = 1000
const COMPLETE_DUP_WINDOW_MS = 3000
const TOMBSTONE_TTL_MS = 5 * 60_000
const PENDING_PERM_TTL_MS = 10 * 60_000
const DEDUPE_CAP = 2048

function idleDelayMs(cfg: any): number {
  const v = Number(cfg?.idleDelayMs)
  return Number.isFinite(v) && v >= 0 ? v : IDLE_COMPLETE_DELAY_MS
}
function permissionWaitMs(cfg: any): number {
  const v = Number(cfg?.permissionWaitMs)
  return Number.isFinite(v) && v >= 0 ? v : 5000
}

// ---------- 工具 ----------
const seen = new Set<string>()
function first(key: string): boolean {
  if (seen.has(key)) return false
  if (seen.size >= DEDUPE_CAP) seen.delete(seen.keys().next().value as string)
  seen.add(key)
  return true
}
const norm = (p = "") => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase()
const needsFill = (v?: string) => !v || v.includes("在此填")
function writeLog(msg: string) {
  try {
    appendFileSync(LOG_FILE, `${new Date().toISOString()} ${msg}\n`)
  } catch {}
}
function log(...args: unknown[]) {
  console.error("[qq-notify]", ...args)
  writeLog(args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" "))
}

function readOptionFile(): Partial<QQConfig> {
  try {
    const text = readFileSync(OPTION_FILE, "utf8")
    const json = text.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n")
    return JSON.parse(json)
  } catch {
    return {}
  }
}

// ==================== QQ REST 客户端 ====================
type TokenState = { accessToken: string; expiresAt: number; refreshToken: string }
const token: TokenState = { accessToken: "", expiresAt: 0, refreshToken: "" }
let sendSeq = 0

async function fetchQQ(url: string, init: RequestInit, proxy?: string): Promise<Response> {
  if (proxy) return (fetch as any)(url, { ...init, proxy })
  return fetch(url, init)
}

async function refreshAccessToken(cfg: QQConfig, useRefreshToken: boolean): Promise<void> {
  const body: Record<string, string> = { appId: cfg.appId, clientSecret: cfg.clientSecret }
  if (useRefreshToken && token.refreshToken) {
    body.grantType = "refresh_token"
    body.refreshToken = token.refreshToken
  } else {
    body.grantType = "client_credentials"
  }
  const res = await fetchQQ(
    "https://bots.qq.com/app/getAppAccessToken",
    { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) },
    cfg.proxy,
  )
  if (!res.ok) throw new Error(`getAppAccessToken HTTP ${res.status}: ${await res.text().catch(() => "")}`)
  const j = (await res.json()) as any
  const accessToken: string = j.access_token ?? j.accessToken ?? ""
  const exp = Number(String(j.expires_in ?? j.expiresIn ?? 7200))
  token.accessToken = accessToken
  token.refreshToken = j.refresh_token ?? j.refreshToken ?? ""
  token.expiresAt = Date.now() + (Number.isFinite(exp) ? exp : 7200) * 1000 - 120_000
  if (cfg.debug) log("token 已刷新, 有效至", new Date(token.expiresAt).toISOString())
}

async function getAccessToken(cfg: QQConfig): Promise<string> {
  if (token.accessToken && Date.now() < token.expiresAt) return token.accessToken
  await refreshAccessToken(cfg, !!token.refreshToken)
  return token.accessToken
}

function apiBase(cfg: QQConfig): string {
  return cfg.sandbox ? "https://sandbox.api.sgroup.qq.com" : "https://api.sgroup.qq.com"
}

export type C2CPayload = {
  msg_type: 0 | 2
  content?: string
  markdown?: { content: string }
  keyboard?: { id?: string; content?: any }
  msg_seq: number
  msg_id?: string
}

/** 通版 C2C 发送; 401 自动换 token 重试一次 */
async function postC2C(cfg: QQConfig, payload: C2CPayload): Promise<{ ok: boolean; status?: number; body?: string }> {
  const base = apiBase(cfg)
  const doSend = async (accessToken: string): Promise<Response> => {
    sendSeq += 1
    const p: any = { ...payload, msg_seq: sendSeq }
    return fetchQQ(
      `${base}/v2/users/${cfg.openid}/messages`,
      {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `QQBot ${accessToken}` },
        body: JSON.stringify(p),
      },
      cfg.proxy,
    )
  }
  try {
    let res = await doSend(await getAccessToken(cfg))
    if (res.status === 401) {
      if (cfg.debug) log("token 过期, 刷新重试")
      await refreshAccessToken(cfg, false)
      res = await doSend(token.accessToken)
    }
    if (!res.ok) return { ok: false, status: res.status, body: await res.text().catch(() => "") }
    return { ok: true, body: await res.text().catch(() => "") }
  } catch (e) {
    log("QQ C2C 发送异常", (e as Error).message)
    return { ok: false }
  }
}

/** 经统一网关投递纯文本通知；成功返回 true，未配置/失败返回 false（调用方回退直连 QQ）。 */
async function sendViaGateway(cfg: QQConfig, content: string): Promise<boolean> {
  const base = (cfg.gatewayUrl ?? "").replace(/\/+$/, "")
  if (!base || !cfg.gatewayToken) return false
  const source = cfg.gatewaySource ?? "opencode"
  const lines = content.split("\n")
  const title = (lines[0] ?? "").trim().slice(0, 100) || "(无标题)"
  const body = lines.slice(1).join("\n").trim()
  try {
    const res = await fetchQQ(
      `${base}/n/${encodeURIComponent(source)}`,
      {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${cfg.gatewayToken}`, "x-source": source },
        body: JSON.stringify({ title, body }),
      },
      cfg.proxy,
    )
    return res.ok
  } catch (e) {
    log("网关投递异常", (e as Error).message)
    return false
  }
}

async function sendText(cfg: QQConfig, content: string): Promise<boolean> {
  if (await sendViaGateway(cfg, content)) {
    if (cfg.debug) log("经网关投递成功")
    return true
  }
  const r = await postC2C(cfg, { msg_type: 0, content: content.slice(0, 1800) })
  if (!r.ok) {
    log(`QQ C2C 发送失败 HTTP ${r.status} ${r.body ?? ""}`.trim())
    if (r.status === 404 || (r.body ?? "").includes("openid"))
      log("提示: openid 有误, 或该用户与机器人之间无会话记录(先在 QQ 里给机器人发过消息)")
    if (r.status === 400 || r.status === 403) log("提示: C2C 主动消息受额度/资质限制, 详见插件 README")
    return false
  }
  if (cfg.debug) log("发送成功 seq", sendSeq)
  return true
}

/** 审批卡片: markdown + 内嵌键盘(回调按钮); 失败降级为纯文本 */
async function sendPermissionCard(
  cfg: QQConfig,
  shortId: string,
  info: SessionInfo,
  action: string,
  resources: string[],
): Promise<boolean> {
  const md =
    `**🔐 OpenCode 权限审批 #${shortId}**\n` +
    `> 会话: ${info.title || "-"}\n` +
    `> 动作: ${action}\n` +
    `> 对象: ${resources.join("  ").slice(0, 300)}\n`
  const mkBtn = (id: string, label: string, data: string, style: number, modal?: any) => ({
    id,
    render_data: { label, visited_label: label, style },
    action: { type: 1, permission: { type: 2, specify_user_ids: [cfg.openid] }, data, ...(modal ? { modal } : {}) },
    group_id: `perm_${shortId}`,
  })
  const rows = [
    { buttons: [mkBtn("a", "批准(仅本次)", `permapp_${shortId}`, 4), mkBtn("r", "拒绝", `permrej_${shortId}`, 3)] },
    {
      buttons: [
        mkBtn("aa", "一直批准", `permalways_${shortId}`, 1, {
          content: "将对同类请求保存'始终允许'规则, 确认?",
          confirm_text: "批准",
          cancel_text: "取消",
        }),
        mkBtn("rr", "忽略(不回答)", `permnoop_${shortId}`, 0),
      ],
    },
  ]
  const card = await postC2C(cfg, {
    msg_type: 2,
    markdown: { content: md.slice(0, 3400) },
    keyboard: { content: { rows } },
  })
  if (card.ok) {
    if (cfg.debug) log("卡片发送成功 #", shortId)
    return true
  }
  log(`卡片发送失败 HTTP ${card.status} ${card.body ?? ""}`.trim())
  if (cfg.cardFallback === false) return false
  if (card.status === 400 && /markdown|keyboard|40|模板/i.test(card.body ?? "")) {
    log("疑似无 markdown/键盘权限, 降级纯文本")
    return sendText(
      cfg,
      `🔐 OpenCode 权限审批 #${shortId}\n${info.title || "-"}\n动作: ${action}\n对象: ${resources.join("  ").slice(0, 280)}`,
    )
  }
  return sendText(
    cfg,
    `🔐 OpenCode 权限审批 #${shortId}\n${info.title || "-"}\n动作: ${action}\n对象: ${resources.join("  ").slice(0, 280)}\n(卡片失败: ${card.body?.slice(0, 120) ?? card.status})`,
  )
}

// ==================== 待批登记簿 ====================
const pendingPerms = new Map<
  string,
  { sessionID: string; requestID: string; action: string; resources: string[]; at: number; cardSent?: number; resKey: string }
>()
const resIndex = new Map<string, string>() // 同会话同动作同资源 → 短编号(吸收 ask 重试风暴)
let shortSeq = 1000
const normRes = (arr: string[]) => arr.map((r) => r.replace(/\\/g, "/").toLowerCase()).join(" ")
function registerPending(
  sessionID: string,
  requestID: string,
  action: string,
  resources: string[],
): { shortId: string; dup: boolean } {
  for (const [sid, v] of pendingPerms)
    if (Date.now() - v.at > PENDING_PERM_TTL_MS) {
      resIndex.delete(v.resKey)
      pendingPerms.delete(sid)
    }
  for (const [sid, v] of pendingPerms) if (v.requestID === requestID) return { shortId: sid, dup: true } // 已登记
  // 同一会话 retry 循环会换新 requestID 重问同一资源: 复用编号并把映射指向最新请求, 旧卡按钮仍然有效
  const resKey = `${sessionID}|${action}|${normRes(resources)}`
  const known = resIndex.get(resKey)
  if (known && pendingPerms.has(known)) {
    const entry = pendingPerms.get(known)!
    entry.requestID = requestID
    entry.action = action
    entry.resources = resources
    entry.at = Date.now()
    return { shortId: known, dup: true }
  }
  const shortId = String(++shortSeq)
  pendingPerms.set(shortId, { sessionID, requestID, action, resources, at: Date.now(), resKey })
  resIndex.set(resKey, shortId)
  return { shortId, dup: false }
}
function findByData(data: string): { shortId: string; want: string; index?: number } | undefined {
  const fm = /^forma_(\d+)_(\d+)$/.exec(data ?? "")
  if (fm) return { shortId: fm[1], want: "formans", index: Number(fm[2]) }
  const m = /^(permapp|permalways|permrej|permnoop)_(\d+)$/.exec(data ?? "")
  if (!m) return undefined
  return { shortId: m[2], want: m[1] }
}

/** 互动事件响应: PUT /interactions/{id} {code:0}, 告知 QQ 后台已收到点击(客户端 3s 超时) */
async function ackInteraction(cfg: QQConfig, interactionId: unknown) {
  const id = String(interactionId ?? "")
  if (!id) return
  try {
    const res = await fetchQQ(
      `${apiBase(cfg)}/interactions/${encodeURIComponent(id)}`,
      {
        method: "PUT",
        headers: { "content-type": "application/json", authorization: `QQBot ${await getAccessToken(cfg)}` },
        body: JSON.stringify({ code: 0 }),
      },
      cfg.proxy,
    )
    if (cfg.debug) log("互动响应", res.status, res.ok ? "ok" : (await res.text().catch(() => "")).slice(0, 120))
  } catch (e) {
    log("互动响应失败", (e as Error).message)
  }
}

// ==================== 会话缓存与状态 ====================
const sessions = new Map<string, SessionInfo>()
const busySince = new Map<string, number>()
const idleTimers = new Map<string, ReturnType<typeof setTimeout>>()
const idleSeq = new Map<string, number>()
const completedAt = new Map<string, number>()
const permTimers = new Map<string, ReturnType<typeof setTimeout>>()
const errorTombstones = new Map<string, number>()

function invalidateIdle(sessionID: string) {
  const t = idleTimers.get(sessionID)
  if (t) clearTimeout(t)
  idleTimers.delete(sessionID)
}
function markComplete(sessionID: string) {
  completedAt.set(sessionID, Date.now())
  for (const [sid, ts] of completedAt) if (Date.now() - ts > 60_000) completedAt.delete(sid)
}
function recentlyCompleted(sessionID: string) {
  const ts = completedAt.get(sessionID)
  return ts !== undefined && Date.now() - ts < COMPLETE_DUP_WINDOW_MS
}

// ---------- 模块级单例: 同一进程多个实例只允许一份订阅 ----------
// (运行时会因热重载/多加载路径创建多个实例, 每次都 subscribe 会重复推送)
const g = globalThis as any
if (!g.__qqNotifyState) g.__qqNotifyState = {}
const singleton = g.__qqNotifyState as {
  alive: boolean
  locations: Set<string>
  ws: any | null
  wsTag: number
  pendingReplies: Set<string>
}
// 历史版本留下的全局状态可能缺字段(曾导致 wsTag=NaN, 网关永远连不上), 这里补齐默认值
if (typeof singleton.alive !== "boolean") singleton.alive = false
if (!(singleton.locations instanceof Set)) singleton.locations = new Set<string>()
if (typeof singleton.wsTag !== "number" || Number.isNaN(singleton.wsTag)) singleton.wsTag = 0
if (!(singleton.pendingReplies instanceof Set)) singleton.pendingReplies = new Set<string>()

// ---------- 消息模板 ----------
function fmtComplete(info: SessionInfo, sessionID: string, elapsed: number): string {
  const lines = ["✅ OpenCode 任务完成", `会话: ${info.title || sessionID}`]
  if (elapsed > 0)
    lines.push(`用时: ${elapsed < 60 ? `${Math.round(elapsed)}s` : `${Math.floor(elapsed / 60)}m${Math.round(elapsed % 60)}s`}`)
  return lines.join("\n")
}
const fmtSubagent = (info: SessionInfo, parent?: SessionInfo) =>
  ["🔁 子任务完成", `子会话: ${info.title || "-"}`, `父会话: ${parent?.title || "-"}`].join("\n")
const fmtError = (info: SessionInfo, message: string) =>
  ["⚠️ OpenCode 会话出错", `会话: ${info.title || "-"}`, message.slice(0, 500)].join("\n")
/** form(本 fork 的问答) 文本 */
const fmtForm = (info: SessionInfo, form: any) => {
  const lines = ["❓ OpenCode 提问", `会话: ${info.title || "-"}`]
  if (form?.title) lines.push(String(form.title).slice(0, 200))
  for (const f of form?.fields ?? []) {
    if (f?.hidden) continue
    lines.push(`${f.title || f.key || "-"}${f.description ? `: ${String(f.description).slice(0, 150)}` : ""}`)
    const opts: any[] = Array.isArray(f.options) ? f.options : []
    if (opts.length) lines.push(`选项: ${opts.slice(0, 8).map((o) => String(o.label ?? JSON.stringify(o.value))).join(" / ")}`)
  }
  return lines.join("\n").slice(0, 900)
}

/** 问答卡片: 首个可见字段选项≤4(或布尔)时给按钮作答, 否则返回 false 走纯文本 */
async function sendFormCard(
  cfg: QQConfig,
  shortId: string,
  info: SessionInfo,
  form: any,
  answers: { label: string; value: any }[],
): Promise<boolean> {
  if (!answers.length || answers.length > 4) return false
  const fields: any[] = (form?.fields ?? []).filter((f: any) => !f?.hidden)
  const f0 = fields[0]
  if (!f0) return false
  const md =
    `**❓ OpenCode 提问 #${shortId}**\n` +
    `> 会话: ${info.title || "-"}\n` +
    (form?.title ? `> ${String(form.title).slice(0, 120)}\n` : "") +
    `> ${String(f0.title || f0.key || "问题")}${f0.description ? `: ${String(f0.description).slice(0, 160)}` : ""}\n`
  const mkBtn = (id: string, label: string, data: string, style: number) => ({
    id,
    render_data: { label, visited_label: label, style },
    action: { type: 1, permission: { type: 2, specify_user_ids: [cfg.openid] }, data },
    group_id: `form_${shortId}`,
  })
  const rows = [
    { buttons: answers.map((a, i) => mkBtn(`f${i}`, a.label.slice(0, 12), `forma_${shortId}_${i}`, 1)) },
  ]
  const card = await postC2C(cfg, {
    msg_type: 2,
    markdown: { content: md.slice(0, 3400) },
    keyboard: { content: { rows } },
  })
  if (card.ok) {
    if (cfg.debug) log("问答卡发送成功 #", shortId)
    return true
  }
  if (cfg.debug) log("问答卡发送失败 #", shortId, card.status)
  return false
}

// ==================== 插件主体 ====================
export default {
  id: "qq-notify",
  async setup(ctx: any) {
    const locationDir = String(ctx.location?.directory ?? "")
    const fileOpts = readOptionFile()
    if (singleton.alive) {
      singleton.locations.add(norm(locationDir))
      writeLog(`多例登记 location=${locationDir} (主实例已在运行)`)
      return () => singleton.locations.delete(norm(locationDir))
    }
    singleton.alive = true
    singleton.locations.add(norm(locationDir))
    let cleanupRan = false
    const cleanupAll = () => {
      if (cleanupRan) return
      cleanupRan = true
      singleton.alive = false
      singleton.locations.delete(norm(locationDir))
      controller.abort()
      try {
        if (singleton.ws) {
          singleton.ws.ws?.close?.(1000)
          singleton.ws.timer && clearInterval(singleton.ws.timer)
          singleton.ws = null
        }
      } catch {}
      writeLog(`实例卸载 location=${locationDir}`)
    }

    const opts = { ...DEFAULTS, ...fileOpts, ...(ctx.options ?? {}) } as any as QQConfig
    if (process.env.QQ_BOT_APPID) opts.appId = process.env.QQ_BOT_APPID
    if (process.env.QQ_BOT_CLIENT_SECRET) opts.clientSecret = process.env.QQ_BOT_CLIENT_SECRET
    if (process.env.QQ_BOT_C2C_OPENID) opts.openid = process.env.QQ_BOT_C2C_OPENID
    if (process.env.QQ_GATEWAY_URL) opts.gatewayUrl = process.env.QQ_GATEWAY_URL
    if (process.env.QQ_GATEWAY_TOKEN) opts.gatewayToken = process.env.QQ_GATEWAY_TOKEN
    if (process.env.QQ_GATEWAY_SOURCE) opts.gatewaySource = process.env.QQ_GATEWAY_SOURCE
    const ev = { ...DEFAULTS.events, ...(fileOpts.events ?? {}), ...((ctx.options as any)?.events ?? {}) } as Required<
      NonNullable<QQConfig["events"]>
    >

    if (needsFill(opts.appId) || needsFill(opts.clientSecret))
      log("尚未配置 appId/clientSecret —— 在 ~/.config/opencode/gate-notify.jsonc(或环境变量)填入后生效。")
    if (needsFill(opts.openid)) log("尚未配置 C2C openid —— 同上。")
    const ready = !needsFill(opts.appId) && !needsFill(opts.clientSecret) && !needsFill(opts.openid)
    writeLog(
      `主实例 location=${locationDir} 选项键数=${Object.keys(fileOpts).length} ready=${ready} debug=${opts.debug}`,
    )

    const notifyText = (
      kind: "complete" | "error" | "permission" | "question",
      content: string,
      send?: (c: string) => Promise<boolean>,
    ) => {
      if (!ev[kind]) return Promise.resolve(false)
      if (!ready) {
        if (opts.debug) log(`(未填写凭据, 仅记录) ${kind}:`, content.replace(/\n/g, " | "))
        return Promise.resolve(false)
      }
      return (send ?? sendText)(opts, content)
    }

    async function infoOf(sessionID: string): Promise<SessionInfo> {
      let info = sessions.get(sessionID)
      const incomplete = !info || (!info.directory && !info.title)
      if (!info || incomplete) {
        try {
          const s = await ctx.session.get({ sessionID })
          info = {
            ...info,
            title: s?.title ?? info?.title,
            parentID: s?.parentID,
            directory: s?.location?.directory ?? s?.directory,
            model: s?.model ? { providerID: s.model.providerID, id: s.model.id } : info?.model,
          }
          sessions.set(sessionID, info)
        } catch (e) {
          if (opts.debug) log("session.get 失败", sessionID, (e as Error).message)
          info = { title: sessionID, ...(info ?? {}) }
        }
      }
      return info!
    }

    const gated = (info: SessionInfo) =>
      !!opts.onlyProjects?.length && !!info.directory && !opts.onlyProjects.some((q) => info.directory!.includes(q))

    function scheduleComplete(sessionID: string, seq: number) {
      idleSeq.set(sessionID, seq)
      invalidateIdle(sessionID)
      idleTimers.set(
        sessionID,
        setTimeout(
          () =>
            void fireComplete(sessionID, seq).catch((e: any) =>
              log("fireComplete 异常", e?.message ?? String(e), e?.stack?.split("\n")[1] ?? ""),
            ),
          idleDelayMs(opts),
        ),
      )
    }

    async function fireComplete(sessionID: string, seq: number) {
      if ((idleSeq.get(sessionID) ?? 0) !== seq) return
      if (recentlyCompleted(sessionID)) return
      idleTimers.delete(sessionID)
      idleSeq.set(sessionID, seq + 100)
      if (errorTombstones.get(sessionID)) {
        errorTombstones.delete(sessionID)
        return
      }
      markComplete(sessionID)
      const startedEl = busySince.get(sessionID)
      const elapsed = startedEl ? (Date.now() - startedEl) / 1000 : 0
      let info = await infoOf(sessionID)
      if (!info.title) {
        // 标题由运行时异步生成(rename), 稍等补取一次; unref 不拖进程退出
        await new Promise<void>((r) => {
          const t = setTimeout(r, 1500)
          try {
            t.unref?.()
          } catch {}
        })
        info = await infoOf(sessionID)
      }
      if (gated(info)) return
      if (info.parentID) {
        void infoOf(info.parentID).then((parent) => notifyText("complete", fmtSubagent(info, parent)))
        return
      }
      busySince.delete(sessionID)
      if ((opts.minDuration ?? 0) > 0 && elapsed < (opts.minDuration ?? 0)) return
      void notifyText("complete", fmtComplete(info, sessionID, elapsed))
    }

    const controller = new AbortController()
    void (async () => {
      try {
        writeLog("开始订阅事件流")
        if (!ctx.event?.subscribe) {
          log("ctx.event.subscribe 不存在 —— 运行时 ctx 形态与预期不符:", Object.keys(ctx ?? {}).join(", "))
          return
        }
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          try {
            const type = String((event as any).type)
            const p = (event as any).data ?? (event as any).properties ?? {}
            const relevant =
              type.startsWith("session.execution") ||
              type.startsWith("session.inbox") ||
              type.startsWith("session.idle") ||
              type.startsWith("session.status") ||
              type.startsWith("session.error") ||
              type.startsWith("permission") ||
              type.startsWith("question") ||
              type.startsWith("session.created") ||
              type.startsWith("session.updated") ||
              type.startsWith("session.renamed")
            if (!relevant) continue
            const evDir = norm((event as any).location?.directory)
            if (evDir && !singleton.locations.has(evDir)) {
              if (opts.debug) log("忽略其它位置事件", type, evDir)
              continue
            }
            switch (type) {
              case "session.created":
              case "session.updated": {
                const s = p.info ?? {}
                sessions.set(s.id, {
                  title: s.title,
                  parentID: s.parentID,
                  directory: s.directory,
                  model: s.model ? { providerID: s.model.providerID, id: s.model.id } : undefined,
                })
                break
              }
              case "session.renamed": {
                const cur = sessions.get(p.sessionID)
                sessions.set(p.sessionID, { ...cur, title: p.title ?? cur?.title })
                break
              }
              case "session.inbox.enqueued": {
                if (p.item?.type === "user" && !sessions.has(p.sessionID)) void infoOf(p.sessionID)
                break
              }
              case "session.execution.started": {
                invalidateIdle(p.sessionID)
                if (!busySince.has(p.sessionID)) busySince.set(p.sessionID, Date.now())
                break
              }
              case "session.status": {
                const st = p.status?.type
                if (st === "busy") {
                  invalidateIdle(p.sessionID)
                  if (!busySince.has(p.sessionID)) busySince.set(p.sessionID, Date.now())
                } else if (st === "idle") {
                  scheduleComplete(p.sessionID, (idleSeq.get(p.sessionID) ?? 0) + 1)
                }
                break
              }
              case "session.idle": {
                scheduleComplete(p.sessionID, (idleSeq.get(p.sessionID) ?? 0) + 1)
                break
              }
              case "session.execution.succeeded": {
                if (opts.debug) log("节点 complete", p.sessionID)
                scheduleComplete(p.sessionID ?? (event as any).sessionID ?? "", (idleSeq.get(p.sessionID ?? "") ?? 0) + 1)
                break
              }
              case "session.execution.interrupted": {
                invalidateIdle(p.sessionID)
                busySince.delete(p.sessionID)
                break
              }
              case "session.execution.failed": {
                if (opts.debug) log("节点 error", p.sessionID)
                tombstone(p.sessionID)
                invalidateIdle(p.sessionID)
                const msg =
                  (typeof p.error === "string" ? p.error : (p.error?.message ?? JSON.stringify(p.error ?? {}).slice(0, 200))) ||
                  "unknown"
                void notifyText("error", fmtError(await infoOf(p.sessionID), msg))
                break
              }
              case "session.error": {
                const sessionID = p.sessionID
                if (!sessionID) break
                const err = p.error
                if (err?.name === "MessageAbortedError" || String(err?.message ?? "").includes("abort")) break
                tombstone(sessionID)
                invalidateIdle(sessionID)
                const msg = (typeof err === "string" ? err : (err?.message ?? JSON.stringify(err ?? {}).slice(0, 200))) || "unknown"
                void notifyText("error", fmtError(await infoOf(sessionID), msg))
                break
              }
              case "permission.v2.asked":
              case "permission.asked": {
                if (opts.debug) log("节点 permission RAW", JSON.stringify(event).slice(0, 500))
                const requestID: string | undefined = p.id
                if (!requestID || permTimers.has(requestID)) break
                const action = p.action ?? p.permission ?? "-"
                const resources: string[] = p.resources ?? p.patterns ?? []
                if (opts.permissionSilence?.includes(String(action))) {
                  // 静音 = 不推 QQ, 但必须回复运行时消费掉 ask, 否则 ask 挂着:
                  // TUI 会话角标(感叹号)常亮 + 智能体每 ~33s 换 requestID 重试重问
                  const reply = (opts.permissionSilenceReply === "reject" ? "reject" : "once") as
                    | "once"
                    | "reject"
                  if (ctx.permission?.reply && requestID) {
                    const sid = p.sessionID
                    void Promise.resolve(
                      // 本 fork 的 reply payload 用 decision 键(Permission.Reply: once|always|reject)
                      ctx.permission.reply({ sessionID: sid, requestID, decision: reply }),
                    ).catch((e: any) => log("静音自动回复失败", action, requestID, (e as Error)?.message))
                    if (opts.debug) log("动作在静音列表, 自动" + (reply === "reject" ? "拒绝" : "批准"), action, requestID)
                  } else {
                    if (opts.debug) log("动作在静音列表, 运行时无 permission.reply, 仅忽略 ask", action)
                  }
                  break
                }
                const reg = registerPending(p.sessionID, requestID, String(action), resources.map(String))
                const shortId = reg.shortId
                const info = await infoOf(p.sessionID)
                if (gated(info)) break
                invalidateIdle(p.sessionID)
                const timer = setTimeout(() => {
                  permTimers.delete(requestID)
                  void (async () => {
                    let stillPending: boolean | undefined
                    try {
                      if (ctx.permission?.list) {
                        const list = await ctx.permission.list({ sessionID: p.sessionID })
                        if (opts.debug) log("permission.list 回核", JSON.stringify(list ?? []).slice(0, 400))
                        if (Array.isArray(list)) stillPending = list.some((r: any) => r?.id === requestID)
                      }
                    } catch (e) {
                      if (opts.debug) log("permission.list 失败(默认仍待批)", (e as Error).message)
                    }
                    if (stillPending === false) {
                      if (opts.debug) log("权限已被自动处理, 不推送", requestID)
                      singleton.pendingReplies.add(requestID)
                      return
                    }
                    if (!ev.permission) return
                    if (!ready) {
                      if (opts.debug) log("(未填写凭据, 仅记录) permission #", shortId)
                      return
                    }
                    void (async () => {
                      // 同资源 retry 重问: 编号复用且旧卡已发, 不再重推
                      const entry = pendingPerms.get(shortId)
                      if (reg.dup && entry?.cardSent) {
                        if (opts.debug) log("同资源重复 ask, 沿用卡片 #", shortId, "→", requestID)
                        return
                      }
                      const ok = await sendPermissionCard(opts, shortId, info, String(action), resources)
                      if (ok) {
                        const en = pendingPerms.get(shortId)
                        if (en) en.cardSent = Date.now()
                        if (opts.debug) log("审批卡已发 #", shortId)
                      }
                    })()
                  })()
                }, permissionWaitMs(opts))
                permTimers.set(requestID, timer)
                break
              }
              case "permission.v2.replied":
              case "permission.replied": {
                const t = permTimers.get(p.requestID)
                if (t) clearTimeout(t)
                permTimers.delete(p.requestID)
                break
              }
              case "form.created": {
                if (opts.debug) log("节点 form RAW", JSON.stringify(event).slice(0, 700))
                const form = p.form
                if (!form?.id || !form.sessionID) break
                const sessionID = String(form.sessionID)
                const info = await infoOf(sessionID)
                if (gated(info)) break
                // 可作答判定: 首个可见字段为 boolean 或带选项且 ≤4 个(Form.Value: string|number|boolean|string[])
                const fields: any[] = (form.fields ?? []).filter((f: any) => !f?.hidden)
                const f0 = fields[0]
                let answers: { label: string; value: any }[] = []
                if (f0?.type === "boolean") {
                  answers = [
                    { label: "是", value: true },
                    { label: "否", value: false },
                  ]
                } else if (Array.isArray(f0?.options) && f0.options.length && f0.options.every((o: any) => o && ["string", "number"].includes(typeof o.value))) {
                  answers = f0.options.slice(0, 4).map((o: any) => ({ label: String(o.label ?? JSON.stringify(o.value)), value: o.value }))
                }
                const shortId = registerForm(sessionID, String(form.id))
                const en = pendingForms.get(shortId)
                if (en) {
                  en.values = answers.map((a) => a.value)
                  en.at = Date.now()
                }
                void (async () => {
                  if (!ev.question) return
                  if (!ready) {
                    if (opts.debug) log("(未填写凭据, 仅记录) form:", fmtForm(info, form).replace(/\n/g, " | "))
                    return
                  }
                  const ok = answers.length ? await sendFormCard(opts, shortId, info, form, answers) : false
                  if (ok) return
                  const tail = fields.length > 1 || !answers.length ? "\n(多字段或自由输入, 请回 OpenCode 作答)" : ""
                  await notifyText("question", fmtForm(info, form) + tail)
                })()
                break
              }
              case "form.replied":
              case "form.cancelled": {
                for (const [sid, v] of pendingForms)
                  if (v.formID === p.id && v.sessionID === p.sessionID) pendingForms.delete(sid)
                break
              }
              case "session.deleted": {
                sessions.delete(p.sessionID)
                invalidateIdle(p.sessionID)
                break
              }
              default:
                break
            }
          } catch (e) {
            log("单条事件处理异常", (e as Error).message)
          }
        }
      } catch (e) {
        const name = String((e as Error)?.name ?? "")
        if (!name.includes("Abort")) log("事件流中断", (e as Error).message)
      }
    })()

    // ---------- QQ WebSocket: 按钮回调 → 审批 ----------
    pluginCtx = ctx
    void startGate(opts, ctx)

    return cleanupAll
  },
}

let pluginCtx: any = null

function tombstone(sessionID: string) {
  errorTombstones.set(sessionID, Date.now())
  for (const [sid, ts] of errorTombstones) if (Date.now() - ts > TOMBSTONE_TTL_MS) errorTombstones.delete(sid)
}

/** 统一审批执行: 按钮回调与文字指令共用 */
async function executeApproval(cfg: QQConfig, shortId: string, want: string) {
  const entry = pendingPerms.get(shortId)
  if (!entry) {
    void sendText(cfg, `❓ #${shortId} 已过期或不存在(登记仅保留10分钟)`)
    return
  }
  if (want === "permnoop") {
    void sendText(cfg, `🙈 已忽略 #${shortId}, 可稍后回 OpenCode 处理`)
    return
  }
  if (!pluginCtx?.permission?.reply) {
    void sendText(cfg, "⚠️ 当前运行时不支持远程审批(permission.reply 不可用), 请回 OpenCode 处理")
    return
  }
  const key = `${entry.requestID}:${want}`
  if (singleton.pendingReplies.has(key)) return
  singleton.pendingReplies.add(key)
  const reply = want === "permrej" ? "reject" : want === "permalways" ? "always" : "once"
  try {
    // 本 fork 的 reply payload 用 decision 键; 旧参数形态 {reply} 会报 Missing key at ["decision"]
    await pluginCtx.permission.reply({ sessionID: entry.sessionID, requestID: entry.requestID, decision: reply })
    pendingPerms.delete(shortId)
    if (entry.resKey) resIndex.delete(entry.resKey)
    void sendText(
      cfg,
      `${reply === "reject" ? "🚫 已拒绝" : reply === "always" ? "✅ 已批准(始终允许)" : "✅ 已批准(仅本次)"} #${shortId} · ${entry.action}`,
    )
    log("QQ 审批完成", shortId, reply, entry.requestID)
  } catch (e) {
    void sendText(cfg, `⚠️ 审批失败 #${shortId}: ${String((e as Error).message).slice(0, 160)}`)
    log("审批调用失败", shortId, (e as Error).message)
  } finally {
    singleton.pendingReplies.delete(key)
  }
}

/** 机器人 WebSocket 网关: 收按钮回调(INTERACTION_CREATE)+文字指令, 完成审批 */
async function startGate(cfg: QQConfig, ctx: any) {
  log("startGate 进入", `tag前=${singleton.wsTag}`, `ws存在=${!!singleton.ws}`)
  if (singleton.ws) {
    log("startGate: 已有网关实例, 跳过", !!singleton.ws.ws)
    return
  }
  if (needsFill(cfg.appId) || needsFill(cfg.clientSecret)) {
    log("startGate: 凭据未填, 跳过网关")
    return
  }
  if (cfg.cardInbound === false) {
    log("startGate: cardInbound=false, 跳过网关")
    return
  }
  singleton.wsTag += 1
  const tag = singleton.wsTag
  const state = { ws: null as WebSocket | null, timer: undefined as ReturnType<typeof setInterval> | undefined, lastSeq: 0 }
  singleton.ws = state
  const stale = () => tag !== singleton.wsTag

  const connect = async (): Promise<void> => {
    if (stale()) return
    try {
      const gwRes = await fetchQQ(
        `${apiBase(cfg)}/gateway`,
        { headers: { authorization: `QQBot ${await getAccessToken(cfg)}` } },
        cfg.proxy,
      )
      log("网关探测 HTTP", gwRes.status)
      const gwJson = (await gwRes.json()) as any
      const url = gwJson?.url
      if (!url || stale()) {
        if (!url) log("网关探测无 url:", JSON.stringify(gwJson).slice(0, 200))
        return
      }
      state.lastSeq = 0
      const ws = new WebSocket(url)
      state.ws = ws
      log("ws 已创建, 等待 hello")
      ws.onmessage = (ev) => {
        void (async () => {
          try {
            const m = JSON.parse(ev.data)
            if (m.s) state.lastSeq = m.s
            switch (m.op) {
              case 10: {
                const interval = m.d?.heartbeat_interval ?? 30000
                if (state.timer) clearInterval(state.timer)
                state.timer = setInterval(() => ws.send(JSON.stringify({ op: 1, d: state.lastSeq })), interval)
                const intents = (1 << 25) | (1 << 26) // GROUP_AND_C2C_EVENT | INTERACTION(按钮回调)
                ws.send(
                  JSON.stringify({
                    op: 2,
                    d: {
                      token: `QQBot ${await getAccessToken(cfg)}`,
                      intents,
                      shard: [0, 1],
                      properties: { $os: "linux", $browser: "opencode-qq-notify", $device: "opencode-qq-notify" },
                    },
                  }),
                )
                log("网关已连接, 等待按钮回调/文字指令")
                break
              }
              case 11:
                break
              case 0: {
                const m0 = m.d ?? {}
                if (cfg.debug) log("WS 事件", m.t)
                if (m.t === "INTERACTION_CREATE") {
                  const raw = JSON.stringify(m0)
                  if (cfg.debug) log("INTERACTION RAW", raw.slice(0, 600))
                  const hit = findByData(extractButtonData(m0))
                  // 先响应互动事件(客户端 3s 超时), 否则 QQ 界面会转圈报"请求第三方失败"
                  void ackInteraction(cfg, m0.id)
                  if (!hit) break
                  const who = m0.user_openid ?? m0.member?.user_openid ?? m0.group_member_openid
                  if (cfg.openid && who && who !== cfg.openid) {
                    void sendText(cfg, "⚠️ 仅登记的接收人可审批")
                    break
                  }
                  if (hit.want === "formans") {
                    await executeFormAnswer(cfg, hit.shortId, Number(hit.index ?? 0))
                  } else {
                    await executeApproval(cfg, hit.shortId, hit.want)
                  }
                } else if (m.t === "C2C_MESSAGE_CREATE" || m.t === "GROUP_AT_MESSAGE_CREATE") {
                  const who = m0.user_openid ?? m0.group_openid
                  const text = String(m0.content ?? "").trim()
                  if (who && who !== cfg.openid) break
                  const cmd = parseTextCommand(text)
                  if (cmd) await executeApproval(cfg, cmd.shortId, cmd.want)
                }
                break
              }
              default:
                break
            }
          } catch (e) {
            log("WS 处理异常", (e as Error).message)
          }
        })()
      }
      ws.onclose = (e: any) => {
        if (state.timer) clearInterval(state.timer)
        if (state.ws === ws) state.ws = null
        if (stale()) return
        log("网关关闭, 5s 后重连 code=", e?.code)
        setTimeout(() => void connect(), 5000)
      }
      ws.onerror = () => {}
      return
    } catch (e) {
      log("网关连接失败", (e as Error).message, "稍后重试")
      setTimeout(() => void connect(), 15000)
    }
  }
  void connect()
}

/** 从按钮回调 payload 的任意层级里找 perm* 数据 / 解析文字指令 */
function extractButtonData(obj: any, depth = 0): string {
  if (obj == null || depth > 4) return ""
  if (typeof obj === "string" && /^(perm|forma)/i.test(obj)) return obj
  if (Array.isArray(obj)) {
    for (const o of obj) {
      const r = extractButtonData(o, depth + 1)
      if (r) return r
    }
    return ""
  }
  if (typeof obj === "object") {
    for (const k of ["button_data", "data", "resolved"]) {
      if (k in obj && obj[k] != null) {
        const r = extractButtonData(obj[k], depth + 1)
        if (r) return r
      }
    }
    for (const v of Object.values(obj)) {
      const r = extractButtonData(v, depth + 1)
      if (r) return r
    }
  }
  return ""
}

function parseTextCommand(text: string): { shortId: string; want: string } | undefined {
  const m = /^(?:#|审批|批准|拒绝)?\s*#?(\d{4})(?:\s*(批准|拒绝|approve|reject|always|忽略|noop))?\s*$/i.exec(text)
  if (!m) return undefined
  const shortId = m[1]
  const verb = (m[2] ?? "批准").toLowerCase()
  const want =
    verb.includes("拒") || verb === "reject" ? "permrej" : verb.includes("忽") || verb === "noop" ? "permnoop" : verb === "always" ? "permalways" : "permapp"
  return { shortId, want }
}

// ==================== 待答 form 登记(本 fork 的问答=form) ====================
const pendingForms = new Map<string, { sessionID: string; formID: string; values: any[]; at: number }>()
const FORM_TTL_MS = 30 * 60_000
let formSeq = 3000

function registerForm(sessionID: string, formID: string): string {
  for (const [sid, v] of pendingForms) if (Date.now() - v.at > FORM_TTL_MS) pendingForms.delete(sid)
  for (const [sid, v] of pendingForms)
    if (v.sessionID === sessionID && v.formID === formID) {
      v.at = Date.now()
      return sid
    }
  const shortId = String(++formSeq)
  pendingForms.set(shortId, { sessionID, formID, values: [], at: Date.now() })
  return shortId
}

function findByFormData(data: string): { shortId: string; index: number } | undefined {
  const m = /^forma_(\d+)_(\d+)$/.exec(data ?? "")
  if (!m) return undefined
  return { shortId: m[1], index: Number(m[2]) }
}

/** QQ 卡片按钮作答: 提交第 index 个选项 */
async function executeFormAnswer(cfg: QQConfig, shortId: string, index: number) {
  const entry = pendingForms.get(shortId)
  if (!entry) {
    void sendText(cfg, `❓ #${shortId} 已过期或不存在(登记保留30分钟)`)
    return
  }
  const value = entry.values[index]
  if (value === undefined) {
    void sendText(cfg, `❓ #${shortId} 选项无效`)
    return
  }
  const key = `form:${entry.formID}:${index}`
  if (singleton.pendingReplies.has(key)) return
  singleton.pendingReplies.add(key)
  try {
    // payload 为 Form.Answer: 每个字段一个值, 单字段即 [value]
    await pluginCtx.session.form.reply({ sessionID: entry.sessionID, formID: entry.formID, answer: [value] })
    pendingForms.delete(shortId)
    void sendText(cfg, `✅ 已回答 #${shortId}: ${String(typeof value === "string" ? value : JSON.stringify(value)).slice(0, 80)}`)
    log("QQ 答问完成", shortId, entry.formID)
  } catch (e) {
    void sendText(cfg, `⚠️ 回答失败 #${shortId}: ${String((e as Error).message).slice(0, 160)}`)
    log("答问调用失败", shortId, (e as Error).message)
  } finally {
    singleton.pendingReplies.delete(key)
  }
}


