## 0.13.1-qq.19（2026-11-22）

### 入站零直连：统一网关模式（transport='gateway'）

- `inbound.qq` 配置 `gatewayUrl + gatewaySecret（+gatewayKeyId/gatewaySource）` 即启用：
  事件源换成 noti-gateway 的 SSE（`/stream?types=qq_event`，HMAC v1 签名），
  发送（回执/审批卡/提问卡）全部改走 `/n/{source}` raw_qq 透传；
  QQ WS 协议层（HELLO/IDENTIFY/心跳/RESUME）与交互回应（PUT /interactions）
  由网关托管，本进程不再持有/使用任何 QQ 凭证。
- 直连模式（transport='direct'）原样保留：未配网关键时仍走 QQ WS 裸协议，行为不变。
- 信封 accountId：网关模式取 SSE 载荷的 bot 字段（回落 gatewaySource）；直连取 appId。
- 冒烟测试（mock fetch）：SSE 事件→信封→raw_qq HMAC 签名复算→msg_id 回执提取→
  INTERACTION 无直连 ACK，全链路通过；真实网关 SSE 订阅验证通过。

## [0.13.1-qq.18]

一次功能中立的重构：砍掉自建 Web 管理台与 Native Control Surface（约 -8,000 行），
项目/会话静音改由 DSH 原生插件设置页的 volatile 配置承载，并默认静音子智能体。

### 1. 精简：移除 admin web + control-surface

- 删除 src/admin/*（HTTP 壳/API/单文件 UI/SSE hub）与 src/control-surface/*
  （Native RPC 面、各 projection）。仅 apply-mode / channel-config-migration
  两个被入站主链/启动迁移引用的模块迁至 src/assembly/，调用点改路径。
- index.mjs 删除两块装配及散落钩子（eventHub / surfaceRevision / Activity /
  Health / outboundConfigService / launchTickets / adminSessions / RPC 挂载）；
  onSend 收敛为「账本 + sent 事件」两挂；inboundRuntimeOf 接线移除。
- 模块 85 -> 71，src 行数 24,409 -> 17,375；package.json 移除 ./client 旧导出，
  新增 dsh.client（自带新配置页）与 @deepseek-ai/schemastery 依赖。
- 出站推送、入站审批/提问、路由、账本、QQ 交互全部行为不变。

### 2. 静音配置迁移到 DSH 原生插件页（volatile 热更新）

- 新增 src/config-schema.mjs：Config 声明 muteRules（dict，值 = true | false |
  [渠道]）并标记 .volatile()；plugin-entry.mjs 导出 Config（DSH settings
  describe 据此暴露命名空间）。schemastery 对 object 未声明字段原样保留——
  既有 channels/events/inbound 配置零影响。
- 新增 src/mute-rules.mjs + event-listener.mjs 出站解析前置层：每次推送实时
  muteRules.get()（不解构不缓存，改完即生效），优先级 会话键 > 项目键 > 原路由链。
- 新增自带配置页 client.js：读宿主 ctx.workspaces / ctx.sessions 实时列表，
  项目行三态按钮（默认/静音/放行）+ 会话行复选框，写入走 SettingsFormModel
  staged Save；数据服务缺失时静默降级，绝不 park 面板。

### 3. 默认静音子智能体（events.subagent，默认关）

- 判定只认 session.header.origin === "subagent"（宿主规范：parentSession 不定性）。
- push() 入口总闸：子会话的 turn/end、心跳/卡住、错误全部静默，且不再
  registry.touch 建档（止住 route:sessions 台账被派生子任务灌满）。
- events.subagent: true 打开后子会话与主会话走完全相同的链。

### 验证

- 全量语法检查、161 处相对 import 解析、入口模块加载、mock-ctx apply 冒烟通过。
- 端到端：子/主会话各发 turn/end——默认仅主会话推送；subagent:true 两条都推。
- muteRules 四形态解析（true/false/[渠道]/缺省）与 volatile ref 实时读验证通过。

## [0.13.1-qq.17]

修复「网页问题卡片时有时无、总失效」——两个提问入口统一走原生 seam，网页与 QQ 双端同时出卡。

### 根因

网页问题卡片由宿主 `user-questions/request` 瀑布驱动（`dsh-client-ui-user-questions` 渲染），
而插件自有 `ask_user` 工具的 execute 直调 `bridge.askQuestions`（只走 aq 桥推 QQ 卡片），
**从不派发该瀑布**——它天然不出网页卡。网页卡片只在模型碰巧选官方 `ask_user_question` 时
才出现；更糟的是 `ask_user` 的工具说明写着「用户装了 dsh-notifier 手机桥接时优先用本工具
而不是 ask_user_question」，把模型系统性引向只发 QQ 的入口。于是「网页出不出卡」取决于
模型选了哪个工具：会话日志实证（11/9 以来 6 次提问）2 次官方入口网页出卡（含用户在网页
作答的 `aq:d7bd78c3`）、4 次插件入口网页无卡（含 2026-11-12 上午两次），表现为随机失效。

### 修复

- `ask_user` 改走宿主原生 seam（`ctx.userQuestions.askTimed/ask`）：问题经
  `user-questions/request` 瀑布下发，网页问题卡片与 QQ 卡片（native-questions 拦截器
  → aq 桥）同时出、先答为准；GUI 先答回灌（settleFromWeb）等既有机制全部复用
  （`src/questions/router.mjs` 新增 `askViaNativeSeam`）。
- 工具说明删去「优先用本工具而不是 ask_user_question」误导行，改为声明两入口出卡效果一致。
- `hostAsk` 透传 `context` / `timeoutMs`：QQ 卡片引言与 `askTimed` 前台等待共用同一时限
  （原生 `ask_user_question` 请求无此两字段，行为不变）。
- 兼容语义保留：seam 缺失或调用前失败退回 aq 桥（仅 QQ 出卡，行为同旧版）；超时不代答
  （`askTimed` 到期返回 answered=false，网页卡片仍留着可继续作答）；用户取消不重复出题；
  选项 `description` 保留（网页灰字提示、QQ 折进卡片上下文）。

### 验证

- 真实链路实测（2026-11-12 14:44，插件 `ask_user` 入口——即旧缺陷路径）：网页弹出问题
  卡片并作答，`state.json` 行 `aq:fabdbb1c` 落账 `decision:"answered"`, `via:"web"`，
  同时 QQ 卡片照常推送（`pushedTo` 含 qq 投递）——同一路径此前网页必无卡。

---

## [0.13.1-qq.16]

修复「GUI 先答时 QQ 卡片误标 ⏹ 已终止」——多端同步作答回归（QQ→宿主一直正常，GUI→QQ 断了）。

### 根因

`user-questions/request` waterfall 拦截器与 GUI answerer 双面抢答，GUI 先答时拦截器
`controller.abort()` 收尾本侧等待，而 askQuestions 的 onAbort 一律
`terminateDurable + bus.abandon`——账本行被终结成 `terminated`，已推 QQ 卡片改
「⏹ 已终止：agent 会话已结束，提问取消」，可实际上宿主拿到了 GUI 答案、会话正常
继续。事故行 `aq:c00eda90`（20:40:23 提问、20:41:31 terminated，68 秒远早于 5 分钟
超时）即该路径。DSH 核心代码未改动（全树无 2026-11-07 之后的修改），断点只在
插件侧 GUI→QQ 收尾维度。

### 修复

- GUI 胜出时把 Web 答案批次随 `abort` reason 传入（`{kind:'downstream-win', answers}`），
  onAbort 据此走新增 `settleFromWeb`：按「先答先算」把在途问题结算成 answered
  （via web）/skipped 并 `bus.settle` 唤醒 waitPromise——常规收尾把已推卡片改
  「✅ 已作答：…（来源 web）」/「⏭ 已跳过」，绝不误标终态
  （`src/questions/router.mjs` + `src/host/native-questions.mjs`）。
- 答案对位：payload 问题带 `id`（GUI 答案批次按 id 对位在途行，索引兜底）；选项 label
  按行选项映射下标，custom 走 aq-text（单选 custom 优先、多选两者一并呈现）；
  空答案 → skipped 语义（交还桌面、绝不编造），绝不伪装已作答。
- 首达不覆盖：行已有终态（QQ 同刻作答）时不改写，收尾仍按赢家终态呈现；结算直连
  settle/settleText 同款原语（ledger.resolve 先落账、bus.settle 后唤醒），不经
  `control.handle('question-answer')`（避免绕过授权语义与回环）。
- 其余取消（会话销毁/下游死亡/显式中止）终止语义不变；迟到投递收尾共享 finalTextOf，
  按真实终态呈现。

### 验证

- 真实链路实测（2026-11-10 07:58，Web GUI 先答）：`ask_user_question` 实测题在网页端选「同意」后，
  `state.json` 新行 `aq:8e74248d` 落账 `decision:"answered"`, `via:"web"`, `userId:"web"`,
  `answers:["同意"]`，作答后 28 秒内结账——非 5 分钟兜底、非 `terminated`，GUI→QQ 同步恢复。
  QQ 卡片按 `finalTextOf` 收尾为「✅ 已作答：同意（来源 web）」（同一收尾通道此前已实测编辑生效）。
- 回归对照：QQ 端先答（`via:"qq:button"` / `qq:text`）行为不变（`aq:6523281c`、`aq:d77e828f`）；
  旧缺陷样本 `aq:c00eda90`（GUI 先答误落 `terminated`）作为根因证据保留。

---

## [0.13.1-qq.15]

修复「同一答案收到两条回执」，并整理 README（QQ 消息审核机制、真实配额、网关部署坑位）。

### 根因

问答回执有两条独立发送路径叠加：questions 路由自己回一条裸 `✅ 已作答：…`
（按钮 / 编号 / 自定义三个入口各一处），卡片收尾 `markResolved → editTarget →
editResolved` 又发一条 `[审批结果] …（来源 …）`。有卡片的问题两条都发。

### 修复

- 三处裸回执改为条件发送：仅当 `ledger.get(qKey)?.pushedTo` 为空（无卡片投递、
  纯编号兜底）时才发；有卡片的问题统一由 `[审批结果] …（来源 …）` 一条收口
  （`src/questions/router.mjs` 按钮 / 编号 / 自定义三入口）。
- 错误路径回执不变；判级与提权卡片不受影响。

### 验证

卡片自定义作答只收到一条 `[审批结果] ✅ 已作答（自定义）：…`；
hint 编号兜底仍收到唯一裸回执。已实测。

---

## [0.13.1-qq.14]

修复「升级后所有入站消息被判不在白名单」。

### 根因

v0.13（C11.5 / R2）把身份 principal 从 `(channel, userId)` 升级为
`(channel, accountId, userId)`，非默认账号走 `<channel>:<accountId>:<userId>` 三元键；
适配器同时开始上报真实账号标识（QQ 传 `appId`）。

但**存量绑定行是升级前落的**，键是不带账号的 `qq:<userId>`，记录本身也没有 `accountId` 字段。
`allows()` 在 `accountId` 非默认时直接拼三元键，必然打 miss：

```
state 里:  qq:1950B333...              → 命中
查询时:    qq:<appId>:1950B333...   → miss → 判「不在白名单」
```

同一个键不匹配造成三处互不相干的表现，成因完全一致：

| 表现 | 位置 |
|---|---|
| `拒绝入站消息：qq user ... 不在白名单` | `bus.allows` |
| `/whoami` 把 owner 显示成 member | `commands.whoamiText` 自己按 `accountId ?? 'default'` 过滤 |
| `/unpair` 报 not-found / 绕过末位 owner 守卫 | `commands.handleUnpair` 同款过滤 + `removeBinding` 拼键 |

### 修复

- 新增 `resolveBindingKey()`：先精确命中 `(channel, accountId, userId)`，未命中且查询的是
  非默认账号时回落到渠道级键 `(channel, userId)`。回落**只放宽历史无账号记录**——带
  `accountId` 的行仍是账号级精确绑定，多账号场景不会串到别的账号。
- `allows` / `removeBinding` / `updateBinding` 统一走该解析；`addBinding` 的重复判定也走它，
  避免给同一个身份再插一条账号级重复行。
- 新增 `identity.find(channel, userId, accountId)`，`/whoami` 与 `/unpair` 改用它，
  不再各自复制一套（错误的）过滤逻辑。

### 验证

对真实 `state.json` 副本跑 13 项断言全 PASS（含多账号隔离：`bot-A` 的行不被 `bot-B` 命中）；
真实 bus 端到端 8 项全 PASS：入站消息被接受、业务扇出收到、`/whoami` 显示 owner、
陌生人仍被拒、非引导态、`/unpair` 末位 owner 守卫生效。

---

## [0.13.1-qq.13]

修复「QQ 按钮显示操作成功，实际没生效」。

### 根因（端到端复现）

`handleCardAction` 调 `deps.control.handle({...})` 时**漏传了 `trusted: true`**。
于是 `question-answer` 的注册 spec 走的是非 trusted 分支：

```js
settle: (input) => input.trusted === true
  ? settle(..., input.optIdxes, ...)            // ← 本该走这里
  : decide({ ..., optIdx: input.optIdx, ... })  // ← 实际走了这里
```

而按钮路径只传 `optIdxes: [optIdx]`、不传 `optIdx`，`decide` 里变成 `[undefined]`
→ `resolveIdxs` 返回 null → `invalid-option` → arbiter 判 `desktop_fallback`。

QQ 客户端此时早已收到互动 ACK（`PUT /interactions`），所以显示"操作成功"——
这正是「看着成功、实际没结算」的完整解释。

### 修复

- 按钮路径补回 `trusted: true`（授权/来源校验本就在 `handleCardAction` 上方完成：
  token 校验、pushedTo 精确匹配、spec.authorize）。
- 按钮路径成功回执改为从已结算账本行回读答案标签（与编号回复路径同款），
  不再把「已作答或已过期」当成功话术。

### 验证（真实生产链路端到端）

| 入口 | 修复前 | 修复后 |
|---|---|---|
| QQ 按钮点选 | `answered=false`、`invalid-option`、账本 timeout | **`answered=true`、`answers=["Approve"]`** |
| 编号回复「1」 | 正常 | 正常（未回归） |
| 自定义「答: xxx」 | 正常 | 正常（未回归） |

回执同步修正：按钮成功回执从「该提问已回答或已过期」变为「✅ 已作答：Approve」。

# Changelog

本副本基于 `dsh-notifier` 0.13.1（MIT）裁剪。上游完整变更史见
<https://github.com/THEWOLFWALKER/dsh-notifier/blob/main/CHANGELOG.md>。

## [0.13.1-qq.12]

修复「Web 计划审批点击后窗口消失、没生效」。

### 根因（真机复盘，2026-11-02 10:53 计划审批）

`native-questions.mjs` 的空答案兜底定时器（5 分钟）与插件内部提问超时（5 分钟）
**等长且同时触发**：超时空答案被 race 判为赢家交还宿主，宿主（plan-mode）据此判定
"keep planning" 并结束 `exit_plan_mode` 调用——而此刻 **Web 卡片还活着**。
用户随后点击 Approve 落在孤儿卡上：宿主 `ask()` 早已结算，点击只让卡片消失，
答案无人消费。账本实锤：`aq:1f4e17da` 行 `decision=timeout`、`via=null`——
Web 点击从未到达任何结算链路。

### 修复（`src/host/native-questions.mjs` race 语义重构）

- **「未作答不算作答」升级为「下游死亡驱动」**：Web 链路存活期间，手机侧超时空答案
  永不注入宿主；宿主保持等待 GUI 点击（plan-mode 的 `ask()` 本就无超时，语义正确）。
- **下游死亡信号**（`downstreamDied$`）：GUI answerer 真正 reject（headless
  NO_PROVIDER、GUI 异常、请求取消）时才放行手机侧结果（真答案或超时空答案）闭环宿主。
- **5 分钟「替用户决定」定时器 → 30 分钟防泄漏硬顶**：到点仅退出 race 等待
  `mobileAnswerRaw`（必然 settle），不再主动制造空答案抢在 GUI 前面。
- 分支修复：died/lease 分支改等 `mobileAnswerRaw`（必然 settle）；旧写法等的是
  已被转换成「永不 settle」的 promise，会挂死宿主。

### 验证（真桥集成测试，6 场景全过）

| 场景 | 赢家 |
|---|---|
| Web 先答 | Web ✔ |
| Web 悬置 + 手机已答 | 手机 ✔ |
| Web 悬置 + 手机超时 | 租约后空答案（不挂死）✔ |
| Web 拒绝(NO_PROVIDER) + 手机超时 | 空答案闭环 ✔ |
| Web 拒绝 + 手机已答 | 手机 ✔ |
| **事故形态：手机先超时 + Web 存活** | **Web 点击生效** ✔（修复前：空答案抢先，Web 点击落孤儿卡） |

测试桩接线注意：GUI 行为须经 handler 的 `next()` 注入（waterfall 下一环），
`ctx.waterfall` 不在这条链上。

## [0.13.1-qq.11]

按钮 label 上限 10 → **15 码点**（实测值）。

### 修复

- **选项在按钮上被提前截断**：`QQ_BUTTON_LABEL_MAX_CODEPOINTS` 原为 **10**，来自腾讯
  `dsh-qqbot` 参考实现的 `BUTTON_LABEL_MAX=10`，但平台真实上限是 **15**。
  2026-10-28 实测：向平台直发 10/15/20/30 字的 label（**未经任何本地截断**），
  平台均返回 200，客户端表现 **15 字完整显示、20 字起出现省略号**。
  故上限改为 15，11~15 字的选项不再被无谓截断。
- **按钮不再加「1. 」序号前缀**：15 码点额度有限，序号前缀会吃掉 3 个字符。
  编号改由卡片正文的 `**1.**` 清单承担（正文无长度限制），按钮与正文按顺序对应；
  点击回调本身按 `optIdx` 精确结算，不依赖按钮上的序号。

### 验证

截断边界（码点）：

| 输入 | 长度 | 结果 |
|---|---|---|
| `1 分钟后发一次（一次性验证）` | 15 | 完整 ✔ |
| `每 5 分钟一次（持续回归）` | 14 | 完整 ✔ |
| `一二三四五六七八九十一二三四五` | 15 | 完整 ✔ |
| `一二三四五六七八九十一二三四五六` | 16 | 截断为「…一二三四…」✔ |

- 实机发送对照卡片（走完整插件链路）确认按钮文案与平台行为一致。

### 说明

按钮上限是平台硬限，超过 15 字的选项**只能**靠卡片正文承载完整文案——正文那条路
（`**1.** 完整选项`）不受此限，两者配合使用。

## [0.13.1-qq.10]

会话标题取真实值：改从 `session/title` 事件折叠。

### 修复

上一版（qq.9）仍拿不到真实标题——`ctx.sessionTitle.get()` 在插件侧取不到，
而 `session.header.title` 这个回落字段**实际不存在**，所以只有测试桩里硬编码时才"成功"。

- **改为自行折叠会话日志**：取 `session.snapshotEvents()` 中**最后一条 `session/title`
  事件**的 `data.title`。这正是 DSH 标题服务内部的实现
  （`foldSessionTitle(session.snapshotEvents())`），但**不依赖服务注入**，
  宿主只要有会话日志就能拿到。本机实测正确取到当前会话标题 `gate`。
- 三级降级：标题服务 → `snapshotEvents` 折叠 → `header.title`；全失败则该行整段不加
  （不造假「(无标题)」，也不污染正文）。
- 标题清洗改为按码点过滤控制字符（原正则转义写法在多次落盘后易被误伤），
  多行标题压成单行。

### 验证

| 场景 | 结果 |
|---|---|
| 仅有 `session/title` 事件（无服务注入） | 取到 `gate` ✔ |
| 无 title 事件 / `snapshotEvents` 抛错 / 无该方法 | 保持原正文 ✔ |
| 标题含换行 | 压成 `第一行 第二行` ✔ |
| 多次改名 | 取最后一条（`新标题`）✔ |
| 标题为空串 | 保持原正文 ✔ |

- 真实投递经网关验证正文首行为会话标题。

## [0.13.1-qq.9]

会话标题取不到（inject 用法错误）+ 位置修正。

### 修复

- **标题完全没出现**：初版把 `sessionTitle` 写进 `inject`，而 cordis 的 inject 是硬依赖，
  且 `ctx.sessionTitle` 并非这样访问。改为**防御式** `ctx.get('sessionTitle', false)` 探测，
  且**不**写 inject——宿主无该服务时整个插件会拒绝加载（通知全哑）。
- **标题位置修正**：gateway 把「msg.title + 正文第一行」拆成消息标题 / 正文两个字段，
  因此最终呈现为：消息标题 = `✅ 任务完成`，正文第一行 = 会话标题，第二行空行，其后原有内容。
- 回归：宿主无标题服务、服务抛错时均保持原正文且通知照发（已验证）。

## [0.13.1-qq.8]

通知延迟、提问卡片三处体验问题，以及通知正文加会话标题。

### 修复

- **通知延迟「要等好几秒」（感知 10s → 1.5s）**：`turn/end` 走尾沿防抖，
  默认窗口 `debounceMs` 是 10000ms——每次任务完成都要等满合并窗口才推送。
  这是设计期取值而非需求。默认降到 **1500ms**：仍能合并「start→end 快速连发 /
  连续多 turn」的重复通知（实测连续 3 轮仍只发 1 条），但几乎无感知延迟。
  实测延迟 10000ms → **1515ms**。
- **提问卡片看不到选项全文**：QQ 按钮 `label` 平台上限只有 **10 码点**（腾讯官方限制，
  不可突破），超出即被截断成「1. 一次性验证…」。改为**完整选项写进卡片正文**
  （正文上限 3000 码点），按钮只留编号，用户能读全整句。
- **「自定义答案」是个死按钮**：原实现只回一句「请回复 答：xxx」就结束，用户得自己
  记住语法。现在点击后进入**待输入态**：下一条普通消息直接作为答案提交。
  显式 `答: xxx` 前缀仍保留，作为无卡片/降级场景的入口。
- **多端同步退化，只剩 QQ**：原生提问桥是「QQ 与 GUI 双面 race，先答先算」。
  但 QQ 侧**超时/未作答**时返回的空答案被当成了 race 赢家交给宿主——宿主据此认为
  「用户没选」，而此刻 Web 卡片还在等人作答，表现就是「多端同步没了」。
  现在**未作答不算作答**：本侧空答案主动让位，让 GUI 继续等待；同时加有界兜底定时器
  （默认 5 分钟），避免两端都无人作答时宿主 `ask()` 永久挂起。

### 新增

- **通知正文第二行显示会话标题**：结构为「第一行原有正文 / 第二行会话标题 /
  第三行空行 / 其后原有正文剩余部分」。标题取自 `ctx.sessionTitle.get(session)`
  （DSH 原生标题服务），失败回落 `session.header.title`，都取不到则整段不加
  （不凭空造「(无标题)」）。turn 完成、审批、长任务等通知统一生效。

### 仓库

- 新增 `.gitignore`（`node_modules`、本机 state、临时文件）。
- 文档中的真实网关地址与本地路径已替换为占位符。

## [0.13.1-qq.7]

面板返回按钮：修掉几个"进得去出不来"的页面。

### 修复

| 页面 | 旧行为 | 现在 |
|---|---|---|
| 渠道列表（`channels`） | **完全没有返回按钮**——从主页「管理渠道」进去后只能靠浏览器后退 | `← 返回` → 主页 |
| 设置流程（`setup`） | 只能退回"选择渠道"这一步，**无法放弃整个流程** | `← 返回` → 渠道列表 |
| 渠道详情加载态 | 只有标题 + "读取中…"，**没有返回**——拉取失败时彻底卡死 | `← 返回` → 渠道列表 |
| 渠道详情 / 任务 / 活动 | 有返回，但各自手写 `dn-detailBack`，样式与位置不一致 | 统一走 `PageHead.onBack` |

### 变更

- `PageHead` 新增 `onBack` / `t` 参数，**返回按钮成为页面头的统一能力**，
  不再各页手写——以后新增二级页只需传 `onBack`，不会再漏。
- 返回按钮带 `aria-label` 与 `title`（自绘的 `←` 对读屏可读），
  并补 `:hover` / `:focus-visible` 样式和 `cursor:pointer`。
- 主页（顶层）**故意不带**返回按钮。

### 验证

- 静态断言：7 个 `PageHead` 调用点中，仅顶层主页 `onBack=false`，其余 6 个全部有返回。
- 行为单测：执行 `PageHead` 源码，带 `onBack` 时产出 `<button class="dn-backButton"
  type="button" aria-label="返回">← 返回</button>`；不带时不产出。
- 可达性断言：5 个可导航视图（home/channels/channel/tasks/activity）逐一验证有退出路径。

## [0.13.1-qq.6]

控制台降噪。

### 变更

- **新增统一日志出口 `src/log.mjs`**：此前 9 个模块各自实现「logger + stderr 双写」的
  `warn()`，导致**每条 warn 都进控制台**。现在按三级分流：

  | 级别 | 去向 | 用于 |
  |---|---|---|
  | `error` | logger + stderr | 真故障，必须看见 |
  | `warn` | logger + stderr | 异常但已自愈/可忽略的降级 |
  | `info` | **仅 logger** | 常规生命周期（启动/就绪/订阅注册） |
  | `debug` | 仅 logger.debug | 高频诊断（心跳、轮转、@采样） |

  真故障仍然双写——「宁可多几行，不可部署黑盒」的底线只落在**故障可见**上，
  而不是「所有消息都可见」。
- **降级为 `info` 的常规日志**（不再刷控制台）：
  配置来源、渠道跳过、`inbound 已启动`、`远程提问已启用`、`出站就绪`、
  5 条 `宿主事件订阅已注册`、`QQ 网关已就绪`、`QQ 网关断线恢复`、
  `route:sessions 迁移`、`宿主原生提问桥 attach`。
- **state 权限自愈提示改为一次性 `console.info`**：原先每次启动都打
  「权限过宽（666，应为 600），尝试收紧」，而它随后就被自动修正了——已自愈状态
  不该占据告警位。收紧**失败**时仍 `console.error`。

### 新增开关

`DSH_NOTIFIER_VERBOSE=1` 恢复完整控制台输出（info 也打）；`=0` 关闭全部 stderr 双写。
排障时打开，平时不需要。

### 验证

同一份配置跑 `apply()`，统计真正落到控制台的条数：

| | 控制台条数 |
|---|---|
| 旧（全部双写） | **11** |
| 新（默认） | **1**（仅「权限已自动收紧」一次性提示） |
| 新（`VERBOSE=1`） | 11（与旧版等价，排障可用） |

- 分级规则单测：`error`/`warn` 出现在控制台，`info`/`debug` 不出现。
- 回归：网关真实投递 `{"ok":true}`；审批仍只发 1 条卡片（去重未被破坏）。

## [0.13.1-qq.5]

出站只走网关、日志去噪、面板文案适配。

### 变更

- **QQ 出站一律走 noti-gateway**（`src/adapters/qq-bot.mjs` 202 → 74 行）：
  删除整条直连 QQ 官方 API 的路径（换 `access_token` → `/v2/.../messages`、
  `msg_seq` 计数、限速门、分段发送、`apiBase`/`markdown`/`rateMs` 配置）。
  现在 `gatewayUrl` + `gatewayToken` 是**唯一必需项**，网关失败即本条失败——
  **不再静默回退直连**（旧行为会让"以为走网关其实走了直连"成为不可观测的事实）。
  `appId`/`appSecret` 仅为**入站**网关保留，出站不再使用。
- **日志前缀统一为 `[dsh-notifier-gate]`**：此前 23 个模块沿用旧包名
  `[dsh-notifier-qq]`，导致卸载原插件后仍看到"dsh-notifier-qq"日志、误以为原插件没卸干净。
  这是本插件自己的前缀，包名早已是 `dsh-notifier-gate`。
  （`PANEL_ID`/`RPC 通道`/`state 目录`/locale 命名空间仍是旧名——改这些要两端同步且会丢状态，故保留。）
- **网关会话轮转日志降噪**：`op7`（服务端要求重连）与闭码 `4009`（会话轮转）
  在稳态下周期性出现，属平台常规行为。现在只在**首次** warn，之后降为 `debug`；
  `READY` 同样只首次出声。`4004`（认证失败）/`4008`（限速）仍无条件 warn。
- **启动日志改为网关口径**：`已启用渠道：qq-bot` → `出站就绪：qq-bot（一律经 noti-gateway 投递）`。

### 面板（client.js）

- 标题 `通知与控制` → `QQ 通知`，副标题说明"经 QQ 机器人送达（一律走统一网关）"。
- `通知渠道` → `QQ 通道`；空状态由"尚未配置通知渠道/选择你已经在使用的渠道"
  改为"尚未配置 QQ 网关/填写 noti-gateway 地址与 token"。
- 修正面板内残留的旧包名展示（`无法读取 dsh-notifier-qq 状态` 等）。
- 管理台凭证表单字段表改为网关优先：`gatewayUrl`/`gatewayToken` 标为必填，
  `gatewayToken` 纳入脱敏字段；`appId`/`appSecret` 标注"仅入站网关使用"。

### 验证

- 真实投递：`runChannelTest` 经网关 → `{"ok":true}`，QQ 侧实收。
- 仅网关字段（无 appId）可解析通过；只填直连凭据被正确拒绝并给出网关指引。
- 网关不可达（`127.0.0.1:9`）→ `ok:false`，**确认不再回退直连**。
- 日志降噪：模拟 6 轮 `4009 + READY` → 仅 2 条 warn，10 条 debug。
- 回归上一版的去重修复：审批仍只有 1 条卡片、提问仍为「卡片 + 回执」。

## [0.13.1-qq.4]

「仅保留卡片，不要纯文字，不要重复发」。

### 修复

同一次审批/提问会连发两条消息（卡片 + 纯文字）。根因是「已送达」判定用了
**入站 channel 名**（`qq`）、却拿去和**出站 adapter type**（`qq-bot`）比较：

| 位置 | 旧行为 | 现在 |
|---|---|---|
| `approval/router.mjs` 广播线 | `channelTypes === null`（全局广播）时**整个过滤被跳过**，卡片已到的 QQ 又收一条「回复 1 批准」 | 广播同样按别名归一剔除已覆盖渠道 |
| `approval/router.mjs` 广播线 | 卡片失败时先 `sendText` 逐目标兜底，广播**再发一遍** | `hintTargets` 也计入已覆盖，兜底成功后不再广播 |
| `questions/router.mjs` 编号线 | `deliveredTypes` 存 `qq`，`allTypes` 是 `qq-bot`，`has()` 恒 false → 卡片后又发编号文字 | 改用 `isCoveredByOutbound` 做别名归一（`qq-bot ↔ qq`） |

### 变更

- **升级提醒不再打扰已有卡片的目标**：审批的「仍在等待批准」与提问的
  「仍在等待作答」原本对全部目标发纯文字。现在卡片已送达的目标不再收提醒
  （卡片就在会话里，点一下即可）；提醒只保留给**没有卡片可点**的目标。

### 有意保留的纯文字（不是重复）

群聊无按钮降级、卡片发送失败兜底、编号回复话术、裁决/超时结果回执
（回执是结果而非重复通知）、长消息分段。

### 验证

桩测试逐条统计真实发送次数：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 审批（单聊，卡片可达） | 2（卡片 + 文字） | **1（仅卡片）** |
| 提问（单聊，卡片可达） | 3（卡片 + 文字 + 回执） | **2（卡片 + 回执）** |
| 审批（卡片发送失败） | 2（兜底文字 + 广播） | **1（兜底文字）** |
| 审批/提问 升级提醒（卡片已送达） | 卡片 + 提醒文字 | **仅卡片** |
| 审批/提问 升级提醒（卡片失败） | — | 提醒文字照发（兜底不降级） |

## [0.13.1-qq.3]

配置自持：不再读取 `dsh-notifier` 插件的配置与状态。

### 变更

- **移除配置借用**（删 `src/compat/original-config.mjs`）：`apply()` 不再调用
  `mergeOriginalConfig()`，配置**唯一来源是本插件自己的 cordis 配置行**
  （profile `cordis.patch.yml` 里 `- id: dsh-notifier-gate` 的 `config`）。
  原插件即使装着、即使有配置行，对本站也不再产生任何影响。
- **移除身份播种**：不再从 `~/.dsh/dsh-notifier/state.json` 搬
  `inbound:bindings` / `inbound:pairing` / `route:sessions` / `aq:*`。本插件状态
  只在 `~/.dsh/dsh-notifier-qq/state.json` 里读写，两个插件完全独立。
- **删除零依赖 YAML 解析器**（`src/compat/mini-yaml.mjs`）：它只为读原插件 patch
  而存在，随借用一起移除（`src/compat/` 目录整体删除）。运行时依赖仍是 0——
  配置已由宿主 cordis 解析后作为 `config` 传入。
- **日志改为回报自身配置来源**：启动时打印
  `配置来源：本插件配置行（channels: …；不再读取 dsh-notifier 插件的配置）`；
  配置行里没有启用渠道时给出可操作告警（`本插件配置行里没有启用任何渠道…`），
  取代原先的「未取到原插件配置」提示。

### 升级注意

- 早期版本靠借用可以零 YAML 跑起来；升级后必须把凭证写进 `- id: dsh-notifier-gate`
  自己的 config（该 config 是整块替换，bundle 默认行会被完全覆盖）。
- owner 绑定若只存在于原插件 state，升级后需用管理台重新配对，或手工把
  `inbound:bindings` 等键复制进本插件 state。

### 验证

- 用桩 ctx 跑通 `apply()`：显式配置下 `channels: ['qq-bot']`、`skipped: 0`，
  日志出现「配置来源：本插件配置行」、无任何借用类日志；原插件 `state.json`
  字节级未变（只读断言通过）。
- 空 `channels` 分支：`channels: 0` 且给出「本插件配置行里没有启用任何渠道」告警。
- `dsh --profile web --dump-config` 确认组合后的 `- id: dsh-notifier-gate` 行带上了
  本插件的完整 config（channels/approval/events/admin/inbound），
  且 profile 里不再有 `- id: dsh-notifier` 行。

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

- **沿用原插件配置与身份**（`src/compat/original-config.mjs`，**已在 0.13.1-qq.3 移除**）：装配期按
  「本插件显式配置 → 本插件 state → 原插件 profile 配置行 → 原插件 state」
  优先级取值，字段级合并（原插件作底、显式字段覆盖）。
  借用范围：`channels[qq-bot]`、`inbound.qq`、`approval`、`events`、`routing`、
  `segment`、`debounceMs`、`summaryMaxChars`、`titlePrefix`、`redaction`、`lang`。
  持久身份借用仅四类键（`inbound:bindings` / `inbound:pairing` / `route:sessions` /
  `aq:*`），目标键已存在则不动（幂等播种）。**对原插件全程单向只读**。
- **零依赖 YAML 子集解析器**（`src/compat/mini-yaml.mjs`，**已在 0.13.1-qq.3 移除**）：插件位置不保证能解析
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
