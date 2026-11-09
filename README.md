# noti-gateway

统一通知网关。**一个端口（默认 `10086`）同时接收 curl / HTTP / Webhook / WebSocket 上行，并提供 SSE 事件订阅流**，支持多个客户端并发长连接，按配置扇出到 **QQ 官方机器人 / 任意 Webhook / 出站 WebSocket 连接池**。

> 接入方式、连接协议细节、服务器部署与排错见 **[接入文档.md](接入文档.md)**。
> 配套 DSH 插件（把 DSH 的通知 / 审批 / 提问接到本网关）：**[dsh-notifier-gate](https://github.com/zlqd123/dsh-notifier-gate)**。
> 各客户端接入脚本与脱敏配置模板（ZCode / OpenCode / DSH / WorkBuddy）见 **[platform/](platform/)**。

## 为什么需要它

散落在各处的 WebSocket 推送会有四个真实问题：

1. **QQ 的主动消息限流是共享配额**（单聊主动：Bot 10 qps、单用户 20 条/分钟、1000 条/用户/天；群主动：60 条/分钟、1000 条/群/天；频道私信 2 条/人/天，[官方限额](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)）。多个服务各自推送 = 互相抢配额，表现为"随机丢通知"。
2. **发送失败是静默的**：QQ 失败也返回 HTTP 200，错误在 body 的 `code` 里。只判断 HTTP 状态码就永远发现不了。
3. **多个 Gateway 连接**要各自配对 `shard` 和 `intents`，配错静默漏收；断网后 N 个服务同时重连会触发限流。
4. **没有持久化和重试**：进程重启/部署时飞行中的消息全部丢失。

本网关把这些问题收口到一个进程里解决。

## 快速开始

```bash
cd noti-gateway
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 然后填 auth.token / qq.app_id / qq.client_secret / group_openid
python -m noti_gateway.server
```

等价于：

```bash
uvicorn "noti_gateway.server:get_app" --factory --host 0.0.0.0 --port 10086
```

systemd 部署：

```ini
[Unit]
Description=noti-gateway
After=network-online.target

[Service]
WorkingDirectory=/opt/noti-gateway
Environment=NOTI_CONFIG=/opt/noti-gateway/config.yaml
ExecStart=/usr/bin/python3 -m noti_gateway.server
Restart=always
RestartSec=5
# 停止时先排空队列
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

> 服务端部署注意：`host` 要绑 `0.0.0.0` 并在防火墙/安全组放行端口。只绑 `127.0.0.1`
> 时外部调用方全部连不通（表象是客户端「静默丢通知」，网关自己毫无异常）。

## 调用方式

### curl（最常见）

```bash
# 异步：立刻返回 202，消息已落盘，网关后台慢慢发
curl -X POST "http://IP:10086/n/ci" \
     -H "Authorization: Bearer <token>" \
     -H "X-Source: ci" \
     -d "构建失败"

# 同步：等平台真实回执，拿到每个 target 的成败
curl -X POST "http://IP:10086/n/ci?wait=1" \
     -H "Authorization: Bearer <token>" -H "X-Source: ci" \
     -d "构建失败"
# => {"ok":false,"results":{"qq:default":{"ok":false,"code":100001,...}}}

# token 走 query（不想改调用方脚本时）
curl "http://IP:10086/n/ci?token=<token>&level=error&title=标题" -d "正文"
```

### JSON / 表单 / 纯文本都吃

```bash
# JSON
curl -X POST "http://IP:10086/n/ci" -H "Authorization: Bearer <t>" -H "X-Source: ci" \
     -H "Content-Type: application/json" \
     -d '{"level":"error","title":"MySQL 主库宕机","body":"10.0.0.5:3306","tags":["prod"]}'

# 表单
curl -X POST "http://IP:10086/n/ci" -H "Authorization: Bearer <t>" \
     -d "title=磁盘将满&body=/dev/sda1 91%"

# 纯文本：第一行当标题，其余当正文
curl -X POST "http://IP:10086/n/ci" -H "Authorization: Bearer <t>" \
     -H "Content-Type: text/plain" --data-binary $'备份完成\n耗时 12m'
```

### Webhook 风格

```bash
curl -X POST "http://IP:10086/webhook/ci?token=<t>" \
     -H "X-Source: ci" -d '{"title":"CI failed"}'
```

### WebSocket 上行（多客户端并发）

```js
const ws = new WebSocket("ws://IP:10086/ws?token=<t>&source=ci");
ws.onopen = () => ws.send(JSON.stringify({ level: "error", title: "异常", body: "详情" }));
ws.onmessage = (e) => console.log(JSON.parse(e.data)); // {"event_id":"...","queued":true,...}
```

支持任意多客户端同时在线（全局/单 source 上限可配 `ingest.*`，超限关闭码 4429）；空闲时服务端发 `{"type":"ping"}` 心跳；JSON 数组一次批量提交；单条消息加 `"_wait": true` 可拿同步回执。详见接入文档。

### SSE 订阅流

`GET /stream?token=<t>` 以 `text/event-stream` 实时推送 `submitted / receipt / dead / suppressed` 事件，可用 `&source=` `&types=` 过滤，浏览器 `EventSource` 原生可用。

## 返回码含义

| 码 | 含义 |
|---|---|
| `202` | **已入队**。注意：这只代表"记下来了"，不代表平台已收到 |
| `200` | 同步模式（`wait=1`）拿到了各 target 的真实回执，看 `ok` 和 `results` |
| `401` | token 缺失/错误 |
| `403` | 来源 IP 不在白名单 |
| `404` | 没有路由匹配到（检查 `routes` 是否漏了 `source: "*"` 兜底） |
| `429` | 该来源超过 `per_source_per_minute` |

## 投递语义

**默认异步**：入队先落 SQLite 再返回 202，进程崩溃/重启不丢；后台 worker 按 target 独立消费、指数退避重试（默认最多 5 次），不可重试的错误直接进死信。

| 平台返回 | 判定 | 行为 |
|---|---|---|
| HTTP 429 / 5xx / 网络异常 | 可重试 | 退避重试 |
| QQ `code=100001`（频控） | 可重试 | 退避重试 |
| QQ `code=100007/100016/10004` | 不可重试 | 直接死信 |
| QQ 内容违规（`40034006` / `304061`） | 不可重试 | 直接死信，保留原文供人工改写 |
| QQ HTTP 200 但 `code != 0` | 按 code 判定 | 同上 |
| 通用 webhook `code/errcode != 0` | 按业务码判定 | 同上 |

`?wait=1` 的同步路径在超时时（默认 5s，`dispatch.sync_timeout`）仍返回已知的部分结果。

## QQ 消息审核与合规要点

官方没有逐条人工审消息，「审核」分三层（来源：[消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)、[运营规范](https://bot.q.qq.com/wiki/business/)、[常见问题](https://bot.q.qq.com/wiki/bot_FAQ/)）：

**发送方向（机器人→用户）**：平台实时自动审，先审后发。

| 机制 | 表现 |
|---|---|
| 内容安全自动审 | 违规内容发送接口当场拒绝：`40034006 消息内容违规`、`304061 消息内容无效` |
| 频道主动消息审核 | 异步 `MESSAGE_AUDIT` 事件（Intents 订阅）返回 MessageAudited 结果 |
| 用户侧开关 | 用户可关闭「允许主动发送」，关闭后主动消息一律失败（`40054013 用户拒收消息`） |
| 事后举报与处罚 | 用户可聊天界面举报机器人消息；AI 生成内容责任也在开发者，违规直至封搜索/封添加/下架/封号 |

对网关的含义：内容违规类错误（`40034006`/`304061`）重试无意义，落死信保留原文供改写；频控类才值得退避重试（见「投递语义」）。

**接收方向（用户→机器人）**：平台不二次审，合规责任在开发者。

- 运营规范 §5.1/§8 硬性要求自建 UGC 过滤机制（建议调用官方「内容安全检测接口」校验文本/图片），被投诉须及时响应。
- §11 数据安全：归档的用户数据不得外泄/挪用，用户要求即删，停止服务 24 小时内删除——chatdownload 归档适用同一条。

## 配置

见 `config.example.yaml`。要点：

- `routes` **按顺序匹配，第一条命中即生效**，务必把 `source: "*"` 放最后兜底。
- `source` 来自 **`X-Source` 头**（不是 body），配合共享 Token 用；不可信的调用方不要给它伪造 source 的机会，或用不同 Token 区分。
- `dedup` 按 `(source, level, title)` 在窗口内只发一次。**QQ 配额下这不是优化项而是必需项**。
- `limits.per_target_per_minute` 建议不超过 QQ 侧配额（未认证群 30/min）。
- 每个 target 可单独 `rate_per_minute` 覆盖。

## 观测

| 接口 | 说明 |
|---|---|
| `GET /_health` | 各 target 连通性（**免鉴权**） |
| `GET /_stats?token=` | 计数器 + 队列积压 + WS 连接状态 |
| `GET /_receipt/{event_id}?token=` | 单条通知各 target 的投递回执 |
| `GET /_failures?token=` | 死信列表（排查"到底丢没丢"） |

`/_stats` 返回的 `targets.<id>.connections` 会显示每条出站 WS 的 `state` / `sent` / `uptime_s`，可以确认连接池是否真的活着。

## 关于出站 WebSocket

`type: ws` 的 target 支持**同时维持多条连接**（`pool: N`），round-robin 分配，每条连接有独立的：

- 重连循环（指数退避 + jitter，多连接不会同时重连造成惊群）
- 应用层心跳（`ping_payload`，如 OneBot 可填 `{"action":"ping"}`）
- ack 等待（`ack_timeout`，用 `echo` 或 `status/retcode` 匹配回执）

**但先问一句：那条 WS 有人回话吗？** 如果只是"连上只管发"（你现在散落各处的就是这种），建议直接换成 `type: webhook` 或 QQ 原生 HTTP——更快、更稳、不用管心跳和重连。WS 唯一不可替代的价值是双向。

## 私聊消息归档（chatdownload）

对标 nonebot_plugin_chatdownload：**复用 `qq` 段同一个机器人**（同一份 access_token 缓存），WS 长连接收 `C2C_MESSAGE_CREATE` 单聊消息，实时落盘：

| 产出 | 说明 |
|---|---|
| `quest/chat.txt` | 全部文本，一行一条（原始行，不分类、不滚动） |
| `quest/media/` | 图片/文件/视频即时下载到同一子目录（附件 URL 是临时 rkey，过期即失效，必须马上下载） |
| `quest/events.jsonl` | 原始事件流水（append-only），**先落盘后下载**——附件下载失败也不丢消息本体 |
| `quest/error.jsonl` | 下载失败记录（不重试，人工复查） |

- 配置见 `config.example.yaml` 的 `chatdownload` 一节；白名单 `target_users` 填 **user_openid**（不是 QQ 号），空 = 归档全部单聊。
- 按需求裁剪：**不补历史、不做去重、不回消息**（收到消息不回复任何内容）。
- 归档器随 app 启停，状态见 `/_health` 与 `/_stats` 的 `chatdownload` 段（received/archived 计数可用于监控丢消息）。

**硬限制（QQ 开放平台无对应 API）**：没有历史消息补抓——离线/重启期间消息永久丢失；没有合并转发展开（`get_forward_msg` 等价物不存在）。

## 已知边界

- **单机单实例**设计。队列是本地 SQLite，多副本部署需要换 Redis。
- 关闭时会排空队列（`TimeoutStopSec` 要给够时间），但排空超时后未发完的会留在 SQLite 里，下次启动自动恢复。
- QQ **频道（子频道）** 场景官方要求机器人保持 WebSocket 在线。单聊收消息已由 chatdownload 实现（Gateway 收事件）；群聊 @机器人 消息、按钮交互、频道消息仍未接。
- 只有共享 Token 的单点防护。多人/多机接入建议换成按来源的 Token 表 + HMAC 签名。

## 测试

```bash
python tests/selftest.py          # 全链路 25 项：鉴权/异步/同步/WS池/去重/路由/格式/回执
python tests/test_retry.py        # 重试与死信语义 9 项
python tests/test_connections.py  # 多客户端并发/心跳/批量/_wait/SSE 22 项
```

三个测试都自带 mock 服务，不需要真实 QQ 凭证、不需要联网。
