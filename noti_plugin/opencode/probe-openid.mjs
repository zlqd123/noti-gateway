// 探针: 连上 QQ 官方机器人的 WebSocket 网关, 打印收到的所有事件的 openid
// 用途: 你在 QQ 里给机器人发一条消息(私聊/群里@它), 这里有响应就能拿到 openid
// 用法:
//   node probe-openid.mjs --appid xxx --secret yyy [--sandbox]
//   (凭据缺省从 ~/.config/opencode/opencode.jsonc 读取)
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { homedir } from "node:os"

const args = process.argv.slice(2)
const arg = (k) => {
  const i = args.indexOf(`--${k}`)
  return i >= 0 ? args[i + 1] : undefined
}
function parseJsonc(text) {
  return JSON.parse(text.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n"))
}
let cfg = {}
try {
  const rc = parseJsonc(readFileSync(join(homedir(), ".config", "opencode", "opencode.jsonc"), "utf8"))
  const entry = (rc.plugins ?? []).find((p) => typeof p === "object" && String(p.package).includes("qq-notify"))
  cfg = entry?.options ?? {}
} catch {}

const appId = arg("appid") ?? process.env.QQ_BOT_APPID ?? cfg.appId
const secret = arg("secret") ?? process.env.QQ_BOT_CLIENT_SECRET ?? cfg.clientSecret
const sandbox = arg("sandbox") !== undefined || cfg.sandbox === true
const base = sandbox ? "https://sandbox.api.sgroup.qq.com" : "https://api.sgroup.qq.com"

if (!appId || !secret) {
  console.error("缺少 appId/clientSecret")
  process.exit(1)
}

const tr = await fetch("https://bots.qq.com/app/getAppAccessToken", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ appId, clientSecret: secret, grantType: "client_credentials" }),
})
const tok = await tr.json()
if (!tok.accessToken) {
  console.error("换 token 失败:", JSON.stringify(tok))
  process.exit(1)
}

const gw = await fetch(`${base}/gateway`, { headers: { authorization: `QQBot ${tok.accessToken}` } })
const { url } = await gw.json()
if (!url) {
  console.error("获取网关失败 HTTP", gw.status)
  process.exit(1)
}
console.log("网关:", url, "(Ctrl+C 退出; 在 QQ 里给机器人发条消息即可看到 openid)")

// GROUP_AND_C2C_EVENT
const IDENTIFY_INTENTS = 1 << 25
let lastSeq = 0
let hbTimer = null
const ws = new WebSocket(url)

ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data)
  if (m.s) lastSeq = m.s
  switch (m.op) {
    case 10: {
      const interval = m.d.heartbeat_interval ?? 30000
      hbTimer = setInterval(() => ws.send(JSON.stringify({ op: 1, d: lastSeq })), interval)
      ws.send(
        JSON.stringify({
          op: 2,
          d: {
            token: `QQBot ${tok.accessToken}`,
            intents: IDENTIFY_INTENTS,
            shard: [0, 1],
            properties: { $os: "linux", $browser: "opencode-probe", $device: "opencode-probe" },
          },
        }),
      )
      break
    }
    case 11:
      break // 心跳 ACK
    case 0: {
      const d = m.d ?? {}
      const who =
        d.user_openid ? `user_openid=${d.user_openid}` : d.group_openid ? `group_openid=${d.group_openid}` : ""
      if (who) console.log(`[${m.t}] ${who} content=${JSON.stringify(d.content ?? "")}`)
      else console.log(`[${m.t}]`, JSON.stringify(d).slice(0, 300))
      break
    }
    case 9:
      console.error("鉴权失败(op9), 尝试 intents=0 重连…")
      break
    default:
      if (m.t) console.log(`op${m.op} ${m.t}`)
  }
}
ws.onclose = (e) => {
  clearInterval(hbTimer)
  console.error("连接关闭:", e.code)
  process.exit(e.code === 4010 ? 0 : 1)
}
ws.onopen = () => console.log("probe 已连接")
