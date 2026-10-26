# Changelog

本副本基于 `dsh-notifier` 0.13.1（MIT）裁剪。上游完整变更史见
<https://github.com/THEWOLFWALKER/dsh-notifier/blob/main/CHANGELOG.md>。

## [0.13.1-qq.2]

修复「插件不通知」的第二个 P0：工具返回值含 `undefined` 被宿主判为非法 JSON。

### 修复

- **工具返回值非 lossless JSON 导致整个工具调用失败（P0）**：
  `notify` / `notify_test` 在「未配置任何渠道」分支原样返回 `channel: args.channel`。
  调用方省略 `channel` 时该值为 `undefined`，而宿主（`dsh-tools` 的 `snapshotToolValue`
  → `dsh-util-values` 的 `walkJsonValue`）对返回值做 lossless JSON 校验：**对象里只要
  存在一个值为 `undefined` 的键，整条调用即抛
  `ToolOutputError: tool "notify" returned invalid output: value is not lossless JSON`**。
  用户看到的是「工具坏了」，而不是「渠道未配置」。
  修法：返回值里所有可省略字符串经 `stringOf()` 归一为空串；失败文案经
  `errorTextOf()` 归一（Error → message，其余给「未知错误」兜底，不再泄露字符串
  `"undefined"`）。
- **借用失败时的静默**：原插件配置取不到时只打「未配置任何可用渠道」，用户无从判断
  是路径没定位到 profile。现在明确 warn 出 profile 路径与修复指引。
- **profile 目录推断加固**：`profileDir()` 增加「从插件自身安装位置反推」
  （`<profile>/node_modules/<pkg>/src/compat/…` 往上定位），不再单靠
  `DSH_PROFILE_DIR` / `DSH_PROFILE` / `DSH_HOME` 三个环境变量——宿主不保证把它们导出给
  插件运行时。

### 验证

- 用宿主真实校验器（`isJsonValue` + `validateJsonSchemaValue`）逐条验证 7 个返回分支
  （无渠道不传 channel / 无渠道传 channel / 限流 / 广播失败 / 单渠道失败 / notify_test
  失败 / 正常成功）：全部 lossless JSON、schema 违规为空。
- 端到端真实投递：`notify` 与 `notify_test` 经真实 QQ 接口各发一条，均返回
  `{"ok":true,"delivered":["qq-bot"],"failed":[]}`，QQ 侧实收。

## [0.13.1-qq.1]

dsh-notifier 的 QQ 专项裁剪版首个版本。

### 修复

- **出站推送必然失败（P0）**：`v0.12` 起出站配置的唯一运行时真值是 `OutboundSource`
  的深冻结快照，而 `adapters/qq-bot.mjs` 把 `resolved` 当可变缓存容器写
  `_tokenManager` / `_rateGate` / `_msgSeq`。严格模式下必抛
  `TypeError: Cannot add property _tokenManager, object is not extensible`，
  且发生在首次 HTTP 请求之前，导致 QQ 主动消息 100% 失败而日志只有一句笼统失败。
  管理台「测试通道」走 `health.mjs` 的 `adapter.resolve()` 新建可变对象路径，因此
  永远显示成功，掩盖了该缺陷。
  修法：运行态移入模块级 `WeakMap` 侧表，以 `resolved` 对象为键；保住「快照深冻结」
  与「`msg_seq` 全段成功才推进」两条既有契约。token 缓存在多次 `send` 间正常复用
  （不改成每次重建配置——那会在 60qpm 限速下迅速触顶）。
- **自定义 `apiBase` 时换 token 端点不可跟随**：`TOKEN_URL` 原为硬编码常量，
  `apiBase` 指向自建网关/本地联调时无法完整联调。现按 `apiBase` 同源推导
  （未设置或等于官方默认时行为不变）。

### 新增

- **沿用原插件配置与身份**（`src/compat/original-config.mjs`）：装配期按
  「本插件显式配置 → 本插件 state → 原插件 profile 配置行 → 原插件 state」
  优先级取值，字段级合并（原插件作底、显式字段覆盖）。
  借用范围：`channels[qq-bot]`、`inbound.qq`、`approval`、`events`、`routing`、
  `segment`、`debounceMs`、`summaryMaxChars`、`titlePrefix`、`redaction`、`lang`。
  持久身份借用仅四类键（`inbound:bindings` / `inbound:pairing` / `route:sessions` /
  `aq:*`），目标键已存在则不动（幂等播种）。**对原插件全程单向只读**。
- **零依赖 YAML 子集解析器**（`src/compat/mini-yaml.mjs`）：插件位置不保证能解析
  `yaml`/`js-yaml`，故自带解析器覆盖 profile patch 实际语法（块映射、块序列、引号
  标量、空 flow 集合 `{}`/`[]`、行尾注释、plain scalar 折行续写）。已对真实 profile
  patch 与原 `js-yaml` 输出逐字段比对一致。
- **入站凭证回退到出站**：未显式配置 `inbound.qq` 时沿用 `channels[qq-bot]` 的
  `appId`/`appSecret`（同一凭证既是出站也是网关凭证），免去重复填写。

### 变更（裁剪）

- 出站渠道 28 → 1：`config.mjs` 的 `ADAPTERS` 只注册 `qq-bot`；移除
  telegram/dingtalk/feishu/wxpusher/pushplus/serverchan/bark/webhook/bell/desktop/
  wecom-app 及 spec 声明表引擎。
- 入站通道 6 → 1：`INBOUND_CHANNELS` 只剩 `qq`；移除 telegram/feishu/wxpusher/
  wechat-ilink/dingtalk transport、网页扫码流（`admin/scan.mjs`，管理台
  `scanChannel` 按既有契约返回 501）。
- 包身份：`name` → `dsh-notifier-qq`；`stateDir` → `~/.dsh/dsh-notifier-qq`
  （绝不写原插件 state）；RPC 通道 `/dsh-notifier` → `/dsh-notifier-qq`；
  client bundle 侧栏面板与 slot key 同步改名，可与原插件并存不冲突。
- 模块数 120 → 85，从入口出发 85/85 可达（无死代码）。

### 兼容性

- `engines.dsh`: `>=0.1.7 <0.3.0`。原插件 `peerDependencies` 只声明
  `@deepseek-ai/dsh-session` 到 `0.1.7-rc.*`，在 dsh `0.2.1-rc.2` 上被判
  incompatible 而整个 bundle 被跳过（这正是原插件在本机完全没加载的原因之一）。
  本副本运行时不 import 任何宿主包，故改用 `engines.dsh` 范围声明。
- 零运行时依赖不变：`node:*` + 原生 `fetch` / `WebSocket`。

### 验证边界

- 已在本机 dsh `0.2.1-rc.2` 真实宿主启动：插件加载、QQ 入站网关就绪、
  `已启用渠道：qq-bot`、原生提问桥 attach。
- 出站链路以本地 mock 网关验证：冻结快照下连续两次发送成功、token 仅换取 1 次且
  复用、`msg_seq` 1→2 递增、`Authorization: QQBot <token>` 正确、冻结对象未被污染。
- **未做**：真实 QQ 平台主动消息实收验证（需在 q.qq.com 开通单聊/群主动消息权限）。
