# noti-gateway

统一通知网关。**一个端口（默认 `10086`）同时接收 curl / HTTP / Webhook / WebSocket / 文件分片上行，并提供 SSE 事件订阅流**（出站回执 + QQ 入站消息/按钮回调），支持多个客户端并发长连接，按配置扇出到 **QQ 官方机器人（文本/markdown/ark/raw 透传/图片文件视频音频）/ 任意 Webhook / 出站 WebSocket 连接池**。

> 接入方式、连接协议细节、服务器部署与排错见 **[接入文档.md](接入文档.md)**。

## 为什么需要它

散落在各处的 WebSocket 推送会有四个真实问题：

1. **QQ 的主动消息限流是共享配额**（未认证 30 条/分钟，单关系 20 条/分钟，1000 条/群/天）。多个服务各自推送 = 互相抢配额，表现为"随机丢通知"。
2. **发送失败是静默的**：QQ 失败也返回 HTTP 200，错误在 body 的 `code` 里。只判断 HTTP 状态码就永远发现不了。
3. **多个 Gateway 连接**要各自配对 `shard` 和 `intents`，配错静默漏收；断网后 N 个服务同时重连会触发限流。
4. **没有持久化和重试**：进程重启/部署时飞行中的消息全部丢失。

本网关把这些问题收口到一个进程里解决。

## 快速开始

```bash
cd noti-gateway
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 然后填 keys.conf 密钥表（HMAC v1）与 bots 段（app_id / client_secret / targets.user_openid）
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

## 调用方式

> **鉴权**：**HMAC v1 签名**为主（密钥表 `keys.conf` 实时读取，随时增删改即生效，
> 详见 `接入文档.md` §3）；共享 Token（`auth.token` + `allow_query_token`）作为兼容保留。
> 签名速记模板见接入文档 §3。

### curl（最常见）

```bash
# 异步：立刻返回 202，消息已落盘，网关后台慢慢发
body=$(printf '%s' "构建失败"); ts=$(date +%s); nonce=$(openssl rand -hex 8)
sig=$(printf 'POST\n/n/ci\n%s\n%s\n%s' "$ts" "$nonce" \
      "$(printf '%s' "$body" | sha256sum | awk '{print $1}')" \
      | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')
curl -X POST "http://IP:10086/n/ci" --data-binary "$body" -H "X-Source: ci" \
     -H "X-Noti-Signature: v1=$sig" -H "X-Noti-Timestamp: $ts" \
     -H "X-Noti-Nonce: $nonce" -H "X-Noti-Key-Id: zcode"

# 同步：?wait=1 等平台真实回执，拿到每个 target 的成败（签名同上，略）
# => {"ok":true,"results":{"qq:me":{"ok":true,"code":0,...}}}

# 兼容：token 走 query（配置 auth.token + allow_query_token 后可用）
curl "http://IP:10086/n/ci?token=<token>&level=error&title=标题" -d "正文"
```

### JSON / 表单 / 纯文本都吃

```bash
# 以下示例省略 HMAC 鉴权头（同上）

# JSON
curl -X POST "http://IP:10086/n/ci" -H "X-Source: ci" \
     -H "Content-Type: application/json" \
     -d '{"level":"error","title":"MySQL 主库宕机","body":"10.0.0.5:3306","tags":["prod"]}'

# 表单
curl -X POST "http://IP:10086/n/ci" \
     -d "title=磁盘将满&body=/dev/sda1 91%"

# 纯文本：第一行当标题，其余当正文
curl -X POST "http://IP:10086/n/ci" \
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

`GET /stream` 以 `text/event-stream` 实时推送事件，`&types=`（逗号分隔）与 `&source=` 过滤，浏览器 `EventSource` 原生可用：

| types | 内容 |
|---|---|
| `submitted / receipt / dead / suppressed` | 出站投递流水（入队/回执/死信/抑制） |
| `reminder` | 定时提醒 fired / missed / error |
| `qq_event` | **QQ 入站事件**：C2C 消息、群 @ 消息、按钮回调 INTERACTION_CREATE（回调回应由网关自动完成，QQ 客户端不转圈） |

`qq_event` 事件负载：`{"event": "INTERACTION_CREATE", "bot": "<appid>", "data": {原始 dispatch}}`。监听类调用方订阅它即可实现"按钮点击 → 本地动作"的零直连闭环，完整示例见 `platform/zcode/qq-bot-listener.py`。

## 文件与消息投递（对接 QQ）

**接收入口**（多种形态，统一持久化后转换发送）：

| 入口 | 用法 | 适用 |
|---|---|---|
| 内联文件 | `/n/{route}` body 带 `file.b64`（请求体 ≤16MB） | 常规小文件 |
| 分片收件 | `/u/init` → `/u/{id}/part/{i}` → `/u/{id}/complete`，再 `file.ref` 引用发送 | 大文件（≤190MB） |
| multipart | `/n/{route}` 或 `/f/{src}` 以 `multipart/form-data` 传文件+字段 | curl -F / 现成工具 |
| 裸二进制 | `POST /f/{src}?name=a.zip&mime=...`，body 为原始字节；`&send=1&title=...` 直接构造成通知发送 | 脚本直传 |
| URL 代下载 | `file.url` 填 http(s) 地址，网关代下载后转存发送 | 对端只有下载链接 |

**发送到 QQ**：任意入口的文件统一转换成官方机器人可接受的通道——≤5MB 先走 sgroup `/files` 直传，失败自动降级 `upload_prepare → PUT 分片 → part_finish → /files 合并`（image/video/audio/file 全覆盖，`msg_type=7` 富媒体）。消息类型支持 `msg_type 0/2/3/4/7`：文本、markdown+keyboard 按钮卡、模板 markdown、ark、`raw_qq` 原始载荷透传。端点细节见 `接入文档.md` §2.6。

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
| QQ HTTP 200 但 `code != 0` | 按 code 判定 | 同上 |
| 通用 webhook `code/errcode != 0` | 按业务码判定 | 同上 |

`?wait=1` 的同步路径在超时时（默认 5s，`dispatch.sync_timeout`）仍返回已知的部分结果。

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

对标 nonebot_plugin_chatdownload。**本插件是纯订阅者**：WS 长连接由 `qqws` 插件独占，
chatdownload 订阅其广播的 `C2C_MESSAGE_CREATE` 事件（同一份 access_token 缓存），实时落盘：

| 产出 | 说明 |
|---|---|
| `quest/chat.txt` | 全部文本，一行一条（原始行，不分类、不滚动） |
| `quest/media/` | 图片/文件/视频即时下载到同一子目录（附件 URL 是临时 rkey，过期即失效，必须马上下载） |
| `quest/events.jsonl` | 原始事件流水（append-only），**先落盘后下载**——附件下载失败也不丢消息本体 |
| `quest/error.jsonl` | 下载失败记录（不重试，人工复查） |

- 配置见 `config.example.yaml` 的 `chatdownload` 一节；白名单 `target_users` 填 **user_openid**（不是 QQ 号），空 = 归档全部单聊。
- 归档器本身不回消息；白名单用户的文本消息在落盘后会进框架指令总线（前缀 `#`/`/`/`@`/`＠`），由注册了指令的插件（如 reminders）认领并经调度器投递回复。
- 归档器随 app 启停，状态见 `/_health` 与 `/_stats` 的 `chatdownload` 段；连接状态在 `qqws` 段（state/session/received/uptime）。

**硬限制（QQ 开放平台无对应 API）**：没有历史消息补抓——离线/重启期间消息永久丢失；没有合并转发展开（`get_forward_msg` 等价物不存在）。

## 插件机制

功能按插件组织在 `noti_gateway/plugins/`（一个功能一个模块，`_` 开头为内部助手），
**连接与业务分层**（机制借鉴 NoneBot / Koishi / AstrBot，全程无 AI）：

- **`qqws`** 独占 QQ 官方 WS 长连接（一个 appid 只有一条事件流，必须收口）：
  连接/心跳/重连/Resume，收到 dispatch 事件后广播给 `ctx.event_listeners`
  （C2C/群@消息与按钮回调同时经 hub 以 `qq_event` 广播给 SSE 订阅者，按钮回调的
  PUT /interactions 回应由网关自动完成），并登记 msg_id 供**被动回复**；
- **其他插件都是纯消费者**：chatdownload 订阅事件做归档；reminders 等用
  **声明式指令**做交互——彼此互不感知、可插拔；
- **声明式指令（学 Koishi）**：`ctx.command("提醒", hint="<时间> <内容>",
  usage=_HELP, handler=fn)`——按「前缀后第一个词」匹配，回复由框架统一发送；
  `#<指令> 帮助` 或空参数自动回 usage；**`#帮助` 总览由框架按已注册指令自动生成**，
  帮助文本只写一份；priority 控制同名冲突；
- **通知中间件（学 AstrBot pipeline）**：`ctx.notification_middleware` 在 submit 入队前
  依次改写/丢弃通知（agents 插件：静默时段、per-source 模板、来源统计）；
- **多轮会话（学 NoneBot got）**：`ctx.sessions.open(openid, handler)`——指令开启会话后，
  用户下一条消息（**无需前缀**）交给会话处理器，返回 `Pending("下一个问题")` 继续追问，
  「算了/取消」退出，5 分钟超时失效；
- **定时任务服务（学 NoneBot APScheduler）**：`ctx.schedule(name, fn, cron=... 或 interval=...)`，
  框架统一调度，插件不自己开循环；
- **被动回复（QQ 平台机制）**：收到用户消息后 4 分钟内的回复自动带 `msg_id/msg_seq`
  （同一条消息最多 5 条），**不占主动消息配额**；超窗自动降级主动消息；
- 插件元信息：模块暴露 `META = PluginMeta(name, desc, version)`，`#插件` 指令与
  `/_stats` 的 `plugins` 段展示；框架自带 `#状态`（运行摘要）与 `#插件` 指令；
- 插件不直连 QQ 发送 API：一律走 `Dispatcher.submit` 进持久化队列，复用路由/限流/重试/回执；
- 启用清单 `config.yaml` 的 `plugins`，缺省 `["qqws", "chatdownload", "reminders", "agents"]`
  （顺序即事件分发顺序：归档先于指令；webchat 面板为可选加载）。

## 网页消息面板（webchat 插件）

网页版 QQ 风格消息面板（迁移自 bison2 的 nonebot_plugin_webchat）：

- **实时查看** bot 收发的全部单聊消息：进站挂 `qqws` 事件广播，出站经
  `targets.register_send_observer` 记录全部 QQ 投递（告警/提醒/面板手动发送都能看到）；
  WebSocket 实时推送，每会话内存保留最近 N 条，jsonl 落盘重启恢复；
- **手动发送**：面板以 bot 身份发文本/图片/文件；被动回复自动带 `msg_id/msg_seq`
  （不占主动配额，窗口外自动降级主动）；媒体复用 targets 分块上传通道；
- **图片代理**：消息内 QQ CDN 图由服务端无 Referer 回源中转，不受 rkey 时效与防盗链影响；
- **安全**：TOTP 动态码登录签发 HMAC 会话令牌（重启不失效）、同源校验挡 CSRF、
  登录限速防爆破、图片域名白名单防 SSRF。

访问 `http://<host>:<port>/webchat/`，用验证器 App 的 TOTP 动态码登录。
`webchat.totp_secret` 留空时自动生成并持久到 `webchat/webchat_totp_secret.txt`（启动日志打印一次）；
面板数据目录 `webchat/`（messages.jsonl + 密钥文件）**已列入 .gitignore，不入库**。

```yaml
# config.yaml：plugins 清单加 "webchat"，再加如下段落
webchat:
  enabled: true
  totp_secret: ""          # 留空自动生成（绑定验证器用）
  session_days: 7          # 会话令牌有效期（天）
  max_per_chat: 300        # 每会话内存保留条数
  persist: true            # jsonl 落盘
  data_dir: "webchat"      # 落盘目录（相对运行目录）
```

限制：仅单聊（qqws 当前只订阅 C2C）；图片+说明文字拆成「图+文」两条消息；视频/音频按文件发送。

## 定时提醒（reminders 插件）

一次性 / 每天 / 每周(多天) / 工作日 / 周末 / 每月 / 每年 / 间隔 / cron 提醒，**手机 QQ 发指令（前缀 `#`，`/` `@` `＠` 等价）或 HTTP API 都行**：

```
#提醒 30分钟后 喝水                一次性（也认 半小时后 / 10s / 5m / 2小时 / 3天 / in 5min）
#提醒 18:00 下班打卡               今天没过用今天，过了自动算明天
#提醒 下午3点 起来活动             时段限定词（凌晨/早上/上午/中午/下午/傍晚/晚上/深夜…；也认 九点十五 / 9点15 / 9点半）
#提醒 明天 09:00 起床              也认 大后天 / 下周一 / 下下周三
#提醒 周三 09:00 交周报            下一个周三（也认 星期五 18:00）
#提醒 2026-11-20 09:00 交报告      完整日期，24 小时制
#提醒 26年11月15日 09:00 交周报    中文日期（两位年）；省略年份写 11月15日 09:00
#提醒 1115 09:00 交周报            紧凑日期（MMDD；261115=YYMMDD、20261115=YYYYMMDD）
#提醒 每天 09:00 吃药              每日重复
#提醒 每周一三五 10:00 站会        每周多天（也认 每周一至周五 / 每周六、日）
#提醒 周末 10:00 大扫除            周六+周日
#提醒 工作日 09:00 站会            周一至周五
#提醒 每月1号 09:00 交房租         每月重复（也认 每月1、15号 / 每月最后一天）
#提醒 每年11月20日 09:00 续费      每年重复
#提醒 每30分钟 喝水                间隔重复（也认 每小时 / 每小时30分 / 每隔2小时 / 每3天 09:00 / 每季度 / 每半年）
#提醒 0 9 * * * 背单词             cron（分 时 日 月 周，日字段支持 L=月末）
#提醒 列表 ｜ #提醒 取消 12 ｜ #提醒 帮助
```

`#帮助`（help/? 同义）是**框架级指令总览**，由框架按已注册指令自动生成（含框架自带的 `#状态`/`#插件`）；`#<指令名> 帮助` 或空参数回该指令的用法详情。

每条命令都会收到确认回复（含解析出的规则与下次触发时间，看一眼即可发现理解偏差）。

HTTP API（HMAC 鉴权同其它端点）：

```bash
POST   /schedule          {"title": "...", "body": "...", "when": "每天 09:00"}
                          when 语法同 QQ 命令；也可用 "at": ISO8601/epoch 或 "cron": "0 9 * * *"
GET    /schedule?state=active    列表（state: active|done|cancelled|missed|all）
DELETE /schedule/{id}            取消
```

投递语义：

- 到点经 `Dispatcher.submit` 进持久化队列，复用路由/限流/重试/回执；**崩溃最多重复一次、不丢提醒**（at-least-once）；
- 提醒是用户主动创建的离散事件，提交时绕过去重窗口（`dedup=False`）；
- **错过补发**：停机跨过触发点，重启后 `reminders.grace_seconds`（默认 900s）内补发并在正文注明原定时间；更久远的一次性提醒转 `missed`，重复提醒跳过错过的次数直接算下一次；
- 时间统一东八区，不依赖服务器系统时区；cron 为 5 段常用子集（`*/步长`、列表、区间、日与周「或」语义），由内置 120 行引擎解析，无新依赖；
- 事件进 SSE：`GET /stream?types=reminder` 可订阅 fired/missed/error。

## ZCode 钩子脚本（platform/zcode/）

本机 ZCode 钩子套件，全部只与网关对话（**零 QQ 直连**；密钥经环境变量注入，仓库副本为占位符）：

| 脚本 | 职责 |
|---|---|
| `qq-notify.sh` | 任务完成通知 / 权限确认卡（允许·拒绝回调按钮）/ 选择题远程代答（挂起等 QQ 应答） |
| `qq-bot-listener.py` | 常驻 SSE 监听（`/stream?types=qq_event`）：写按钮点击与消息文件供钩子轮询 |
| `qq-ask-once.py` | 一次性监听器：先等 SSE 就绪再发多题按钮卡（多选累加 / 自定义回答），收齐即退出 |
| `qq-send-file.sh` | 发文件：≤12MB 内联 b64，更大走 `/u/*` 分片，再经 `/n/zcode` 引用发送 |
| `qq-choice-parse.py` / `qq-choice-watch.py` | 选择题解析与双端并行监视（opt-in） |
| `hooks/` | 会话回合监视等 ZCode hook 机制脚本 |

接入协议、HMAC 签名速记与端点一览见 `接入文档.md`。

## 已知边界

- **单机单实例**设计。队列是本地 SQLite，多副本部署需要换 Redis。
- 关闭时会排空队列（`TimeoutStopSec` 要给够时间），但排空超时后未发完的会留在 SQLite 里，下次启动自动恢复。
- QQ **频道（子频道）** 场景官方要求机器人保持 WebSocket 在线。单聊收消息（chatdownload 归档）、群聊 @机器人 消息、按钮交互（INTERACTION 自动回应 + `qq_event` 广播）已接；频道消息仍未接。
- 鉴权为 HMAC 多密钥签名（`keys.conf` 实时生效）；来源 IP 白名单 `auth.ip_allow` 公网部署建议开启。

## 测试

```bash
python tests/selftest.py              # 全链路 25 项：鉴权/异步/同步/WS池/去重/路由/格式/回执
python tests/test_retry.py            # 重试与死信语义 9 项
python tests/test_connections.py      # 多客户端并发/心跳/批量/_wait/SSE 22 项
python tests/test_reminders.py        # 定时提醒 58 项：解析/cron/HTTP全链路/命令/错过补发
python tests/test_media.py            # 富媒体：内联/分片/双通道上传与降级
python tests/test_receive_variants.py # 接收形态 13 段：multipart/裸二进制/url 代下载/GBK 字段
python tests/test_qq_events.py        # qq_event：fake QQ WS → SSE 广播 + 交互自动回应
python tests/test_auth.py             # HMAC v1：canonical/防重放/多密钥
python tests/test_config_bots.py      # bots/targets 配置解析
python tests/test_dispatch_order.py   # 多 target 分发顺序与保序
```

全部自带 mock 服务，不需要真实 QQ 凭证、不需要联网。
