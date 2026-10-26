#!/usr/bin/env bash
# ZCode hook 通知脚本（QQ 官方机器人，C2C 单聊）
#   无参数        → Stop 事件：任务完成，发文本通知
#   参数 ask      → PermissionRequest 事件：发带「允许/拒绝」回调按钮的提问卡片，
#                   等待你在 QQ 里点击按钮（点击事件由 qq-bot-listener.py 经 WebSocket 长连接推回本机，不打开链接，手机在外网也能点）
# 说明：
#   - 永远 exit 0（exit 2 会直接拒绝请求）；stdout 只输出决策 JSON（hook 会解析）
#   - 日志: ~/.zcode/qq-notify.log；token 缓存: ~/.zcode/.qq-bot-token.cache
#   - Windows 下非 ASCII 不能走命令行参数，JSON 一律先写文件再 --data-binary 发送

# ==================== 在这里填你的配置 ====================
APPID="<在 q.qq.com 开放平台-开发设置 获取>"
CLIENT_SECRET="<同页 AppSecret，勿外泄>"
USER_OPENID="<你的用户openid：私聊机器人一次，从 C2C_MESSAGE_CREATE 事件里取>"  # 用户 openid：私聊机器人一次，从 C2C_MESSAGE_CREATE 事件里取
PING_URL="http://127.0.0.1:39527"  # qq-bot-listener.py 的健康检查地址
WAIT_SECONDS=90                     # 最多等按钮点击的秒数，超时回退到 ZCode 本地确认框
GATEWAY_URL="${GATEWAY_URL:-http://<网关IP>:10086}"        # 阿里云 noti-gateway；置空则任务完成通知直连 QQ
GATEWAY_TOKEN="${GATEWAY_TOKEN:-<网关 auth.token>}"
# =========================================================

TOKEN_URL="https://api.bot.qq.com/app/getAppAccessToken"
API_BASE="https://api.sgroup.qq.com"
SEND_URL="${API_BASE}/v2/users/${USER_OPENID}/messages"

CACHE_FILE="$HOME/.zcode/.qq-bot-token.cache"
RESP_FILE="$HOME/.zcode/.qq-notify-resp"
PAYLOAD_FILE="$HOME/.zcode/.qq-notify-payload"
LOG_DIR="$HOME/.zcode/log"          # 日志目录：一天一个文件

log() {
  mkdir -p "$LOG_DIR"
  echo "[$(date '+%F %T')] $*" >> "$LOG_DIR/qq-notify-$(date '+%F').log"
}

# 诊断：钩子由宿主进程拉起，环境与交互 shell 不同。记录代理变量和最终退出码，
# 排查"手动正常、钩子失败"类问题（2026-10-25 Stop 钩子连续失败 29 次后加入）
log "触发 argv=($*) proxy=(${HTTP_PROXY:-无}|${HTTPS_PROXY:-无}|${ALL_PROXY:-无}) pwd=$PWD"
trap 'log "退出 code=$?"' EXIT

# 代理免疫：本机装有 GHelper/clash 等代理，宿主进程可能给钩子注入代理变量；
# 网关和 QQ API 均为国内直连地址，走代理反而超时（10s curl 上限 → 钩子失败）
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}<网关IP>,api.bot.qq.com,api.sgroup.qq.com,bots.qq.com"
export no_proxy="$NO_PROXY"

if [[ "$APPID" == 请替换* || "$CLIENT_SECRET" == 请替换* || "$USER_OPENID" == 请替换* ]]; then
  exit 0
fi

# ---------- 1. 获取 access_token（优先用缓存） ----------
now=$(date +%s)
token=""
if [[ -f "$CACHE_FILE" ]]; then
  read -r cached_token cached_exp < "$CACHE_FILE" 2>/dev/null
  if [[ -n "${cached_token:-}" && -n "${cached_exp:-}" ]] && (( cached_exp - now > 0 )); then
    token="$cached_token"
  fi
fi
if [[ -z "$token" ]]; then
  resp=$(curl -s -m 10 -X POST "$TOKEN_URL" \
    -H "Content-Type: application/json" \
    -d "{\"appId\":\"$APPID\",\"clientSecret\":\"$CLIENT_SECRET\"}")
  token=$(printf '%s' "$resp" | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  expires_in=$(printf '%s' "$resp" | sed -n 's/.*"expires_in"[[:space:]]*:[[:space:]]*"\{0,1\}\([0-9]*\).*/\1/p')
  if [[ -z "$token" ]]; then
    log "获取 access_token 失败，响应: ${resp:0:300}"
    exit 0
  fi
  expires_in=${expires_in:-7200}
  echo "$token $(( now + expires_in - 300 ))" > "$CACHE_FILE"
fi

# ---------- 2. 公共函数 ----------
json_escape() {  # 转成可安全放进 JSON 字符串的形式
  local s="$1"
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/}
  printf '%s' "$s"
}

seq=$(((RANDOM << 15) + RANDOM + now % 100000 + 1))   # msg_seq，全局起始值

send_payload() {  # 发送 $PAYLOAD_FILE；成功返回 0，失败记日志并返回 1
  local code body
  code=$(curl -s -m 10 -o "$RESP_FILE" -w '%{http_code}' -X POST "$SEND_URL" \
    -H "Authorization: QQBot $token" \
    -H "Content-Type: application/json; charset=utf-8" \
    --data-binary @"$PAYLOAD_FILE")
  body=$(head -c 500 "$RESP_FILE" 2>/dev/null)
  if [[ "$code" == 2* && "$body" != *'"err_code"'* && "$body" != *'"code"'* ]]; then
    return 0
  fi
  log "发送失败 HTTP $code 响应: $body"
  return 1
}

dir="${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"
dir_e=$(json_escape "$dir")

# ---------- 3a. 提问模式：发卡片并等待按钮点击 ----------
if [[ "${1:-}" == "ask" ]]; then
  # 应答服务不在线就只发纯文本提示，立即回退到本地确认框（不空等）
  if ! curl -s -m 2 "${PING_URL}/ping" | grep -q pong; then
    msg=$(json_escape "❓ Zcode 请求权限确认 $(date '+%H:%M:%S')（本地应答服务未启动，按钮不可用）")
    printf '{"msg_type":0,"content":"%s","msg_seq":%s}' "$msg" "$seq" > "$PAYLOAD_FILE"
    send_payload
    exit 0
  fi

  rid="$(date +%s)x$RANDOM"
  answer_file="$HOME/.zcode/.qq-answer-$rid"

  # 从 hook stdin 的 JSON 里尽力提取工具名和命令摘要
  if [[ -t 0 ]]; then input=""; else input=$(head -c 4096 2>/dev/null); fi
  tool=$(printf '%s' "$input" | sed -n 's/.*"tool_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | tail -1)
  cmd=$(printf '%s' "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | tail -1)

  card="❓ **Zcode 请求权限确认**"$'\n'"🕐 $(date '+%H:%M:%S')"
  [[ -n "$dir" ]] && card+=$'\n'"📁 $dir"
  [[ -n "$tool" ]] && card+=$'\n'"🔧 $tool"
  [[ -n "$cmd" ]] && card+=$'\n'"💻 ${cmd:0:100}"
  card_e=$(json_escape "$card")

  # 第一优先：Markdown 卡片 + 允许/拒绝回调按钮（点击不打开链接，事件推送到 WebSocket 监听服务）
  printf '{"msg_type":2,"markdown":{"content":"%s"},"keyboard":{"content":{"rows":[{"buttons":[{"id":"1","render_data":{"label":"✅ 允许","visited_label":"已允许","style":1},"action":{"type":1,"data":"allow:%s","permission":{"type":2}}},{"id":"2","render_data":{"label":"❌ 拒绝","visited_label":"已拒绝","style":0},"action":{"type":1,"data":"deny:%s","permission":{"type":2}}}]}]}},"msg_seq":%s}' \
    "$card_e" "$rid" "$rid" "$seq" > "$PAYLOAD_FILE"
  if ! send_payload; then
    seq=$((seq + 1))
    # 降级二：纯文本 + 按钮
    printf '{"msg_type":0,"content":"%s","keyboard":{"content":{"rows":[{"buttons":[{"id":"1","render_data":{"label":"✅ 允许","visited_label":"已允许","style":1},"action":{"type":1,"data":"allow:%s","permission":{"type":2}}},{"id":"2","render_data":{"label":"❌ 拒绝","visited_label":"已拒绝","style":0},"action":{"type":1,"data":"deny:%s","permission":{"type":2}}}]}]}},"msg_seq":%s}' \
      "$card_e" "$rid" "$rid" "$seq" > "$PAYLOAD_FILE"
    if ! send_payload; then
      seq=$((seq + 1))
      # 降级三：纯文本（按钮不可用）
      printf '{"msg_type":0,"content":"%s","msg_seq":%s}' "$card_e" "$seq" > "$PAYLOAD_FILE"
      send_payload
      exit 0  # 没按钮可点，直接回退本地确认框
    fi
  fi

  # 等待按钮点击（qq-answer-server.py 写入应答文件）
  waited=0
  while (( waited < WAIT_SECONDS )); do
    if [[ -f "$answer_file" ]]; then
      ans=$(cat "$answer_file" 2>/dev/null)
      rm -f "$answer_file"
      if [[ "$ans" == "allow" || "$ans" == "deny" ]]; then
        printf '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","permissionDecision":"%s"}}' "$ans"
        exit 0
      fi
    fi
    sleep 2
    waited=$((waited + 2))
  done
  # 超时：不输出决策，回退到 ZCode 本地确认框
  exit 0
fi

# ---------- 3a2. 测试模式：多选项 + 自定义回答卡片 ----------
if [[ "${1:-}" == "test" ]]; then
  t0=$(date +%s%3N)

  card="🧪 **多选项卡片测试**"$'\n'"🕐 $(date '+%H:%M:%S')"$'\n'"今晚主菜吃什么？"$'\n'"点按钮选择；点「✍️ 自定义回答」后补全发送；或直接回复消息"
  card_e=$(json_escape "$card")

  printf '{"msg_type":2,"markdown":{"content":"%s"},"keyboard":{"content":{"rows":[{"buttons":[{"id":"1","render_data":{"label":"🍲 火锅","visited_label":"已选火锅","style":1},"action":{"type":1,"data":"answer:火锅","permission":{"type":2}}},{"id":"2","render_data":{"label":"🍛 咖喱饭","visited_label":"已选咖喱饭","style":1},"action":{"type":1,"data":"answer:咖喱饭","permission":{"type":2}}},{"id":"3","render_data":{"label":"🥗 沙拉","visited_label":"已选沙拉","style":1},"action":{"type":1,"data":"answer:沙拉","permission":{"type":2}}}]},{"buttons":[{"id":"4","render_data":{"label":"✍️ 自定义回答","visited_label":"请输入","style":0},"action":{"type":2,"data":"主菜:","permission":{"type":2}}}]}]}},"msg_seq":%s}' \
    "$card_e" "$seq" > "$PAYLOAD_FILE"
  if ! send_payload; then
    seq=$((seq + 1))
    printf '{"msg_type":0,"content":"%s","keyboard":{"content":{"rows":[{"buttons":[{"id":"1","render_data":{"label":"🍲 火锅","visited_label":"已选火锅","style":1},"action":{"type":1,"data":"answer:火锅","permission":{"type":2}}},{"id":"2","render_data":{"label":"🍛 咖喱饭","visited_label":"已选咖喱饭","style":1},"action":{"type":1,"data":"answer:咖喱饭","permission":{"type":2}}},{"id":"3","render_data":{"label":"🥗 沙拉","visited_label":"已选沙拉","style":1},"action":{"type":1,"data":"answer:沙拉","permission":{"type":2}}}]},{"buttons":[{"id":"4","render_data":{"label":"✍️ 自定义回答","visited_label":"请输入","style":0},"action":{"type":2,"data":"主菜:","permission":{"type":2}}}]}]}},"msg_seq":%s}' \
      "$card_e" "$seq" > "$PAYLOAD_FILE"
    send_payload
  fi

  # 等待：按钮点击（.qq-click-latest）或文本回复（.qq-msg-latest），取 t0 之后的新内容
  result=""
  waited=0
  while (( waited < ${QQ_TEST_WAIT:-90} )); do
    if [[ -f "$HOME/.zcode/.qq-click-latest" ]]; then
      line=$(cat "$HOME/.zcode/.qq-click-latest" 2>/dev/null)
      cts=${line%%$'\t'*}; cval=${line#*$'\t'}
      if [[ "$cts" =~ ^[0-9]+$ ]] && (( cts > t0 )) && [[ "$cval" == answer:* ]]; then
        result="按钮选择: ${cval#answer:}"
        break
      fi
    fi
    if [[ -f "$HOME/.zcode/.qq-msg-latest" ]]; then
      line=$(cat "$HOME/.zcode/.qq-msg-latest" 2>/dev/null)
      mts=${line%%$'\t'*}; mval=${line#*$'\t'}
      if [[ "$mts" =~ ^[0-9]+$ ]] && (( mts > t0 )) && [[ -n "$mval" ]]; then
        result="自定义回答: $mval"
        break
      fi
    fi
    sleep 2
    waited=$((waited + 2))
  done
  if [[ -n "$result" ]]; then
    printf '结果 -> %s\n' "$result"
  else
    printf '%s 秒内未收到任何回答\n' "${QQ_TEST_WAIT:-90}"
  fi
  exit 0
fi

# ---------- 3b. 任务完成模式：优先走 noti-gateway（持久化/重试/配额保护），失败直连兜底 ----------
# 会话标识：钩子环境注入 CLAUDE_SESSION_ID；兜底从 stdin JSON 提取
input=""
if [[ ! -t 0 ]]; then input=$(head -c 4096 2>/dev/null); fi
sid="${CLAUDE_SESSION_ID:-${ZCODE_SESSION_ID:-}}"
if [[ -z "$sid" ]]; then
  sid=$(printf '%s' "$input" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | tail -1)
fi

# 同一会话完成通知的静默窗口：秒。快速连发的回合（含自动重试触发的多次 Stop）只推第一条
STOP_MIN_INTERVAL="${STOP_MIN_INTERVAL:-40}"
if [[ -n "$sid" && "$STOP_MIN_INTERVAL" -gt 0 ]]; then
  ts_file="$HOME/.zcode/.qq-stop-ts-${sid}"
  now_s=$(date +%s)
  last_s=$(cat "$ts_file" 2>/dev/null || echo 0)
  if (( now_s - last_s < STOP_MIN_INTERVAL )); then
    exit 0
  fi
  echo "$now_s" > "$ts_file"
  find "$HOME/.zcode" -maxdepth 1 -name ".qq-stop-ts-*" -mmin +1440 -delete 2>/dev/null
fi

# 任务标题：ZCode 任务索引库 tasks.title（查不到回落为项目目录）
title=""
if [[ -n "$sid" ]]; then
  title=$(python - "$sid" <<'PYEOF'
import sqlite3, sys
try:
    db = sqlite3.connect(
        r'file:' + os.path.expanduser('~') + '/.zcode/v2/tasks-index.sqlite?mode=ro',
        uri=True, timeout=2)
    row = db.execute(
        "SELECT title FROM tasks WHERE task_id=? AND deleted=0",
        (sys.argv[1],)).fetchone()
    print((row[0] or '').strip() if row else '')
except Exception:
    print('')
PYEOF
)
fi

msg="✅ ZCode 任务完成 $(date '+%H:%M:%S')"
if [[ -n "$title" ]]; then
  msg="$msg"$'\n'"📌 ${title:0:60}"
elif [[ -n "$dir" ]]; then
  msg="$msg"$'\n'"📁 $dir"
fi

if [[ -n "$GATEWAY_URL" ]]; then
  # 中文必须写文件再发（命令行参数会被 Windows 编码转换成 ?）
  printf '%s' "$msg" > "$HOME/.zcode/.qq-gateway-payload"
  gw_code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "${GATEWAY_URL}/n/zcode" \
    -H "Authorization: Bearer ${GATEWAY_TOKEN}" \
    -H "X-Source: zcode" \
    -H "Content-Type: text/plain" \
    --data-binary @"$HOME/.zcode/.qq-gateway-payload")
  if [[ "$gw_code" == 20* ]]; then
    exit 0   # 202=已入队落盘，网关负责投递；200=同步成功
  fi
  log "网关投递失败 HTTP $gw_code，回退直连 QQ"
fi

msg_e=$(json_escape "$msg")
printf '{"msg_type":0,"content":"%s","msg_seq":%s}' "$msg_e" "$seq" > "$PAYLOAD_FILE"
send_payload

exit 0
