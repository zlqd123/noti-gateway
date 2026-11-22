#!/usr/bin/env bash
# qq-send-file.sh — 经 noti-gateway 发文件到 QQ 单聊（HMAC v1 签名，不再直连 QQ API）
# 接入协议/端点/HMAC 签名速记：同目录《接入文档.md》（noti-gateway 仓库根同名文件）
# 用法: qq-send-file.sh <本地文件路径> [附言文本]
#   ≤12MB:  内联 b64 一次 POST /n/zcode（wait=1 拿同步回执）
#   >12MB:  /u/* 分片收件（按网关 chunk_size 切片）拿 ref，再 /n/zcode 引用发送
#   网关负责转换成 QQ bot 可接受的富媒体通道（upload_prepare → PUT → part_finish
#   → files 合并 → msg_type=7），本脚本只与网关对话
# 密钥: 与 qq-notify.sh 同源（keys.conf zcode 行）
# 日志: ~/.zcode/log/qq-send-file-<date>.log

# ==================== 配置(与 qq-notify.sh 保持一致) ====================
GATEWAY="${GATEWAY:-http://<网关地址>}"
KEY_ID="${GATEWAY_KEY_ID:-zcode}"
SECRET="${GATEWAY_SECRET:-<网关 keys.conf 里 zcode 行的 HMAC secret>}"
INLINE_LIMIT=$((12 * 1024 * 1024))   # b64 膨胀 4/3 后 ≈16MB，贴网关 MAX_BODY 上限
LOG_DIR="$HOME/.zcode/log"
# ========================================================================

log() { mkdir -p "$LOG_DIR"; echo "[$(date '+%F %T')] $*" >> "$LOG_DIR/qq-send-file-$(date '+%F').log"; }

FILE_PATH="${QQ_FILE:-${1:?用法: QQ_FILE=<路径> QQ_NOTE=@附言文件 bash qq-send-file.sh}}"
NOTE="${QQ_NOTE:-${2:-}}"
[[ -n "$NOTE" && "$NOTE" == @* ]] && NOTE=$(cat "${NOTE#@}")
[[ -f "$FILE_PATH" ]] || { echo "文件不存在: $FILE_PATH"; exit 1; }
FSIZE=$(stat -c %s "$FILE_PATH")
FNAME=$(basename "$FILE_PATH")
log "开始发送 $FNAME ($FSIZE bytes) 经网关 $GATEWAY"

# ---------- 简易 mime 推断 ----------
case "${FNAME##*.}" in
  zip) MIME="application/zip" ;;
  pdf) MIME="application/pdf" ;;
  png) MIME="image/png" ;;
  jpg|jpeg) MIME="image/jpeg" ;;
  gif) MIME="image/gif" ;;
  txt|log|md) MIME="text/plain" ;;
  json|yaml|yml) MIME="application/json" ;;
  *) MIME="application/octet-stream" ;;
esac

# ---------- HMAC v1 签名 POST（canonical: METHOD\nPATH\nTS\nNONCE\nBODY_SHA256_HEX） ----------
gw_post() {  # $1=签名用PATH(不含查询串)  $2=完整URL(可带?wait=1)  $3=body文件 -> stdout: 响应体
  local p="$1" url="$2" b="$3" ts nonce sha sig
  ts=$(date +%s)
  nonce=$(openssl rand -hex 8 2>/dev/null || printf '%s%06d' "$ts" "$RANDOM$RANDOM")
  sha=$(openssl dgst -sha256 -r "$b" | awk '{print $1}')
  sig=$(printf '%s\n%s\n%s\n%s\n%s' "POST" "$p" "$ts" "$nonce" "$sha" \
        | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')
  curl -s -m 180 -X POST "$url" \
    -H "Content-Type: application/json; charset=utf-8" \
    -H "X-Noti-Signature: v1=$sig" -H "X-Noti-Timestamp: $ts" \
    -H "X-Noti-Nonce: $nonce" -H "X-Noti-Key-Id: $KEY_ID" \
    --data-binary @"$b"
}

# ---------- JSON 字符串转义 ----------
jesc() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\r'/ }; printf '%s' "${s//$'\n'/\\n}"; }

TMP="$HOME/.zcode/.qq-gw-$$"
mkdir -p "$TMP"
cleanup() { rm -rf "$TMP"; }
trap 'cleanup; log "退出 code=$?"' EXIT

report() {  # $1=/n/zcode 的响应体
  local ok msg_id
  ok=$(printf '%s' "$1" | sed -n 's/.*"results"[^{]*{[^}]*"ok"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p')
  msg_id=$(printf '%s' "$1" | sed -n 's/.*"msg_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  if [[ "$ok" == "true" ]]; then
    echo "[OK] 网关回执成功 msg_id=${msg_id:0:40}…"
    log "成功 msg_id=$msg_id"
  else
    echo "[FAIL] 网关回执失败: $(head -c 300 <<<"$1")"
    log "失败回执: $(head -c 300 <<<"$1")"
    exit 1
  fi
}

TITLE=$(jesc "${NOTE:-$FNAME}")
if (( FSIZE <= INLINE_LIMIT )); then
  # ---------- 方式A: 内联 b64 → /n/zcode ----------
  log "方式A: 内联 b64 ($FSIZE bytes)"
  {
    printf '{"title":"%s","body":"","level":"info","file":{"b64":"' "$TITLE"
    base64 -w0 "$FILE_PATH" 2>/dev/null || base64 "$FILE_PATH" | tr -d '\n'
    printf '","mime":"%s","name":"%s"}}' "$MIME" "$(jesc "$FNAME")"
  } > "$TMP/inline.json"
  resp=$(gw_post "/n/zcode" "$GATEWAY/n/zcode?wait=1" "$TMP/inline.json")
else
  # ---------- 方式B: /u/* 分片收件 → ref → /n/zcode ----------
  log "方式B: 分片收件 ($FSIZE bytes)"
  printf '{"name":"%s","mime":"%s","kind":"file","size":%s}' "$(jesc "$FNAME")" "$MIME" "$FSIZE" > "$TMP/init.json"
  init_resp=$(gw_post "/u/init" "$GATEWAY/u/init" "$TMP/init.json")
  UID_GW=$(printf '%s' "$init_resp" | sed -n 's/.*"upload_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  CHUNK=$(printf '%s' "$init_resp" | sed -n 's/.*"chunk_size"[[:space:]]*:[[:space:]]*"\{0,1\}\([0-9]*\).*/\1/p')
  CHUNK=${CHUNK:-4194304}
  [[ -n "$UID_GW" ]] || { echo "[FAIL] /u/init 失败: $(head -c 200 <<<"$init_resp")"; log "init 失败: $(head -c 200 <<<"$init_resp")"; exit 1; }
  log "upload_id=$UID_GW chunk=$CHUNK"

  split -b "$CHUNK" -d -a 3 "$FILE_PATH" "$TMP/p_"
  i=0
  for part in "$TMP"/p_*; do
    pp="/u/$UID_GW/part/$i"
    resp=$(gw_post "$pp" "$GATEWAY$pp" "$part")
    log "part$i $(stat -c %s "$part") bytes -> $(head -c 120 <<<"$resp")"
    i=$((i+1))
  done
  printf '{}' > "$TMP/comp.json"
  comp_resp=$(gw_post "/u/$UID_GW/complete" "$GATEWAY/u/$UID_GW/complete" "$TMP/comp.json")
  REF=$(printf '%s' "$comp_resp" | sed -n 's/.*"ref"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [[ -n "$REF" ]] || { echo "[FAIL] /u/complete 失败: $(head -c 200 <<<"$comp_resp")"; log "complete 失败"; exit 1; }
  echo "[OK] 分片收件完成 ref=$REF"
  printf '{"title":"%s","body":"","level":"info","file":{"ref":"%s"}}' "$TITLE" "$REF" > "$TMP/ref.json"
  resp=$(gw_post "/n/zcode" "$GATEWAY/n/zcode?wait=1" "$TMP/ref.json")
fi

report "$resp"
cleanup
echo "DONE"
