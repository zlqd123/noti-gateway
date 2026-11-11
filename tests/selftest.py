"""端到端自测：起一个假的 QQ API + 假的下游 WebSocket，验证全链路。

运行：python tests/selftest.py
不需要真实 QQ 凭证，也不需要联网。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试专用密钥（写进临时 keys.conf；真实密钥只存在于本地/服务器 keys.conf，绝不入库）
KEYS: dict[str, tuple[str, str]] = {
    "ci": ("selftest-ci", "selftest-ci-secret-do-not-use-0000000000000000000000"),
    "other": ("selftest-other", "selftest-other-secret-do-not-use-000000000000000000"),
}
PORT = 10087              # 网关
QQ_PORT = 10088           # 假 QQ API
WS_PORT = 10089           # 假下游 WebSocket

received_ws: list[dict] = []


def _sign(source: str, method: str, path: str, body: bytes) -> dict:
    """按 HMAC v1 对请求签名（source 决定用哪把钥匙及其归属）。"""
    key_id, secret = KEYS[source]
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_hex = hashlib.sha256(body or b"").hexdigest()
    canonical = "\n".join([method.upper(), path, ts, nonce, body_hex]).encode("utf-8")
    sig = "v1=" + hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {
        "X-Noti-Signature": sig,
        "X-Noti-Timestamp": ts,
        "X-Noti-Nonce": nonce,
        "X-Noti-Key-Id": key_id,
        "X-Source": source,
    }


def _ws_url(source: str) -> str:
    """入站 WS：签名头走查询串（浏览器式），canonical 为 GET /ws 空体。"""
    key_id, secret = KEYS[source]
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_hex = hashlib.sha256(b"").hexdigest()
    canonical = "\n".join(["GET", "/ws", ts, nonce, body_hex]).encode("utf-8")
    sig = "v1=" + hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    q = urllib.parse.urlencode({"sig": sig, "ts": ts, "nonce": nonce,
                                "key_id": key_id, "source": source})
    return f"ws://127.0.0.1:{PORT}/ws?{q}"


async def _post_json(c: httpx.AsyncClient, url: str, payload: dict, source: str = "ci") -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = urllib.parse.urlparse(url).path
    return await c.post(url, content=body,
                        headers={**_sign(source, "POST", path, body),
                                 "content-type": "application/json"})


# --------------------------------------------------------------------------
# Mock：QQ 官方机器人 API
# --------------------------------------------------------------------------
def build_mock_qq(fail_first: int = 0) -> FastAPI:
    app = FastAPI()
    state = {"token_calls": 0, "sent": 0, "fail_left": fail_first}

    @app.get("/_state")
    async def state_ep():
        return state

    @app.post("/app/getAppAccessToken")
    async def token():
        state["token_calls"] += 1
        return {"access_token": "FAKE_TOKEN", "expires_in": 7200}

    @app.post("/v2/groups/{gid}/messages")
    async def send(gid: str, request: Request):
        auth = request.headers.get("authorization", "")
        body = await request.json()
        if not auth.startswith("QQBot "):
            # 注意：QQ 的失败也是 HTTP 200，错误在 body.code
            return {"code": 10002, "message": "missing token"}
        if state["fail_left"] > 0:
            state["fail_left"] -= 1
            return {"code": 100001, "message": "Too many requests"}
        state["sent"] += 1
        return {"id": f"MSG{state['sent']}", "content": body.get("content", "")}

    return app


# --------------------------------------------------------------------------
# Mock：下游 WebSocket（模拟 NapCat / OneBot）
# --------------------------------------------------------------------------
def build_mock_ws() -> FastAPI:
    app = FastAPI()

    @app.get("/_received")
    async def got():
        return {"count": len(received_ws), "items": received_ws}

    @app.websocket("/onebot")
    async def onebot(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                received_ws.append(msg)
                await ws.send_text(json.dumps({
                    "status": "ok", "retcode": 0, "echo": msg.get("echo"),
                    "data": {"message_id": len(received_ws)},
                }))
        except WebSocketDisconnect:
            pass

    return app


def make_config(keys_path: str) -> str:
    return f"""
server: {{ host: 127.0.0.1, port: {PORT} }}
auth: {{ keys_file: "{keys_path}" }}
limits: {{ per_source_per_minute: 60, per_target_per_minute: 30, per_target_per_day: 1000 }}
dedup: {{ enabled: true, window_seconds: 60 }}
dispatch: {{ poll_interval: 0.2, concurrency: 4, max_attempts: 3 }}
qq: {{ app_id: "TEST", client_secret: "SECRET", api_base: "http://127.0.0.1:{QQ_PORT}" }}

targets:
  - id: qq:default
    type: qq
    group_openid: "test_group_openid"
    template: "{{{{ title }}}}|{{{{ body }}}}"
  - id: hook:echo
    type: webhook
    url: "http://127.0.0.1:{QQ_PORT}/_state"
    method: GET
  - id: ws:onebot
    type: ws
    url: "ws://127.0.0.1:{WS_PORT}/onebot"
    pool: 2
    ack_timeout: 5
    template: '{{"action":"send_group_msg","params":{{"message":"{{{{ title }}}}"}}}}'

routes:
  - source: ci
    level: [error]
    targets: [qq:default, ws:onebot]
  - source: ["*"]
    targets: [qq:default]
"""


async def wait_up(url: str, tries: int = 80) -> bool:
    async with httpx.AsyncClient(timeout=1) as c:
        for _ in range(tries):
            try:
                await c.get(url)
                return True
            except Exception:
                await asyncio.sleep(0.25)
    return False


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="noti-selftest-"))
    keys_path = tmp / "keys.conf"
    keys_path.write_text(
        "".join(f"{kid} {sec} kind=hmac source={src}\n" for src, (kid, sec) in KEYS.items()),
        encoding="utf-8",
    )
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(make_config(str(keys_path).replace("\\", "/")), encoding="utf-8")
    os.environ["NOTI_DB"] = str(tmp / "test.db")
    os.environ["NOTI_CONFIG"] = str(cfg_path)

    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    app = create_app(load_config(str(cfg_path)))

    servers = [
        uvicorn.Server(uvicorn.Config(build_mock_qq(), host="127.0.0.1", port=QQ_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(build_mock_ws(), host="127.0.0.1", port=WS_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")),
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]

    base = f"http://127.0.0.1:{PORT}"
    if not await wait_up(f"{base}/_health"):
        print("FAIL: 网关未启动")
        for t in tasks:
            t.cancel()
        return 1
    await asyncio.sleep(1.2)  # 等 WS 连接池建立

    failures: list[str] = []

    def check(cond: bool, name: str) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    async with httpx.AsyncClient(timeout=15) as c:
        print("\n[1] 鉴权")
        check((await c.post(f"{base}/n/ci?token=wrong", content="x")).status_code == 401, "旧式 token 调用 -> 401")
        check((await c.post(f"{base}/n/ci", content="x")).status_code == 401, "无签名 -> 401")
        check((await c.post(f"{base}/n/ci", content="x", headers={
            "X-Noti-Signature": "v1=00", "X-Noti-Timestamp": str(int(time.time())),
            "X-Noti-Nonce": "bad-1"})).status_code == 401, "坏签名 -> 401")

        print("\n[2] 异步入队 + 后台投递")
        r = await _post_json(c, f"{base}/n/ci?title=构建失败&body=main", {"level": "error"}, "ci")
        check(r.status_code == 202, f"异步返回 202 (实际 {r.status_code})")
        ev = r.json().get("event_id")
        await asyncio.sleep(1.5)

        st = (await c.get(f"{base}/_stats", headers=_sign("ci", "GET", "/_stats", b""))).json()
        check(st["counters"]["sent"] >= 2, f"投递 2 个 target (实际 sent={st['counters']['sent']})")
        check(st["counters"]["dead"] == 0, "无死信")

        rec = (await c.get(f"{base}/_receipt/{ev}", headers=_sign("ci", "GET", f"/_receipt/{ev}", b""))).json()
        check(len(rec["results"]) == 2, "回执 2 条")
        check(all(x["state"] == "done" for x in rec["results"]), "回执全部 done")

        qs = (await c.get(f"http://127.0.0.1:{QQ_PORT}/_state")).json()
        check(qs["sent"] >= 1, f"QQ mock 收到消息 (实际 {qs['sent']})")
        check(qs["token_calls"] >= 1, "access_token 已获取")

        print("\n[3] WebSocket 连接池")
        got = (await c.get(f"http://127.0.0.1:{WS_PORT}/_received")).json()
        check(got["count"] >= 1, f"出站 WS 收到消息 (实际 {got['count']})")
        check(bool(got["items"]) and got["items"][0].get("action") == "send_group_msg", "WS 模板渲染正确")
        st = (await c.get(f"{base}/_stats", headers=_sign("ci", "GET", "/_stats", b""))).json()
        conns = st["targets"]["ws:onebot"]["connections"]
        check(len(conns) == 2, f"同时维持 2 条连接 (实际 {len(conns)})")
        check(all(x["state"] == "open" for x in conns), "连接均为 open")
        check(sum(x["sent"] for x in conns) >= 1, "连接有发送计数")

        print("\n[4] 同步投递 ?wait=1")
        r = await _post_json(c, f"{base}/n/ci?wait=1&title=同步测试",
                             {"level": "error", "body": "hi"}, "ci")
        check(r.status_code == 200, f"同步返回 200 (实际 {r.status_code})")
        js = r.json()
        check(js.get("results", {}).get("qq:default", {}).get("ok") is True, "QQ 同步回执成功")
        check(js.get("results", {}).get("ws:onebot", {}).get("ok") is True, "WS 同步回执成功")

        print("\n[5] 聚合去重")
        r1 = await _post_json(c, f"{base}/n/ci", {"level": "error", "title": "重复标题"}, "ci")
        r2 = await _post_json(c, f"{base}/n/ci", {"level": "error", "title": "重复标题"}, "ci")
        check(r1.json().get("suppressed") is not True, "第一条正常入队")
        check(r2.json().get("suppressed") is True, "第二条被抑制")

        print("\n[6] 路由匹配")
        r = await _post_json(c, f"{base}/n/x", {"level": "info", "title": "t"}, "other")
        await asyncio.sleep(1.0)
        ev2 = r.json().get("event_id")
        rec2 = (await c.get(f"{base}/_receipt/{ev2}", headers=_sign("other", "GET", f"/_receipt/{ev2}", b""))).json()
        ids = [x["target_id"] for x in rec2["results"]]
        check(ids == ["qq:default"], f"非 ci 来源只走兜底路由 (实际 {ids})")

        print("\n[7] 入站格式兼容")
        body_txt = "纯文本第一行\n第二行".encode("utf-8")
        r = await c.post(f"{base}/n/ci", content=body_txt,
                         headers={**_sign("ci", "POST", "/n/ci", body_txt),
                                  "content-type": "text/plain"})
        check(r.status_code in (200, 202), f"text/plain 接受 (实际 {r.status_code})")
        body_form = urllib.parse.urlencode({"title": "表单", "level": "error"}).encode("ascii")
        r = await c.post(f"{base}/n/ci", content=body_form,
                         headers={**_sign("ci", "POST", "/n/ci", body_form),
                                  "content-type": "application/x-www-form-urlencoded"})
        check(r.status_code in (200, 202), "form-urlencoded 接受")
        from noti_gateway.ingest import _parse_body
        t_gbk = _parse_body("中文GBK回落测试".encode("gbk"), "text/plain")
        check(t_gbk == "中文GBK回落测试", f"GBK 字节回落解码 (实际 {t_gbk!r})")
        t_u8 = _parse_body("✅ UTF-8 优先".encode("utf-8"), "text/plain")
        check("✅" in t_u8 and "UTF-8" in t_u8, "严格 UTF-8 不受回落影响")

        print("\n[8] 入站 WebSocket")
        async with websockets.connect(_ws_url("ci")) as w:
            await w.send(json.dumps({"title": "来自WS", "level": "error", "body": "b"}))
            resp = json.loads(await asyncio.wait_for(w.recv(), timeout=5))
            check(resp.get("queued") is True, "WS 上行被接收并入队")
        try:
            bad_q = urllib.parse.urlencode({"sig": "v1=deadbeef",
                                            "ts": str(int(time.time())), "nonce": "bad-ws"})
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?{bad_q}") as w:
                await asyncio.wait_for(w.recv(), timeout=3)
            check(False, "WS 坏签名被拒绝")
        except Exception:
            check(True, "WS 坏签名被拒绝")

        print("\n[9] 观测接口")
        check((await c.get(f"{base}/_health")).json()["ok"] is True, "/_health ok")
        check((await c.get(f"{base}/_stats", headers=_sign("ci", "GET", "/_stats", b""))).status_code == 200, "/_stats 可用")
        check((await c.get(f"{base}/_failures", headers=_sign("ci", "GET", "/_failures", b""))).status_code == 200, "/_failures 受保护访问")
        check((await c.get(f"{base}/_failures")).status_code == 401, "/_failures 无签名 -> 401")

    for s in servers:
        s.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)

    print("\n" + "=" * 54)
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
