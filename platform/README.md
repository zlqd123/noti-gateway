# platform —— 各 Agent 平台的 QQ 通知适配脚本

把不同 Agent CLI/客户端接入本网关（QQ 官方机器人单聊通知 + 按钮卡片）的各平台适配实现。
统一行为：**纯文本/Markdown 推送优先经网关**（统一配额、落盘、重试），网关不可达自动回退直连 QQ；**带按钮的交互卡片始终直连**（需与 QQ 事件监听器闭环）。

## 目录

| 平台 | 文件 | 部署位置 | 说明 |
|---|---|---|---|
| **ZCode** | `zcode/qq-notify.sh` | `~/.zcode/qq-notify.sh` | Stop/PermissionRequest 钩子脚本：任务完成文本通知（走网关+直连兜底）、权限确认按钮卡片（直连，配合 QQ 事件 WebSocket 监听器等待点击） |
| | `zcode/hooks.example.json` | `~/.zcode/cli/config.json` 的 `hooks` 节 | 钩子注册模板。**command 必须用 bash.exe 绝对路径**（Windows 钩子运行器不解析裸命令） |
| **OpenCode** | `opencode/index.ts` | `~/.config/opencode/plugins/gate-notify/index.ts` | 插件本体：任务完成/出错/提问文本通知走网关，审批卡片直连 + WS 监听按钮回调执行审批 |
| | `opencode/config.example.jsonc` | `~/.config/opencode/gate-notify.jsonc` | 配置模板 |
| | `opencode/probe-openid.mjs` `send-test.mjs` `README.md` | 同插件目录 | 辅助：探测 openid / 发送测试 |
| **DSH** | `dsh/`（完整插件源码包） | 见 `dsh/README.md` | `dsh-notifier-gate`：在 dsh-notifier 基础上给 `qq-bot` 适配器加了网关模式（`gatewayUrl/gatewaySecret/gatewayKeyId/gatewaySource` 四个字段；旧 `gatewayToken` 通道已废弃），通道配置在 profile 的 `cordis.patch.yml` |
| **WorkBuddy** | `workbuddy/qq_notifier_mcp.py` `notify_qq.py` | 自选目录（MCP args 指向） | MCP 服务 `gate-notify`：notify_me / notify_me_markdown 走网关，notify_me_card（提问卡片+等待点击）直连 |
| | `workbuddy/mcp.example.json` | `~/.workbuddy/mcp.json` | MCP 注册模板，凭据全部走 env |

## 需要填写的值

- `<q.qq.com 开放平台 AppID / AppSecret>`：[q.qq.com](https://q.qq.com) → 机器人开发设置
- `<你的用户 openid>`：私聊机器人一次，从 `C2C_MESSAGE_CREATE` 事件里取（`opencode/probe-openid.mjs` 可辅助）
- 网关地址与鉴权（HMAC 密钥，`keys.conf` 的 `kind=hmac` 行）：本网关服务端的 `config.yaml` / `keys.conf`；模板中网关地址以 `<网关地址>` 占位，请按自己的部署填写
- ZCode 钩子的 `<bash.exe 绝对路径>`：Windows 下钩子运行器要求可执行文件绝对路径（如 Git Bash 的 `D:/Git/bin/bash.exe`）

## 隐私约定

本目录所有文件均为模板/源码，**不含任何真实凭据、openid、服务器 IP 与本机用户路径**；真实配置文件（`config.yaml`、`gate-notify.jsonc`、`mcp.json`、ZCode `cli/config.json`）只存在于各机器本地。
