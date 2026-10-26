# dsh-notifier-qq

DSH 的 QQ 官方机器人通知插件，是 [`dsh-notifier`](https://github.com/THEWOLFWALKER/dsh-notifier) 的 **QQ 专项裁剪版**。

只做两件事，把这两件事做实：

1. **出站**：把「任务完成 / 需要批准 / 远程提问」等事件推到你的 QQ（官方机器人 v2 接口，单聊或群）。
2. **入站**：用同一个机器人账号开 WebSocket 网关，接收你的回复（编号裁决、`/pair` 配对、会话路由）。

其余 27 个渠道（Telegram / 钉钉 / 飞书 / WxPusher / PushPlus / Server酱 / Bark / Webhook / 企微应用 …）在本副本中全部移除。

---

## 相对原插件的两处实质改动

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

### 2. 直接沿用原插件的配置与身份

不想为了换个插件再把凭证抄一遍、把配对重做一遍。装配期按下面的优先级取值（**本插件显式配置永远最高**）：

| 优先级 | 来源 |
|---|---|
| 1 | 本插件自己的 cordis 配置行（`dsh-notifier-qq` 的 `config`） |
| 2 | 本插件自己的 state（`~/.dsh/dsh-notifier-qq/state.json`） |
| 3 | 原插件在同一 profile 的 `cordis.patch.yml` 里的 `- id: dsh-notifier` 行 |
| 4 | 原插件的 state（`~/.dsh/dsh-notifier/state.json`） |

**配置借用是字段级的**：原插件的 `qq-bot` 行作底，你写的同名字段覆盖它。所以「只想把 `apiBase` 换成联调地址、凭证照用原插件」也能成立。

借用的顶层块：`channels[qq-bot]`、`inbound.qq`、`approval`、`events`、`routing`、`segment`、`debounceMs`、`summaryMaxChars`、`titlePrefix`、`redaction`、`lang`。

**身份借用只搬四类持久键**（`inbound:bindings` owner 绑定、`inbound:pairing` 配对账本、`route:sessions` 会话路由、`aq:*` 待决提问），并且**目标键已存在就不动**，等价于一次性幂等播种。

**单向只读**：原插件的文件全程不被改动、不被写入。启动日志会明确告诉你借了什么：

```
[dsh-notifier-qq] 配置沿用原插件 dsh-notifier：channels[qq-bot].appId/appSecret/targetType/userId、inbound.qq、approval、events（本插件显式配置优先）
[dsh-notifier-qq] 已从原插件 dsh-notifier 借用持久状态 3 项（inbound:bindings、inbound:pairing、route:sessions）
```

---

## 安装

```bash
dsh plugin --profile web add file:/path/to/dsh-notifier-qq
```

或在 profile 的 `package.json` 里加入依赖与 bundle（本机已按此方式装好）：

```json
{
  "dependencies": { "dsh-notifier-qq": "file:D:/tool/deepseek/3/dsh-notifier-qq" },
  "dsh": { "profile": { "bundles": ["…", "dsh-notifier-qq"] } }
}
```

bundle 自带的 `cordis.patch.yml` 会插入默认行（`channels: []`）。**因为会自动沿用原插件配置，装完通常不用再写任何 YAML。**

要覆盖时，在 profile 的 `cordis.patch.yml` 里写：

```yaml
- id: dsh-notifier-qq
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

### 出站 `channels`（仅 `qq-bot`）

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | ✔ | 固定 `qq-bot` |
| `appId` | ✔ | QQ 开放平台开发者 ID（q.qq.com → 机器人开发设置） |
| `appSecret` | ✔ | 同页面 AppSecret |
| `targetType` | | `user`（单聊，默认）或 `group`（群聊） |
| `userId` | | 单聊目标 openid（`targetType: user` 时必填） |
| `groupId` | | 群 open id（`targetType: group` 时必填） |
| `markdown` | | 默认 `true`（`msg_type=2`）；`false` 回退纯文本（`msg_type=0`） |
| `rateMs` | | 发送间隔，默认 `1050`（QQ Bot 维度 60qpm ≈ 1 条/秒） |
| `timeoutMs` | | 单请求超时，默认 `10000` |

### 入站 `inbound.qq`

| 字段 | 说明 |
|---|---|
| `appId` / `appSecret` | 未填时自动沿用出站 `channels[qq-bot]` 的同名凭证 |
| `notifyUsers` / `notifyGroups` | 限定可交互目标（留空则用配对绑定） |
| `intents` | 默认 `GROUP_AND_C2C \| INTERACTION` |

### 其它顶层块

`approval`（`mode: answer` 远程可决 / `observe` 只旁观）、`events`（哪些事件推送）、`routing`（按 level 分流）、`segment`（长消息分段）、`questions`（远程提问）、`debounceMs`（合并窗口）等，语义与原插件一致。

---

## 事件自动推送与工具

- **事件推送**：`turnEnd`（完成/失败/中断，可按原因分控）、`approval`（需要批准）、`longRunning`/`stall`（长任务心跳与卡住提醒）等。默认 `turnEnd.completed` 开。
- **工具**：模型可主动调用 `notify`（发通知）与 `notify_test`（健康自检）。两者都受滑动窗口限流（默认 10 次/分钟），防止把渠道刷成垃圾出口。
- **远程审批与提问**：QQ 侧用编号回复裁决（`1`/`2`），按钮卡片经 `INTERACTION_CREATE` 回执。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 通知静默不到 | 未开通「单聊/群主动消息」权限 | q.qq.com → 机器人 → 开发设置，开启对应场景权限 |
| 日志 `qq-bot 未配置：…未填写` | 既没写配置也没借到原插件行 | 检查原插件 `- id: dsh-notifier` 行是否存在且未 `disabled: true` |
| 日志 `qq-bot 返回错误 <code>` | 凭证错 / 目标 openid 失效 / 被限流 | 按错误码到开放平台核对 |
| 只看到「换 token 失败」 | appId / appSecret 不匹配 | 核对开发设置页 |
| 工具报 `returned invalid output: value is not lossless JSON` | 已修复（0.13.1-qq.2）：旧版在未传 `channel` 时返回 `channel: undefined` | 升级到本版 |
| 日志 `未取到原插件 dsh-notifier 的配置` | 插件不在该 profile 的 `node_modules` 下，且无 `DSH_PROFILE_DIR` | 用 `dsh plugin add file:…` 装，或直接写 `channels` |

**验证是否真的修好**：用 `notify_test` 工具发一条，并在 QQ 侧确认实收。**不要只用管理台「测试通道」判断**——那条路径不经过冻结快照，原插件在它上面永远是绿的。

---

## 裁剪范围

- 模块：120 → 85（`config.mjs` 只留 `qq-bot` 注册表；入站 `INBOUND_CHANNELS` 只剩 `qq`；五个渠道适配器、五个入站 transport、扫码流、声明表引擎全部移除）。
- 无死代码：从 `src/plugin-entry.mjs` 出发可达性分析，85/85 全部可达。
- 运行时依赖：0（`yaml` 解析由自带的 `src/compat/mini-yaml.mjs` 完成，已与原 `js-yaml` 对真实 profile patch 逐字段比对一致）。

---

## 许可

MIT。核心逻辑来自 [`dsh-notifier`](https://github.com/THEWOLFWALKER/dsh-notifier)（MIT），第三方声明见 `THIRD_PARTY_NOTICES.md`。
