// 独立发送测试脚本: 填好凭据后单独验证 QQ 官方机器人 C2C 推送是否打通
// 用法:
//   node send-test.mjs                                (从 opencode.jsonc 的插件 options 读凭据)
//   node send-test.mjs --appid xxx --secret yyy --openid zzz [--sandbox] [--debug]
//
// 注意: OpenCode 内部运行在 Bun, 支持 options.proxy; 本脚本用 Node 原生 fetch,
// 代理请用环境变量:  HTTPS_PROXY=http://127.0.0.1:7890 node send-test.mjs
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { homedir } from "node:os"

const args = process.argv.slice(2)
const arg = (k) => {
  const i = args.indexOf(`--${k}`)
  return i >= 0 ? args[i + 1] : undefined
}

// 简易 JSONC 解析(去 // 行注释)
function parseJsonc(text) {
  return JSON.parse(text.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n"))
}

let cfg = {}
try {
  // 优先读 qq-notify.jsonc(插件选项的真实位置); 回退 opencode.jsonc 的 plugins 选项
  cfg = parseJsonc(readFileSync(join(homedir(), ".config", "opencode", "qq-notify.jsonc"), "utf8"))
} catch {
  try {
    const rc = parseJsonc(readFileSync(join(homedir(), ".config", "opencode", "opencode.jsonc"), "utf8"))
    const entry = (rc.plugins ?? []).find((p) => typeof p === "object" && String(p.package).includes("qq-notify"))
    cfg = entry?.options ?? {}
  } catch (e) {
    console.error("读取配置失败:", e.message)
  }
}

const appId = arg("appid") ?? process.env.QQ_BOT_APPID ?? cfg.appId
const secret = arg("secret") ?? process.env.QQ_BOT_CLIENT_SECRET ?? cfg.clientSecret
const openid = arg("openid") ?? process.env.QQ_BOT_C2C_OPENID ?? cfg.openid
const sandbox = arg("sandbox") !== undefined || cfg.sandbox === true

if (!appId || String(appId).includes("在此填") || !secret || String(secret).includes("在此填") || !openid || String(openid).includes("在此填")) {
  console.error("缺少凭据。请在 ~/.config/opencode/opencode.jsonc 填好后再跑, 或用 --appid/--secret/--openid 传入。")
  process.exit(1)
}

const base = sandbox ? "https://sandbox.api.sgroup.qq.com" : "https://api.sgroup.qq.com"

async function main() {
  // 1. 换 access_token
  const tr = await fetch("https://bots.qq.com/app/getAppAccessToken", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ appId, clientSecret: secret, grantType: "client_credentials" }),
  })
  if (!tr.ok) {
    console.error(`换取 token 失败 HTTP ${tr.status}: ${await tr.text().catch(() => "")}`)
    process.exit(1)
  }
  const tok = await tr.json()
  const accessToken = tok.access_token ?? tok.accessToken
  if (!accessToken) {
    console.error("未拿到 accessToken:", JSON.stringify(tok))
    process.exit(1)
  }
  console.log("token 换取成功, expires_in =", tok.expires_in ?? tok.expiresIn)

  // 2. 发 C2C 测试消息(主动消息, 不带 msg_id, 受平台额度限制)
  const mr = await fetch(`${base}/v2/users/${openid}/messages`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `QQBot ${accessToken}` },
    body: JSON.stringify({
      content: "[OpenCode] qq-notify 测试消息: 收到即代表链路打通",
      msg_type: 0,
      msg_seq: 1,
    }),
  })
  const body = await mr.text().catch(() => "")
  console.log(`发送结果 HTTP ${mr.status} ${body}`)
  if (!mr.ok) {
    if (mr.status === 404 || body.includes("openid"))
      console.error("→ openid 可能有误, 或该用户与机器人之间没有会话记录(需要先在 QQ 里向机器人发过一条消息)")
    if (mr.status === 401) console.error("→ 鉴权失败: 检查 appId/clientSecret")
    if (mr.status === 400 || mr.status === 403)
      console.error("→ C2C 主动消息受额度/审核限制: 建议先在沙箱环境试, 或让用户先给机器人发一条消息再用被动回复方式")
    process.exit(1)
  }
  console.log("✅ 打通完成, 检查 QQ 是否收到消息")
}

main().catch((e) => {
  console.error("异常:", e.message)
  process.exit(1)
})
