# dsh-notifier-gate

DSH 的 QQ 官方机器人通知插件，是 [`dsh-notifier`](https://github.com/THEWOLFWALKER/dsh-notifier) 的 **QQ 专项裁剪版**。

只做两件事，把这两件事做实：

1. **出站**：把「任务完成 / 需要批准 / 远程提问」等事件推到你的 QQ（官方机器人 v2 接口，单聊或群）。
2. **入站**：用同一个机器人账号开 WebSocket 网关，接收你的回复（编号裁决、`/pair` 配对、会话路由）。

其余 27 个渠道（Telegram / 钉钉 / 飞书 / WxPusher / PushPlus / Server酱 / Bark / Webhook / 企微应用 …）在本副本中全部移除。

---

## 相对原插件的实质改动

### 1. 修复「QQ 推送必然失败」的 bug（本副本存在的首要原因）

**症状**：管理台点「测试通道」成功，但真实通知一条也收不到。

**根因**：`v0.12` 起出站配置的唯一运行时真值是 `OutboundSource` 的**深冻结**快照：

```js
// src/runtime/outbound-source.mjs
map.set(type, Object.freeze({ type, config: freezeDeep(clone(entry.config)) }))
```

`notify.mjs` 直接把这个已冻结的 `entry.config` 交给适配器：

```js
const sent = await sendOne(type, entry.config, normalized)
```

而适配器把 `resolved` 当作**可变缓存容器**往里挂运行态：

```js
resolved._tokenManager ??= createTokenManager(...)   // ← 严格模式下必抛
```

于是必抛：

```
TypeError: Cannot add property _tokenManager, object is not extensible
```

崩溃发生在第一个 HTTP 请求之前，所以「换 token 失败 / 权限不足」这类提示都看不到，只看到推送无声失败。

**为什么「测试通道」能通过**：`health.mjs` 走的是另一条路径——`adapter.resolve()` 现场新建一个**可变**对象再 `send()`。两条路径对同一份用户配置给出相反结论，这正是最难排查的地方。

**修法**：不再把运行态挂在 `resolved` 上，改用 `WeakMap` 侧表。

```js
const RUNSTATE = new WeakMap()
function runStateOf(resolved) {
  let state = RUNSTATE.get(resolved)
  if (state === undefined) {
    state = { tokenManager: undefined, rateGate: undefined, msgSeq: 0 }
    RUNSTATE.set(resolved, state)
  }
  return state
}
```

为什么不是「每次 send 现造一份配置」：`qq-bot` 的 token 缓存必须在多次 `send` 之间复用。每发一条都重新换 token，会在 QQ 的 60qpm 限速下迅速触顶。`WeakMap` 以 `resolved` 对象本身为键，配置被热替换后旧键自然被 GC 回收。

同时**保住了**原设计的两条契约：
- 快照依然深冻结（防消费方篡改）
- `msg_seq` 依然「全段成功才推进」（失败重试幂等，不重复投递）

顺带修了一个连带问题：`apiBase` 指向自建网关时，换 token 的端点也跟着 `apiBase` 走（原实现写死 `bots.qq.com`，导致自定义 `apiBase` 的配置无法完整联调）。

### 2. 配置完全自持（不读 dsh-notifier）

本插件的**配置唯一来源是自己的配置行**：profile 的 `cordis.patch.yml` 里
`- id: dsh-notifier-gate` 的 `config`（没写时用 bundle 自带 patch 的默认行，`channels: []`）。

它**不读取、也不借用** `dsh-notifier` 插件的任何配置或状态——两个插件完全独立：

| 项 | 来源 |
|---|---|
| 出站凭证 / 入站凭证 / 各顶层块 | 只有本插件自己的 `- id: dsh-notifier-gate` 配置行 |
| 持久身份（绑定 / 配对 / 会话路由） | 只有本插件自己的 state（`~/.dsh/dsh-notifier-qq/state.json`） |

`dsh-notifier` 插件即使装着、即使有配置行，对本站也不产生任何影响；反过来本站写 state
也只在 `dsh-notifier-qq` 目录。启动日志会明确告诉你配置来自哪里：

```
[dsh-notifier-qq] 配置来源：本插件配置行（channels: qq-bot；不再读取 dsh-notifier 插件的配置）
```

配置行里没有任何启用渠道时给出的是可操作的告警（而不是静默无操作）：

```
[dsh-notifier-qq] 本插件配置行里没有启用任何渠道（channels 为空或全被禁用）：…
```

> **从旧版升级**：早期版本会自动借用原插件配置，因此只装副本、不写 YAML 也能跑。
> 现在必须把凭证写进本插件自己的配置行（见下方「配置」一节）。如果之前靠借用跑起来，
> 升级后照抄一份到 `- id: dsh-notifier-gate` 即可；owner 绑定若只存在于原插件 state，
> 需用管理台重新配对，或手工把 `inbound:bindings` 等键复制进本插件 state。

> **改名残留说明**：包名已从 `dsh-notifier-qq` 改为 `dsh-notifier-gate`。凡是**按包名匹配**的标识都必须跟着改，否则表现为「装了但不加载」：
> package.json `name`、`cordis.patch.yml` 的 `id` 与 `insert.name`、`client.js` 里 `__ModuleLoader__.load({ id })`（前端按包名取模块，id 不对就是 `import failed`）、`plugins.bundle.config` / `plugins.bundle.activation` 插槽的 `key`（插件管理器按 `pkg.name` 匹配，key 不对配置页就是空白）。
>
> 仍沿用旧名、**故意不改**的运行时标识：默认 state 目录 `~/.dsh/dsh-notifier-qq/`、locale 命名空间。
> （曾与 `client.js` 成对的 host↔client RPC 通道 `/dsh-notifier-qq` 已随 control-surface
> 一并移除，见下方「精简（qq.18）」。改这些要两端一起改且会丢现有状态。）

---

## 安装

```bash
dsh plugin --profile web add file:/path/to/dsh-notifier-gate
```

或在 profile 的 `package.json` 里加入依赖与 bundle（本机已按此方式装好）：

```json
{
  "dependencies": { "dsh-notifier-gate": "file:~/dsh-notifier-gate" },
  "dsh": { "profile": { "bundles": ["…", "dsh-notifier-gate"] } }
}
```

> 包名、`cordis.patch.yml` 里的 `id` / `name`、以及 profile 覆盖行的 `id` 必须**四处同名**。
> loader 把 `insert.name` 当模块 specifier 去 `import()`，写成不存在的包名会让整条 bundle
> 在启动时被静默跳过——插件卡片显示「启动时没能加载」，`node_modules` 里却什么也不缺。

bundle 自带的 `cordis.patch.yml` 会插入默认行（`channels: []`）。**默认行没有任何渠道，装完必须自己写配置**——凭证不再从 `dsh-notifier` 借用。

在 profile 的 `cordis.patch.yml` 里写（config 是**整块替换**，bundle 默认行会被完全覆盖，所以这里要写全）：

```yaml
- id: dsh-notifier-gate
  config:
    channels:
      - type: qq-bot
        appId: "你的 appId"
        appSecret: "你的 appSecret"
        targetType: user          # user（单聊）| group（群）
        userId: "目标 openid"
    inbound:
      qq:
        appId: "同 appId"
        appSecret: "同 appSecret"
```

---

## 配置项

### 出站 `channels`（仅 `qq-bot`，一律经 noti-gateway）

出站**只有一条路径**：统一通知网关（[zlqd123/noti-gateway](https://github.com/zlqd123/noti-gateway)）。直连 QQ 官方 API 的实现已整体移除，
网关不可达即本条失败（不存在"静默回退直连"的第二条链路）。

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | ✔ | 固定 `qq-bot` |
| `gatewayUrl` | ✔ | noti-gateway 地址，如 `http://gateway.example:10086` |
| `gatewayToken` | ✔ | 网关 `auth.token`（Bearer 鉴权，脱敏） |
| `gatewaySource` | | 网关侧来源标识，默认 `dsh`，决定配额池归属 |
| `appId` | | QQ 开放平台开发者 ID——**仅入站网关使用**，出站不再需要 |
| `appSecret` | | 同页面 AppSecret——**仅入站网关使用** |
| `targetType` | | `user`（单聊，默认）或 `group`（群聊） |
| `userId` | | 单聊目标 openid（`targetType: user` 时） |
| `groupId` | | 群 open id（`targetType: group` 时） |
| `timeoutMs` | | 单请求超时，默认 `10000` |

### 入站 `inbound.qq`

| 字段 | 说明 |
|---|---|
| `appId` / `appSecret` | 未填时自动沿用出站 `channels[qq-bot]` 的同名凭证（入站 WS 网关必需） |
| `notifyUsers` / `notifyGroups` | 限定可交互目标（留空则用配对绑定） |
| `intents` | 默认 `GROUP_AND_C2C \| INTERACTION` |

### 其它顶层块

`approval`（`mode: answer` 远程可决 / `observe` 只旁观）、`events`（哪些事件推送）、`routing`（按 level 分流）、`segment`（长消息分段）、`questions`（远程提问）、`debounceMs`（合并窗口）等，语义与原插件一致。

---

## 事件自动推送与工具

- **事件推送**：`turnEnd`（完成/失败/中断，可按原因分控）、`approval`（需要批准）、`longRunning`/`stall`（长任务心跳与卡住提醒）等。默认 `turnEnd.completed` 开。
- **工具**：模型可主动调用 `notify`（发通知）与 `notify_test`（健康自检）。两者都受滑动窗口限流（默认 10 次/分钟），防止把渠道刷成垃圾出口。
- **远程审批与提问**：QQ 侧用编号回复裁决（`1`/`2`），按钮卡片经 `INTERACTION_CREATE` 回执。问答收尾回执**统一一条**：有卡片的问题只发 `[审批结果] …（来源 …）`（0.13.1-qq.15 起不再重复发裸 `✅ 已作答`；无卡片的编号兜底仍发裸回执）。

---

## 日志

控制台默认只打**故障**（error/warn），常规生命周期（启动、就绪、事件订阅注册、
网关会话轮转）只进宿主 logger，不刷终端。

| 环境变量 | 效果 |
|---|---|
| （不设） | 默认：`error`/`warn` 双写控制台；`info`/`debug` 仅 logger |
| `DSH_NOTIFIER_VERBOSE=1` | 连 `info` 也打到控制台（排障用） |
| `DSH_NOTIFIER_VERBOSE=0` | 完全关闭 stderr 双写，只走 logger |

> 曾经的做法是「所有 warn 都双写 stderr」——那是为了救 2026-08-16 一次真机事故
> （宿主 logger 不落 stdout，inbound 全死却零可见）。代价是终端被启动装配与
> 会话轮转刷屏。现在把「一定可见」的承诺只留给真故障。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 通知静默不到 | 未开通「单聊/群主动消息」权限 | q.qq.com → 机器人 → 开发设置，开启对应场景权限 |
| 日志 `本插件配置行里没有启用任何渠道` | 本插件自己的 `- id: dsh-notifier-gate` 配置行没有 `channels`，或行内渠道被 `enabled: false` | 在 profile 的 `cordis.patch.yml` 里为该行补 `channels` |
| 日志 `qq-bot 未配置：gatewayUrl / gatewayToken 未填写` | 出站网关没配全 | 在 `channels[qq-bot]` 补 `gatewayUrl` 与 `gatewayToken` |
| 日志 `网关投递失败` | noti-gateway 不可达 / token 错 | 核对 `gatewayUrl` 连通性与 `gatewayToken`；**不会回退直连** |
| 日志 `QQ 网关闭码 4009 / 服务端要求重连（op7）` | QQ 平台常规会话轮转，非故障 | 只首次打 warn，后续降 debug；如频繁且收不到消息再查机器人登录态 |
| 日志 `qq-bot 返回错误 <code>` | 凭证错 / 目标 openid 失效 / 被限流 | 按错误码到开放平台核对 |
| 日志 `qq-bot 返回错误 40034006` | 消息内容未过 QQ 平台内容安全审核（发送是先审后发） | 改写内容后重发；这类错误重试无意义 |
| 「✅ 任务完成」等事件推送收不到 | 出站唯一路径是网关：`gatewayUrl` 不可达（服务端只绑 `127.0.0.1` 时外网必失败）或 `gatewayToken` 失效 | `curl {gatewayUrl}/_health`；服务端绑 `0.0.0.0` 并放行防火墙/安全组，核对 token |
| 同一答案收到两条回执（裸 `✅` + `[审批结果]`） | 0.13.1-qq.14 及更早的双路径叠加 | 升级 0.13.1-qq.15：有卡片只发 `[审批结果]` 一条 |
| 只看到「换 token 失败」 | appId / appSecret 不匹配 | 核对开发设置页 |
| 工具报 `returned invalid output: value is not lossless JSON` | 已修复（0.13.1-qq.2）：旧版在未传 `channel` 时返回 `channel: undefined` | 升级到本版 |
| 插件卡片显示「启动时没能加载」 | `cordis.patch.yml` 的 `id`/`name` 与 package.json 包名不一致 | 三处同名（见「安装」） |

**验证是否真的修好**：用 `notify_test` 工具发一条，并在 QQ 侧确认实收。**不要只用管理台「测试通道」判断**——那条路径不经过冻结快照，原插件在它上面永远是绿的。

---

## 裁剪范围

- 模块：120 → 85（`config.mjs` 只留 `qq-bot` 注册表；入站 `INBOUND_CHANNELS` 只剩 `qq`；五个渠道适配器、五个入站 transport、扫码流、声明表引擎全部移除）。
- 精简（qq.18）：85 → 71（自建 Web 管理台 `src/admin/*` 与 Native Control Surface `src/control-surface/*` 整体移除；仅 `apply-mode` / `channel-config-migration` 两个被主链引用的模块迁至 `src/assembly/`。出站推送、入站审批/提问、路由、账本不受影响。管理台曾提供的静音配置改由 DSH 原生插件设置页承载，见「静音规则（插件页）」）。
- 无死代码：从 `src/plugin-entry.mjs` 出发可达性分析，全部可达。
- 运行时依赖：`@deepseek-ai/schemastery`（仅 `Config` schema 声明用，宿主 profile 已提供；其余逻辑 0 依赖——配置由宿主 cordis 解析后作为 `config` 传入；原先用于读原插件 patch 的 `src/compat/` 已随「配置自持」一并移除）。

---

## 静音规则（插件页可视化配置）

插件自带一个 DSH 原生插件页配置卡片（`plugins.bundle.config` / `plugins.row.config`），
自动读取当前 DSH 的工作区与会话列表，按项目/会话勾选「静音 / 放行 / 默认」。
数据面真值是 volatile 配置字段 `muteRules`，写进 profile 的 `cordis.patch.yml`：

```yaml
- id: dsh-notifier-gate
  config:
    muteRules:
      workspace2: true            # 项目（workspace basename）整体静音
      session-abc: false          # 精确会话显式放行（优先于项目）
      session-xyz: [qq-bot]       # 白名单：只推这些渠道
```

- 出站解析优先级：**muteRules（会话键 > 项目键）> route:sessions diff > 精确 agentId > workspace > 全局渠道池**。
  muteRules 命中即终止（source 标 `mute-rules`），未命中保持原链路，存量用户零感知。
- `muteRules` 为 `.volatile()`：设置页保存即热生效，不重启插件、不重建事件监听。
- 静音只拦自动推送；入站回复、审批、远程提问不受影响。

### 子智能体静音（events.subagent）

子智能体会话（`session.header.origin === 'subagent'`，判定只认 `origin`——
`parentSession` 存在与否不定性）的**一切出站推送默认静音**：turn/end 完成/失败、
心跳、卡住提醒、错误都不再推送，也不再写 route:sessions 台账。
显式打开才与普通会话同链处理：

```yaml
    events:
      subagent: true    # 默认关；打开后子会话事件照常推送（仍受 kinds/muteRules/routing 约束）
```

---

## 相关项目

- **[zlqd123/noti-gateway](https://github.com/zlqd123/noti-gateway)**——统一通知网关：本插件出站的唯一投递目标（`POST /n/{source}`），另提供 SSE 事件流、失败死信与私聊归档（chatdownload）。服务端部署与协议细节见其[接入文档](https://github.com/zlqd123/noti-gateway/blob/main/接入文档.md)。

---

## 许可

MIT。核心逻辑来自 [`dsh-notifier`](https://github.com/THEWOLFWALKER/dsh-notifier)（MIT），第三方声明见 `THIRD_PARTY_NOTICES.md`。
