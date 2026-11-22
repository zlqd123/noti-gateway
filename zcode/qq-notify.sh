#!/usr/bin/env bash
# ZCode hook 通知脚本（QQ 官方机器人，C2C 单聊）
# 接入协议/端点/HMAC 签名速记：同目录《接入文档.md》（noti-gateway 仓库根同名文件）
#   无参数        → Stop 事件：任务完成，发文本通知
#   参数 ask      → PermissionRequest 事件：发带「允许/拒绝」回调按钮的提问卡片，
#                   等待你在 QQ 里点击按钮。按钮点击经网关 SSE 事件订阅接收：
#                   常驻监听在线就用它，否则钩子现场拉起一次性监听器
#                   qq-ask-once.py（先等 SSE 就绪再发卡片，收完点击即退出）——
#                   不打开链接，手机在外网也能点，且无需开机启动任何服务
# 说明：
#   - 永远 exit 0（exit 2 会直接拒绝请求）；stdout 只输出决策 JSON（hook 会解析）
#   - 日志: ~/.zcode/log/qq-notify-<日期>.log（一天一个文件）
#   - Windows 下非 ASCII 不能走命令行参数，JSON 一律先写文件再 --data-binary 发送

# ==================== 在这里填你的配置 ====================
PING_URL="http://127.0.0.1:39527"  # 常驻监听（qq-bot-listener.py，可选）健康检查地址；不在线则按需拉起一次性监听器
WAIT_SECONDS=90                     # 最多等按钮点击的秒数，超时回退到 ZCode 本地确认框
GATEWAY_URL="${GATEWAY_URL:-http://<网关地址>}"        # 阿里云 noti-gateway
GATEWAY_KEY_ID="${GATEWAY_KEY_ID:-zcode}"                       # 网关 keys.conf 里的密钥代号
GATEWAY_SECRET="${GATEWAY_SECRET:-<网关 keys.conf 里 zcode 行的 HMAC secret>}"  # keys.conf zcode 行（HMAC v1，唯一鉴权方式）
# =========================================================

# 直连 QQ 的 TOKEN_URL/SEND_URL 已下线（2026-11-22）：所有发送统一走网关 raw_qq/通知接口

RESP_FILE="$HOME/.zcode/.qq-notify-resp"
RAW_BODY_FILE="$HOME/.zcode/.qq-notify-rawbody"
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
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}<网关域名>"
export no_proxy="$NO_PROXY"

# ---------- 1. （直连 QQ 已下线；QQ access_token 由网关侧统一获取） ----------
now=$(date +%s)

# ---------- 2. 公共函数 ----------
json_escape() {  # 转成可安全放进 JSON 字符串的形式
  local s="$1"
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/}
  printf '%s' "$s"
}

seq=$(((RANDOM << 15) + RANDOM + now % 100000 + 1))   # msg_seq，全局起始值

# 结束本钩子自己拉起的一次性监听器（按 PID，只杀自己启动的进程；ask/choice 两模式共用）
listener_pid=""
ready_file=""
kill_listener() {
  if [[ -n "$listener_pid" ]]; then
    kill "$listener_pid" 2>/dev/null
    wait "$listener_pid" 2>/dev/null
    listener_pid=""
  fi
  [[ -n "$ready_file" ]] && rm -f "$ready_file"
}

send_payload() {  # 经网关 raw_qq 透传发送 $PAYLOAD_FILE；成功返回 0，失败记日志并返回 1
  # msg_id/msg_seq 等被动字段在载荷内原样透传；网关负责转换成 QQ 官方通道发送
  { printf '{"raw_qq":'; cat "$PAYLOAD_FILE"; printf '}'; } > "$RAW_BODY_FILE"
  local resp ts nonce sha sig p="/n/zcode" canonical
  ts=$(date +%s)
  nonce=$(openssl rand -hex 8 2>/dev/null || printf '%s%06d' "$ts" "$RANDOM$RANDOM")
  sha=$(openssl dgst -sha256 -r "$RAW_BODY_FILE" | awk '{print $1}')
  canonical="POST
$p
$ts
$nonce
$sha"
  sig=$(printf '%s' "$canonical" | openssl dgst -sha256 -hmac "$GATEWAY_SECRET" | awk '{print $NF}')
  resp=$(curl -s -m 30 -X POST "$GATEWAY_URL$p?wait=1" \
    -H "Content-Type: application/json; charset=utf-8" \
    -H "X-Noti-Signature: v1=$sig" -H "X-Noti-Timestamp: $ts" \
    -H "X-Noti-Nonce: $nonce" -H "X-Noti-Key-Id: $KEY_ID" \
    --data-binary @"$RAW_BODY_FILE")
  printf '%s' "$resp" > "$RESP_FILE"
  if [[ "$resp" == *'"ok":true'* && "$resp" == *'"qq:me":{"ok":true'* ]]; then
    return 0
  fi
  log "网关投递失败: ${resp:0:300}"
  return 1
}

dir="${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"
dir_e=$(json_escape "$dir")

# ---------- 3a. 提问模式：发卡片并等待按钮点击 ----------
if [[ "${1:-}" == "ask" ]]; then
  # 从 hook stdin 的 JSON 里提取工具名和命令摘要
  if [[ -t 0 ]]; then input=""; else input=$(head -c 4096 2>/dev/null); fi
  tool=$(printf '%s' "$input" | sed -n 's/.*"tool_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | tail -1)
  cmd=$(printf '%s' "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | tail -1)

  # 选择题有自己的钩子（PreToolUse/choice 分支）。AskUserQuestion 会同时触发
  # PermissionRequest，若此处再拦会双重扣住同一工具调用（实测先 600s 再 49s），直接放行
  if [[ "$tool" == "AskUserQuestion" ]]; then
    log "ask: 工具为 AskUserQuestion，交由 choice 钩子处理，本钩子放行"
    exit 0
  fi

  rid="$(date +%s)x$RANDOM"
  answer_file="$HOME/.zcode/.qq-answer-$rid"
  ready_file="$HOME/.zcode/.qq-ws-ready-$rid"
  rm -f "$answer_file" "$ready_file"

  # 按钮点击事件经 WebSocket 长连接接收：常驻监听在线就用它；
  # 不在线则现场拉起一次性监听器（先等 WS 就绪再发卡片，防止点击落在无连接窗口丢失）。
  # 一次性监听器随本钩子退出，不常驻、无需开机启动。
  listener_pid=""
  if curl -s -m 2 "${PING_URL}/ping" | grep -q pong; then
    log "常驻监听在线，直接使用"
  else
    python "$HOME/.zcode/qq-ask-once.py" "$rid" "$((WAIT_SECONDS + 15))" \
      </dev/null >> "$LOG_DIR/qq-ask-once-run.log" 2>&1 &
    listener_pid=$!
    ready_waited=0
    while (( ready_waited < 12 )) && [[ ! -f "$ready_file" ]]; do
      sleep 1
      ready_waited=$((ready_waited + 1))
    done
    if [[ ! -f "$ready_file" ]]; then
      log "按需监听器未就绪（${ready_waited}s），不发卡片，回退本地确认框"
      kill_listener
      exit 0
    fi
    log "按需监听器就绪（${ready_waited}s）"
  fi

  card="❓ **Zcode 请求权限确认**"$'\n'"🕐 $(date '+%H:%M:%S')"
  [[ -n "$dir" ]] && card+=$'\n'"📁 $dir"
  [[ -n "$tool" ]] && card+=$'\n'"🔧 $tool"
  [[ -n "$cmd" ]] && card+=$'\n'"💻 ${cmd:0:100}"
  card_e=$(json_escape "$card")

  # Markdown 卡片 + 允许/拒绝回调按钮（点击不打开链接，事件推送到 WebSocket 监听服务）
  printf '{"msg_type":2,"markdown":{"content":"%s"},"keyboard":{"content":{"rows":[{"buttons":[{"id":"1","render_data":{"label":"✅ 允许","visited_label":"已允许","style":1},"action":{"type":1,"data":"allow:%s","permission":{"type":2}}},{"id":"2","render_data":{"label":"❌ 拒绝","visited_label":"已拒绝","style":0},"action":{"type":1,"data":"deny:%s","permission":{"type":2}}}]}]}},"msg_seq":%s}' \
    "$card_e" "$rid" "$rid" "$seq" > "$PAYLOAD_FILE"
  if ! send_payload; then
    # 不降级纯文本：卡片发不出就放弃远程代答，回退本地确认框
    log "markdown 卡片发送失败，放弃远程代答"
    kill_listener
    exit 0
  fi

  # 等待按钮点击（常驻监听或一次性监听器写入应答文件）
  waited=0
  while (( waited < WAIT_SECONDS )); do
    if [[ -f "$answer_file" ]]; then
      ans=$(cat "$answer_file" 2>/dev/null)
      rm -f "$answer_file"
      if [[ "$ans" == "allow" || "$ans" == "deny" ]]; then
        printf '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","permissionDecision":"%s"}}' "$ans"
        kill_listener
        exit 0
      fi
    fi
    sleep 2
    waited=$((waited + 2))
  done
  # 超时：不输出决策，回退到 ZCode 本地确认框
  kill_listener
  exit 0
fi

# ---------- 3a3. 选择题模式（PreToolUse/AskUserQuestion）----------
# ZCode 的选择题不走 PermissionRequest（工具自动放行，问题挂在 UI 上等人答）。
# 默认=挂起模式（可靠）：钩子按住工具调用，QQ 卡片等你应答，答案经 exit 2+reason
#   喂回同一轮继续；超时（默认 600s，~/.zcode/qq-choice-hold-seconds 可调）放行本地 UI。
#   注意：挂起期间本地不出题（工具未执行），QQ 桌面端/手机端均可作答。
# 可选=双端并行（本地 UI 立即出现 + QQ 卡片；但 QQ 端答案靠注入 ZCode 输入框发送——
#   实测不可靠：文本会落在焦点所在的任意会话且回车不一定发送）：
#   建 ~/.zcode/hooks/QQ-CHOICE-DUAL 旗标启用。
# 停用全部：建空文件 ~/.zcode/hooks/DISABLE-QQ-CHOICE
if [[ "${1:-}" == "choice" ]]; then
  if [[ -f "$HOME/.zcode/hooks/DISABLE-QQ-CHOICE" ]]; then exit 0; fi
  input=""
  if [[ ! -t 0 ]]; then input=$(head -c 65536 2>/dev/null); fi

  # 解析 questions + 会话 id → 计划文件（CHOICE / SID / Q 行）
  input_file="$HOME/.zcode/.qq-choice-input"
  plan_file="$HOME/.zcode/.qq-choice-plan"
  printf '%s' "$input" > "$input_file"
  python "$HOME/.zcode/qq-choice-parse.py" "$input_file" > "$plan_file" 2>/dev/null
  rm -f "$input_file"
  head -1 "$plan_file" | grep -q '^CHOICE$' || exit 0

  sid=$(awk -F'\t' '$1 == "SID" {print $2; exit}' "$plan_file")
  rid="$(date +%s)x$RANDOM"

  # —— 挂起模式（默认）——
  if [[ ! -f "$HOME/.zcode/hooks/QQ-CHOICE-DUAL" ]]; then
    hold_s=600
    if [[ -f "$HOME/.zcode/qq-choice-hold-seconds" ]]; then
      hold_s=$(head -1 "$HOME/.zcode/qq-choice-hold-seconds" | tr -cd '0-9')
      [[ "$hold_s" =~ ^[0-9]+$ ]] && (( hold_s >= 30 )) || hold_s=600
    fi
    answer_file="$HOME/.zcode/.qq-answer-$rid"
    ready_file="$HOME/.zcode/.qq-ws-ready-$rid"
    rm -f "$answer_file" "$ready_file"

    log "choice: 挂起模式——按住 ${hold_s}s 等 QQ 应答（rid=$rid）"
    # choice 子模式下卡片由 python 自行发送（READY 后直接发，无就绪文件），只等应答文件
    python "$HOME/.zcode/qq-ask-once.py" choice "$rid" "$hold_s" "$plan_file" \
      </dev/null >> "$LOG_DIR/qq-ask-once-run.log" 2>&1 &
    listener_pid=$!

    waited=0
    while (( waited < hold_s + 15 )); do
      [[ -f "$answer_file" ]] && break
      kill -0 "$listener_pid" 2>/dev/null || break  # 监听器异常退出（卡片未发出等）
      sleep 2
      waited=$((waited + 2))
    done
    kill_listener

    if [[ -f "$answer_file" ]]; then
      ans=$(cat "$answer_file" 2>/dev/null)
      rm -f "$answer_file"
      if [[ -n "$ans" ]]; then
        reason="用户已通过 QQ 远程回答了你的提问（$(date '+%H:%M:%S')）：${ans}。请直接依据该回答继续后续工作，不要再次询问。"
        log "choice: 挂起解除，已代答 -> ${ans:0:60}"
        printf '%s' "$reason" >&2
        exit 2  # deny：AskUserQuestion 不落地，reason 携带答案回给同一轮的模型
      fi
    fi
    log "choice: 超时无人应答，放行走本地 UI"
    exit 0
  fi

  # —— 双端并行模式（opt-in，QQ-CHOICE-DUAL 旗标）——本地 UI 照常出题，QQ 卡片由分离式监视器负责
  log "choice: 双端并行（opt-in）——钩子放行走本地 UI，分离式监视器发 QQ 卡片（rid=$rid）"
  python "$HOME/.zcode/qq-choice-watch.py" "$sid" "$rid" "$plan_file" \
    </dev/null >> "$LOG_DIR/qq-choice-watch-run.log" 2>&1 &
  disown
  exit 0  # 本地 UI 立即出现；QQ 端由监视器负责，30 分钟内远程代答有效
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

# ---------- 3b. 任务完成模式：文本走网关通知接口；失败换 raw_qq 卡片通道重发 ----------
# 会话标识：钩子环境注入 CLAUDE_SESSION_ID；兜底从 stdin JSON 提取
input=""
if [[ ! -t 0 ]]; then input=$(head -c 4096 2>/dev/null); fi

# 载荷采样：保存最近 10 次 Stop 的原始 stdin JSON，用于区分主会话/子任务事件
if [[ -n "$input" ]]; then
  mkdir -p "$LOG_DIR"
  printf '%s' "$input" > "$LOG_DIR/stop-payload-$(date +%s%3N).json"
  ls -t "$LOG_DIR"/stop-payload-*.json 2>/dev/null | tail -n +11 | xargs -r rm -f
fi

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
import os, sqlite3, sys
try:
    db = sqlite3.connect(
        'file:' + os.path.expanduser('~') + '/.zcode/v2/tasks-index.sqlite?mode=ro',
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
  gw_payload="$HOME/.zcode/.qq-gateway-payload"
  printf '%s' "$msg" > "$gw_payload"
  gw_code=""
  # HMAC 签名 v1：canonical = METHOD\nPATH\nTS\nNONCE\nBODY_SHA256_HEX（密钥按 UTF-8 字节参与 HMAC）
  if [[ -n "$GATEWAY_SECRET" ]]; then
    gw_ts=$(date +%s)
    gw_nonce="${gw_ts}-$$-${RANDOM}${RANDOM}"
    gw_sha=$(sha256sum "$gw_payload" | awk '{print $1}')
    gw_sig=$(printf 'POST\n/n/zcode\n%s\n%s\n%s' "$gw_ts" "$gw_nonce" "$gw_sha" \
      | openssl dgst -sha256 -hmac "$GATEWAY_SECRET" | awk '{print $NF}')
    gw_code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "${GATEWAY_URL}/n/zcode" \
      -H "X-Noti-Signature: v1=${gw_sig}" \
      -H "X-Noti-Timestamp: ${gw_ts}" \
      -H "X-Noti-Nonce: ${gw_nonce}" \
      -H "X-Noti-Content-SHA256: ${gw_sha}" \
      -H "X-Noti-Key-Id: ${GATEWAY_KEY_ID}" \
      -H "Content-Type: text/plain" \
      --data-binary @"$gw_payload")
  fi
  if [[ "$gw_code" == 20* ]]; then
    exit 0   # 202=已入队落盘，网关负责投递；200=同步成功
  fi
  log "网关文本投递失败 HTTP $gw_code，换 raw_qq 通道重发"
fi

msg_e=$(json_escape "$msg")
printf '{"msg_type":0,"content":"%s","msg_seq":%s}' "$msg_e" "$seq" > "$PAYLOAD_FILE"
send_payload

exit 0
