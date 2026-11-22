// qq-gw.mjs 统一网关模式冒烟测试（注入 mock fetch，零网络依赖）
// 验证：SSE 事件 → handleDispatch 信封（accountId=bot 字段）→ bus 回执经 raw_qq 网关发送
//       （HMAC v1 签名可复算、msg_id 提取、INTERACTION 回应跳过）
import assert from 'node:assert'
import { createHmac, createHash } from 'node:crypto'

const PLUGIN = '../src/inbound/qq-gw.mjs'
const { createQqInbound, resolveQqInboundConfig } = await import(PLUGIN)

const SECRET = 'smoke-test-secret'
const state = { envelopes: [], gwCalls: [], sseController: null, encoder: new TextEncoder() }

const fetchMock = async (url, init = {}) => {
  const u = String(url)
  if (u.includes('/stream')) {
    const stream = new ReadableStream({
      start(controller) {
        state.sseController = controller
        controller.enqueue(state.encoder.encode(': keepalive\n\n'))
      },
    })
    return new Response(stream, { status: 200, headers: { 'content-type': 'text/event-stream' } })
  }
  if (u.includes('/n/dsh')) {
    state.gwCalls.push({ url: u, headers: { ...init.headers }, body: JSON.parse(init.body) })
    return new Response(JSON.stringify({
      ok: true,
      results: { 'qq:me': { ok: true, code: 0, detail: 'msg_type=0 msg_id=MOCKMSG42' } },
    }), { status: 200, headers: { 'content-type': 'application/json' } })
  }
  return new Response('{}', { status: 404 })
}

// 1. resolveQqInboundConfig：网关模式不需要 appId/appSecret
const resolved = resolveQqInboundConfig({
  gatewayUrl: 'http://gw.test',
  gatewaySecret: SECRET,
  gatewayKeyId: 'dsh',
  gatewaySource: 'dsh',
  notifyUsers: ['1950B33TEST'],
})
assert.equal(resolved.ok, true, `网关配置应通过: ${resolved.reason ?? ''}`)
assert.equal(resolved.config.transport, 'gateway')
const directResolved = resolveQqInboundConfig({ appId: 'a', appSecret: 'b' })
assert.equal(directResolved.config.transport, 'direct', '直连模式保留')

// 2. createQqInbound + start → SSE 就绪
const bus = { accept: (env) => { state.envelopes.push(env); return { reply: '回执文本' } } }
const inbound = createQqInbound({ config: resolved.config, bus, fetchImpl: fetchMock })
inbound.start()
await new Promise((r) => setTimeout(r, 120))

// 3. 推一条 C2C 事件 → 信封 + 回执经网关 raw_qq 发送
const push = (obj) => state.sseController.enqueue(state.encoder.encode(`event: qq_event\ndata: ${JSON.stringify(obj)}\n\n`))
push({
  event: 'C2C_MESSAGE_CREATE',
  bot: '1999999999',
  data: { id: 'msgid_evt_1', content: 'hello gateway', author: { user_openid: '1950B33TEST' }, timestamp: '2026-11-22' },
})
await new Promise((r) => setTimeout(r, 150))

assert.equal(state.envelopes.length, 1, `应有 1 条信封: ${JSON.stringify(state.envelopes)}`)
const env = state.envelopes[0]
assert.equal(env.channel, 'qq')
assert.equal(env.userId, '1950B33TEST')
assert.equal(env.chatType, 'private')
assert.equal(env.text, 'hello gateway')
assert.equal(env.accountId, '1999999999', 'accountId 应取 SSE bot 字段')
assert.equal(state.gwCalls.length, 1, `回执应经网关发送: ${state.gwCalls.length}`)
const call = state.gwCalls[0]
assert.equal(call.url.includes('/n/dsh?wait=1'), true, `URL: ${call.url}`)
const raw = call.body.raw_qq
assert.deepEqual([raw.msg_type, raw.content, raw.msg_id], [0, '回执文本', 'msgid_evt_1'], 'raw_qq 应透传消息体+被动 msg_id')
assert.equal(typeof raw.msg_seq, 'number')

// 4. HMAC v1 签名复算（canonical: POST\nPATH\nTS\nNONCE\nSHA256(body)）
const h = call.headers
const canonical = ['POST', '/n/dsh', h['x-noti-timestamp'], h['x-noti-nonce'],
  createHash('sha256').update(JSON.stringify(call.body), 'utf8').digest('hex')].join('\n')
const expect = 'v1=' + createHmac('sha256', SECRET).update(canonical, 'utf8').digest('hex')
assert.equal(h['x-noti-signature'], expect, 'HMAC 签名应与网关算法一致')
assert.equal(h['x-noti-key-id'], 'dsh')
// msg_id 应从网关回执提取
assert.equal(raw.msg_seq !== undefined, true)

// 5. INTERACTION 事件：信封照常、无需本侧 ACK（无直连 PUT 调用）
push({
  event: 'INTERACTION_CREATE',
  bot: '1999999999',
  data: { id: 'i1', type: 11, user_openid: '1950B33TEST',
    data: { resolved: { button_data: 'not-a-contract' } } },
})
await new Promise((r) => setTimeout(r, 120))
assert.equal(state.envelopes.length, 1, '非契约按钮应静默忽略（不进 bus）')
assert.equal(state.gwCalls.length, 1, 'INTERACTION 不应产生任何直连/网关 ACK 调用')

console.log('SMOKE PASS: 网关模式 SSE→信封→raw_qq HMAC 发送→ACK 跳过 全链路通过')
