"""QQ 事件监听服务——经 noti-gateway SSE 订阅入站事件（零 QQ 直连）。

接入协议/端点/HMAC 签名速记：同目录《接入文档.md》（noti-gateway 仓库根同名文件）。

职责：
  1. 订阅网关 /stream?types=qq_event（HMAC v1 签名）。网关 qqws 插件独占
     QQ 官方 WebSocket，收到的 C2C/群消息与按钮回调（INTERACTION_CREATE）
     统一以 qq_event 广播；本服务只做本地消费
  2. 收到 INTERACTION_CREATE（提问卡片上的「允许/拒绝」回调按钮点击）时，
     把结果写入 ~/.zcode/.qq-answer-<rid>，qq-notify.sh 轮询该文件得到决策；
     交互回应（PUT /interactions/{id}）由网关自动完成，QQ 客户端不转圈
  3. 收到 C2C/GROUP 消息时写 ~/.zcode/.qq-msg-latest（诊断/测试用）
  4. 在 127.0.0.1:39527 提供 /ping 健康检查（qq-notify.sh 用来判断本服务是否存活）

用法（后台无窗口运行）:
    pythonw C:/Users/TX5pro/.zcode/qq-bot-listener.py
日志: ~/.zcode/log/qq-bot-listener-<日期>.log
依赖: pip install httpx
"""
import datetime
import hashlib
import hmac as hmac_mod
import http.server
import json
import os
import re
import socketserver
import threading
import time

import httpx

GATEWAY_URL = "http://<网关地址>"        # 阿里云 noti-gateway
KEY_ID = "zcode"
GATEWAY_SECRET = "<网关 keys.conf 里 zcode 行的 HMAC secret>"

ZDIR = os.path.join(os.path.expanduser("~"), ".zcode")
LOG_DIR = os.path.join(ZDIR, "log")   # 日志目录：一天一个文件
PING_PORT = 39527
RID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def log(msg: str) -> None:
    now = datetime.datetime.now()
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"qq-bot-listener-{now:%Y-%m-%d}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{now:%Y-%m-%d %H:%M:%S}] {msg}\n")


def sig_headers(method: str, path: str) -> dict:
    """HMAC v1 签名（canonical: METHOD\nPATH\nTS\nNONCE\nSHA256(空体)）。"""
    ts = str(int(time.time()))
    nonce = os.urandom(8).hex()
    canonical = "\n".join([method, path, ts, nonce,
                           hashlib.sha256(b"").hexdigest()]).encode("utf-8")
    sig = hmac_mod.new(GATEWAY_SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {"X-Noti-Signature": "v1=" + sig, "X-Noti-Timestamp": ts,
            "X-Noti-Nonce": nonce, "X-Noti-Key-Id": KEY_ID}


def find_key(obj, key):
    """递归查找 JSON 字段（交互事件 payload 结构随场景变化，防御性解析）。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def _write_latest(name: str, value: str) -> None:
    """把最近的按钮点击/消息写到文件（毫秒时间戳\t内容），供测试脚本读取。"""
    try:
        with open(os.path.join(ZDIR, name), "w", encoding="utf-8") as f:
            f.write(f"{int(time.time() * 1000)}\t{value}")
    except Exception as e:
        log(f"写 {name} 失败: {e!r}")


def handle_event(t: str, d: dict) -> None:
    if t == "INTERACTION_CREATE":
        log(f"交互事件: {json.dumps(d, ensure_ascii=False)[:300]}")
        button_data = str(find_key(d, "button_data") or "")
        _write_latest(".qq-click-latest", button_data)
        if ":" in button_data:
            ans, rid = button_data.split(":", 1)
            if ans in ("allow", "deny") and RID_RE.match(rid):
                with open(os.path.join(ZDIR, f".qq-answer-{rid}"), "w",
                          encoding="utf-8") as f:
                    f.write(ans)
                log(f"按钮点击 -> {ans} rid={rid}")
        # 交互回应（PUT /interactions/{id}）由网关 qqws 自动完成
    elif t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
        content = str(d.get("content") or "").strip()
        _write_latest(".qq-msg-latest", content)
        log(f"收到 {t}: {content[:100]}")


def run_sse() -> None:
    """订阅网关 SSE 事件流；断开自动重连。"""
    client = httpx.Client(timeout=httpx.Timeout(10, read=60), trust_env=False)
    attempt = 0
    while True:
        try:
            with client.stream("GET", GATEWAY_URL + "/stream?types=qq_event",
                               headers={**sig_headers("GET", "/stream"),
                                        "accept": "text/event-stream"}) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"SSE HTTP {r.status_code}")
                log("SSE 已连接")
                event_type = None
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: ") and event_type:
                        try:
                            m = json.loads(line[6:])
                        except Exception as e:
                            log(f"SSE data 解析失败: {e!r}")
                            continue
                        try:
                            handle_event(m.get("event") or event_type, m.get("data") or {})
                        except Exception as e:
                            log(f"事件处理异常: {e!r}")
                log("SSE 流结束")
        except Exception as e:
            log(f"SSE 异常: {e!r}")
        attempt += 1
        wait = min(60.0, 3.0 * (2 ** min(attempt, 5)))
        log(f"{wait:.0f}s 后重连（第 {attempt} 次）")
        time.sleep(wait)


def run_ping() -> None:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"pong")

        def log_message(self, *args):
            pass

    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    S(("127.0.0.1", PING_PORT), H).serve_forever()


if __name__ == "__main__":
    os.makedirs(ZDIR, exist_ok=True)
    log("listener 启动（SSE 订阅模式，零 QQ 直连）")
    threading.Thread(target=run_ping, daemon=True).start()
    run_sse()
