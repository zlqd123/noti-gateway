"""qqws 入站事件广播端到端测试：假 QQ WS → hub → SSE qq_event + 交互自动回应。

运行：python tests/test_qq_events.py
不需要真实 QQ 凭证，也不需要联网。

覆盖：
  [1] qqws 连上假 QQ WS（Identify/READY/心跳）
  [2] C2C_MESSAGE_CREATE dispatch → SSE types=qq_event 收到 {"event": ..., "data": ...}
  [3] INTERACTION_CREATE dispatch → SSE 收到 + 假 QQ API 收到 PUT /interactions/{id} 自动回应
  [4] GROUP_AT_MESSAGE_CREATE dispatch → SSE 收到
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
from pathlib import Path

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KEY_ID = "qqev-test"
SECRET = "qqev-test-secret-do-not-use-3333333333333333333333333333"
PORT = 10111        # 网关
QQ_PORT = 10112     # 假 QQ HTTP API（token/interactions/gateway）
QQ_WS_PORT = 10113  # 假 QQ WS 网关
QQ_BASE = f"http://127.0.0.1:{QQ_PORT}"

STATE = {
    "ws": None,
    "interactions": [],     # PUT /interactions/{id} 记录
    "sse": [],              # SSE 收到的 qq_event
    "ws_open": False,
}


def _sign(method: str, path: str, body: bytes = b"") -> dict:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    canonical = "\n".join([method.upper(), path, ts, nonce,
                           hashlib.sha256(body).hexdigest()]).encode("utf-8")
    sig = "v1=" + hmac.new(SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {"X-Noti-Signature": sig, "X-Noti-Timestamp": ts,
            "X-Noti-Nonce": nonce, "X-Noti-Key-Id": KEY_ID}


def build_fake_qq_api() -> FastAPI:
    app = FastAPI()

    @app.post("/app/getAppAccessToken")
    async def token():
        return {"access_token": "FAKE", "expires_in": 7200}

    @app.get("/gateway")
    async def gateway():
        return {"url": f"ws://127.0.0.1:{QQ_WS_PORT}/qqws"}

    @app.put("/interactions/{iid}")
    async def interactions(iid: str, request: Request):
        STATE["interactions"].append({"id": iid, **(await request.json())})
        return {"code": 0}

    return app


async def fake_qq_ws(ws) -> None:
    """假 QQ WS 网关：Hello → Identify/Resume → 心跳应答；注入 dispatch 用 STATE["ws"]."""
    STATE["ws"] = ws
    STATE["ws_open"] = True
    await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
    try:
        while True:
            m = json.loads(await ws.recv())
            if m.get("op") == 2:
                await ws.send(json.dumps({"op": 0, "s": 1, "t": "READY",
                                          "d": {"session_id": "SID1", "user": {"username": "fake"}}}))
            elif m.get("op") == 1:
                await ws.send(json.dumps({"op": 11}))
    except Exception:
        pass
    finally:
        STATE["ws_open"] = False


async def inject_dispatch(t: str, d: dict) -> None:
    """向当前连接的假 QQ WS 注入一条 dispatch 事件。"""
    ws = STATE.get("ws")
    if ws is None:
        raise RuntimeError("fake ws not connected")
    STATE["seq"] = STATE.get("seq", 1) + 1
    await ws.send(json.dumps({"op": 0, "s": STATE["seq"], "t": t, "d": d}))


def make_config(keys_path: str) -> str:
    return f"""
server: {{ host: 127.0.0.1, port: {PORT} }}
auth: {{ keys_file: "{keys_path}" }}
dedup: {{ enabled: false }}
dispatch: {{ poll_interval: 0.1, concurrency: 4 }}
qq: {{ app_id: "T", client_secret: "S", api_base: "http://127.0.0.1:{QQ_PORT}" }}
qqws: {{ enabled: true }}
ingest: {{ sse_keepalive: 0.3, sse_buffer: 128 }}

targets:
  - id: qq:default
    type: qq
    user_openid: "u1"

routes:
  - source: ["*"]
    targets: [qq:default]

plugins:
  qqws: {{ enabled: true }}
  reminders: {{ enabled: false }}
  chatdownload: {{ enabled: false }}
  webchat: {{ enabled: false }}
  agents: {{ enabled: false }}
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


async def wait_until(cond, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return bool(cond())


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="noti-qqev-"))
    keys_path = tmp / "keys.conf"
    keys_path.write_text(f"{KEY_ID} {SECRET} kind=hmac source=qqev\n", encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(make_config(str(keys_path).replace("\\", "/")), encoding="utf-8")
    os.environ["NOTI_DB"] = str(tmp / "test.db")

    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    app = create_app(load_config(str(cfg_path)))
    async with websockets.serve(fake_qq_ws, "127.0.0.1", QQ_WS_PORT):
        servers = [
            uvicorn.Server(uvicorn.Config(build_fake_qq_api(), host="127.0.0.1", port=QQ_PORT, log_level="error")),
            uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")),
        ]
        tasks = [asyncio.create_task(s.serve()) for s in servers]

        base = f"http://127.0.0.1:{PORT}"
        ok = await wait_up(f"{base}/_health")
        failures = []

        def check(cond: bool, name: str) -> None:
            print(f"  {'PASS' if cond else 'FAIL'}  {name}")
            if not cond:
                failures.append(name)

        check(ok, "网关启动")
        check(await wait_until(lambda: STATE["ws_open"]), "qqws 连上假 QQ WS")

        async with httpx.AsyncClient(timeout=30) as c:
            health = (await c.get(f"{base}/_health")).json()
            check(health.get("qqws") is True, "/_health qqws=true")

            print("\n[1] SSE types=qq_event 订阅 + 事件广播")
            sse_lines: list[str] = []
            events: list[dict] = []

            async def sse_reader():
                try:
                    hdrs = _sign("GET", "/stream")
                    async with c.stream("GET", f"{base}/stream?types=qq_event",
                                        headers={**hdrs, "accept": "text/event-stream"}) as r:
                        async for line in r.aiter_lines():
                            sse_lines.append(line)
                            if line.startswith("data: "):
                                try:
                                    events.append(json.loads(line[6:]))
                                except Exception:
                                    pass
                except Exception as e:
                    import traceback
                    print("SSE-READER-ERR:", repr(e), chr(10) + traceback.format_exc())

            reader_task = asyncio.create_task(sse_reader())
            await asyncio.sleep(1.0)  # 等 SSE 建立并收到 : connected

            await inject_dispatch("C2C_MESSAGE_CREATE", {
                "id": "M1", "content": "hello", "author": {"user_openid": "u1"}})
            check(await wait_until(lambda: any(e.get("event") == "C2C_MESSAGE_CREATE" for e in events)),
                  "SSE 收到 C2C_MESSAGE_CREATE qq_event")

            print("\n[2] INTERACTION_CREATE → SSE + 自动回应")
            await inject_dispatch("INTERACTION_CREATE", {
                "id": "IT1", "button_data": "allow:rid123", "author": {"user_openid": "u1"}})
            check(await wait_until(lambda: any(e.get("event") == "INTERACTION_CREATE" for e in events)),
                  "SSE 收到 INTERACTION_CREATE qq_event")
            check(await wait_until(lambda: any(i["id"] == "IT1" and i.get("code") == 0
                                               for i in STATE["interactions"])),
                  "假 QQ API 收到 PUT /interactions/IT1 code=0")

            print("\n[3] GROUP_AT_MESSAGE_CREATE → SSE")
            await inject_dispatch("GROUP_AT_MESSAGE_CREATE", {
                "id": "G1", "content": "group msg", "author": {"member_openid": "g1"}})
            check(await wait_until(lambda: any(e.get("event") == "GROUP_AT_MESSAGE_CREATE" for e in events)),
                  "SSE 收到 GROUP_AT_MESSAGE_CREATE qq_event")

            ev = next(e for e in events if e.get("event") == "C2C_MESSAGE_CREATE")
            check(ev.get("data", {}).get("content") == "hello" and ev.get("bot") == "",
                  "qq_event 结构 {event, bot, data} 原样")

        for s in servers:
            s.should_exit = True
        try:
            reader_task.cancel()
        except Exception:
            pass
        await asyncio.gather(*tasks, reader_task, return_exceptions=True)

    print("\n" + "=" * 54)
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
