// dsh-notifier-gate adapter: qq-bot（QQ 官方机器人，一律经 noti-gateway 投递）
// 出站唯一路径 = 统一通知网关（noti-gateway）：POST {gatewayUrl}/n/{source}，
// 汇入统一配额池（落盘+重试+死信+多工具共享）。
// 直连 QQ 官方 API（换 access_token → /v2/.../messages）的整条路径已移除：
// 不再有"网关失败静默回退直连"的第二条链路——网关失败即本条失败，行为可预期。
// 入站（WS 网关 + 编号回复）仍由 inbound/qq-gw.mjs 负责，与出站无关。

import { createHash, createHmac, randomBytes } from 'node:crypto'
import { postJson, str, num, NotifyError, ERROR_CODES } from './_shared.mjs'

export const type = 'qq-bot'

/** 校验并归一化配置。
 *  **一律走网关**：本副本只支持 noti-gateway 投递，不再有直连 QQ 官方 API 的路径。
 *  gatewayUrl + gatewayToken 是唯一必需项；appId/appSecret/userId 等直连凭据
 *  仅为兼容旧配置而保留在返回对象里（入站网关仍需 appId/appSecret），出站不再使用。
 *  gatewaySecret（可选）：HMAC 多密钥签名的密钥；填了即对每次请求签名（网关优先走 HMAC），
 *  不填则回退旧共享 token 通道（便于灰度，不硬破坏）。 */
export function resolve(cfg = {}) {
  const gatewayUrl = str(cfg.gatewayUrl).replace(/\/+$/, '')
  const gatewayToken = str(cfg.gatewayToken)
  const gatewaySecret = str(cfg.gatewaySecret)
  const gatewayKeyId = str(cfg.gatewayKeyId) || (gatewaySecret ? str(cfg.gatewaySource) || 'dsh' : '')
  const gatewaySource = str(cfg.gatewaySource) || 'dsh'
  const appId = str(cfg.appId)
  const appSecret = str(cfg.appSecret)
  const targetType = str(cfg.targetType) === 'user' ? 'user' : 'group'
  const targetId = str(targetType === 'user' ? cfg.userId : cfg.groupId)
  const missing = []
  // 仅网关模式：直连凭据不再参与出站校验（它们对入站网关仍然必要，由 inbound 侧校验）
  if (gatewayUrl === '') missing.push('gatewayUrl（noti-gateway 地址，如 http://host:10086）')
  if (gatewayToken === '' && gatewaySecret === '') missing.push('gatewaySecret（HMAC 密钥）或 gatewayToken（旧共享 token）二选一')
  if (missing.length > 0) {
    throw new NotifyError(`qq-bot 未配置：${missing.join('、')} 未填写（本插件出站一律走 noti-gateway）`, ERROR_CODES.NOT_CONFIGURED)
  }
  return {
    gatewayUrl,
    gatewayToken,
    gatewaySecret,
    gatewayKeyId,
    gatewaySource,
    // 直连凭据仅保留给「兼容旧配置读取」与入站，不再驱动出站投递
    appId,
    appSecret,
    targetType,
    targetId,
    // 30s：QQ 官方机器人实际投递延迟常达 9–18s，10s 会误报「投递超时，结果未知」
    //（消息多半已送达），故默认放宽到 30s；可用配置 timeoutMs 覆盖。
    timeoutMs: num(cfg.timeoutMs, 30000, 1000, 60000),
  }
}

/** HMAC v1 签名核心（确定性，便于跨语言对拍）：
 *  canonical = METHOD\nPATH\nTS\nNONCE\nBODY_SHA256_HEX
 *  X-Noti-Signature: v1=<HMAC_SHA256(secret, canonical) 的 hex>
 *  PATH 用请求路径（不含查询串），与网关侧 req.url.path 对齐。 */
export function signHeaders(secret, keyId, { method, path, body, ts, nonce }) {
  const bodyHex = createHash('sha256').update(body, 'utf8').digest('hex')
  const canonical = [method.toUpperCase(), path, String(ts), String(nonce), bodyHex].join('\n')
  const sig = 'v1=' + createHmac('sha256', secret).update(canonical, 'utf8').digest('hex')
  const h = {
    'x-noti-signature': sig,
    'x-noti-timestamp': String(ts),
    'x-noti-nonce': String(nonce),
    'x-noti-content-sha256': bodyHex,
  }
  if (keyId) h['x-noti-key-id'] = keyId
  return h
}

/** 供 postJson 的 sign 钩子：对【将被发送的 body 字符串】签名，保证与实际字节严格一致。 */
function signHmac(resolved) {
  return ({ url, method, body }) => {
    const path = new URL(url).pathname
    const ts = String(Math.floor(Date.now() / 1000))
    const nonce = randomBytes(16).toString('hex')
    return signHeaders(resolved.gatewaySecret, resolved.gatewayKeyId, { method, path, body, ts, nonce })
  }
}

/** 经统一网关（noti-gateway）投递纯推送：汇入统一配额池（落盘+重试+死信）。
 *  msg 可带 image/file 富媒体（{b64,mime?,name?} 内联 或 {ref} 引用 /u/* 分片上传件），
 *  原样透传给网关的 image/file 字段；纯媒体消息不硬造标题。 */
async function sendViaGateway(resolved, msg) {
  const media = {}
  if (msg.image) media.image = msg.image
  if (msg.file) media.file = msg.file
  const hasMedia = Boolean(media.image || media.file)
  const text = msg.title.length > 0 ? `${msg.title}\n${msg.content}` : msg.content
  const lines = text.split('\n')
  const title = (lines[0] ?? '').trim().slice(0, 100) || (hasMedia ? '' : '(无标题)')
  const body = lines.slice(1).join('\n').trim()
  try {
    await postJson(`${resolved.gatewayUrl}/n/${encodeURIComponent(resolved.gatewaySource)}`, {
      title,
      body,
      ...media,
    }, {
      headers: { authorization: `Bearer ${resolved.gatewayToken}`, 'x-source': resolved.gatewaySource },
      // 填了 HMAC 密钥即签名校验（网关优先走 HMAC）；否则回退旧 token 通道
      sign: resolved.gatewaySecret ? signHmac(resolved) : undefined,
      timeoutMs: resolved.timeoutMs,
      channel: 'qq-bot',
    })
    return true // postJson 非 2xx/网络异常会抛 NotifyError，能走到这里即投递成功（202 入队/200 同步）
  } catch (error) {
    try {
      console.error(`[dsh-notifier-gate:qq-bot] 网关投递失败: ${error instanceof Error ? error.message : String(error)}`)
    } catch { /* stderr 不可用不致命 */ }
    throw error instanceof Error ? error : new NotifyError('qq-bot 网关投递失败', ERROR_CODES.NETWORK_ERROR)
  }
}

/** 发送主动消息——**一律经统一网关**（noti-gateway）：汇入统一配额池（落盘+重试+死信）。
 *  直连 QQ 官方 API 的路径已整体移除：网关失败即本条失败（不再静默回退直连，
 *  避免"看起来发出去了、其实是另一条链路"的不可预期）。 */
export async function send(resolved, msg) {
  await sendViaGateway(resolved, msg)
}
