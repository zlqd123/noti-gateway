"""多连接接入测试：并发 WS 客户端、心跳、批量、_wait 同步、SSE 订阅流。

运行：python tests/test_connections.py
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
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试专用密钥（写进临时 keys.conf；真实密钥绝不入库）
KEY_ID = "conn-test"
SECRET = "conn-test-secret-do-not-use-0000000000000000000000000000"
PORT = 10097        # 网关
QQ_PORT = 10098     # 假 QQ API


def _sign(method: str, path: str, body: bytes) -> dict:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_hex = hashlib.sha256(body or b"").hexdigest()
    canonical = "\n".join([method.upper(), path, ts, nonce, body_hex]).encode("utf-8")
    sig = "v1=" + hmac.new(SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {
        "X-Noti-Signature": sig,
        "X-Noti-Timestamp": ts,
        "X-Noti-Nonce": nonce,
        "X-Noti-Key-Id": KEY_ID,
    }


def _ws_url(source: str) -> str:
    """入站 WS：签名头走查询串（浏览器式）；?source= 声明归属。"""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_hex = hashlib.sha256(b"").hexdigest()
    canonical = "\n".join(["GET", "/ws", ts, nonce, body_hex]).encode("utf-8")
    sig = "v1=" + hmac.new(SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    q = urllib.parse.urlencode({"sig": sig, "ts": ts, "nonce": nonce,
                                "key_id": KEY_ID, "source": source})
    return f"ws://127.0.0.1:{PORT}/ws?{q}"


def build_mock_qq() -> FastAPI:
    app = FastAPI()
    state = {"sent": 0, "titles": []}

    @app.get("/_state")
    async def state_ep():
        return state

    @app.post("/app/getAppAccessToken")
    async def token():
        return {"access_token": "FAKE", "expires_in": 7200}

    @app.post("/v2/users/{uid}/messages")
    async def send(uid: str, request: Request):
        body = await request.json()
        state["sent"] += 1
        state["titles"].append(body.get("content", ""))
        return {"id": f"MSG{state['sent']}"}

    return app


def make_config(keys_path: str) -> str:
    return f"""
server: {{ host: 127.0.0.1, port: {PORT} }}
auth: {{ keys_file: "{keys_path}" }}
limits: {{ per_source_per_minute: 120, per_target_per_minute: 60, per_target_per_day: 1000 }}
dedup: {{ enabled: false }}
dispatch: {{ poll_interval: 0.1, concurrency: 4 }}
qq: {{ app_id: "T", client_secret: "S", api_base: "http://127.0.0.1:{QQ_PORT}" }}
ingest: {{ max_ws_clients: 8, max_ws_per_source: 2, ws_keepalive: 0.5, ws_send_timeout: 5, sse_keepalive: 0.5, sse_buffer: 64 }}

targets:
  - id: qq:default
    type: qq
    user_openid: "u1"

routes:
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
    tmp = Path(tempfile.mkdtemp(prefix="noti-conn-"))
    keys_path = tmp / "keys.conf"
    keys_path.write_text(f"{KEY_ID} {SECRET} kind=hmac source=conn-test\n", encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(make_config(str(keys_path).replace("\\", "/")), encoding="utf-8")
    os.environ["NOTI_DB"] = str(tmp / "test.db")

    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    app = create_app(load_config(str(cfg_path)))
    servers = [
        uvicorn.Server(uvicorn.Config(build_mock_qq(), host="127.0.0.1", port=QQ_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")),
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]

    base = f"http://127.0.0.1:{PORT}"
    wsbase = f"ws://127.0.0.1:{PORT}/ws"
    if not await wait_up(f"{base}/_health"):
        print("FAIL: 网关未启动")
        for t in tasks:
            t.cancel()
        return 1

    failures: list[str] = []

    def check(cond: bool, name: str) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    async with httpx.AsyncClient(timeout=15) as c:
        print("\n[1] 多客户端并发互不串扰")
        async def client_session(source: str, title: str) -> tuple[str, str]:
            async with websockets.connect(_ws_url(source)) as w:
                await w.send(json.dumps({"title": title, "level": "info"}))
                resp = json.loads(await asyncio.wait_for(w.recv(), timeout=5))
                return source, resp.get("event_id", "")

        results = await asyncio.gather(
            client_session("svc-a", "A 的消息"),
            client_session("svc-b", "B 的消息"),
            client_session("svc-c", "C 的消息"),
        )
        ids = [r[1] for r in results]
        check(all(ids) and len(set(ids)) == 3, "3 个客户端各拿到独立 event_id")
        a_id = results[0][1]
        check(a_id and a_id not in ids[1:], "回执没有串到别的客户端")

        print("\n[2] 连接注册表与统计")
        async with websockets.connect(_ws_url("stat")) as w:
            st = (await c.get(f"{base}/_stats", headers=_sign("GET", "/_stats", b""))).json()
            check(st["ingest"]["ws_clients"] == 1, f"/_stats 可见 1 个在线 WS (实际 {st['ingest']['ws_clients']})")
            check(st["ingest"]["ws"][0]["source"] == "stat", "注册表记录了 source")
        st = (await c.get(f"{base}/_stats", headers=_sign("GET", "/_stats", b""))).json()
        check(st["ingest"]["ws_clients"] == 0, "断开后注册表清零")

        print("\n[3] 同 source 连接数上限")
        w1 = await websockets.connect(_ws_url("dup"))
        w2 = await websockets.connect(_ws_url("dup"))
        check(True, "同 source 第 2 条连接允许")
        try:
            w3 = await websockets.connect(_ws_url("dup"))
            first = await asyncio.wait_for(w3.recv(), timeout=5)
            check("too many" in first, "第 3 条连接收到拒绝消息")
            try:
                await asyncio.wait_for(w3.recv(), timeout=5)
                check(False, "第 3 条连接应被关闭")
            except websockets.exceptions.ConnectionClosed as e:
                check(e.rcvd is not None and e.rcvd.code == 4429, f"关闭码 4429 (实际 {e.rcvd.code if e.rcvd else '?'})")
        except Exception as ex:
            check(False, f"第 3 条连接应完成握手再被拒（{ex!r}）")
        await w1.close()
        await w2.close()

        print("\n[4] 服务端空闲心跳")
        async with websockets.connect(_ws_url("idle")) as w:
            got_ping = False
            for _ in range(6):
                msg = json.loads(await asyncio.wait_for(w.recv(), timeout=1.5))
                if msg.get("type") == "ping":
                    got_ping = True
                    break
            check(got_ping, "静默连接收到服务端 {type:ping}")

        print("\n[5] 客户端心跳与计数隔离")
        st0 = (await c.get(f"{base}/_stats", headers=_sign("GET", "/_stats", b""))).json()["counters"]["received"]
        async with websockets.connect(_ws_url("hb")) as w:
            await w.send("ping")
            pong = json.loads(await asyncio.wait_for(w.recv(), timeout=5))
            check(pong.get("type") == "pong", "文本 ping 收到 {type:pong}")
            await w.send(json.dumps({"type": "PING"}))
            pong2 = json.loads(await asyncio.wait_for(w.recv(), timeout=5))
            check(pong2.get("type") == "pong", "JSON ping 也按心跳处理")
        st1 = (await c.get(f"{base}/_stats", headers=_sign("GET", "/_stats", b""))).json()["counters"]["received"]
        check(st1 == st0, f"心跳不产生通知 (received {st0} -> {st1})")

        print("\n[6] 批量提交")
        qs0 = (await c.get(f"http://127.0.0.1:{QQ_PORT}/_state")).json()["sent"]
        async with websockets.connect(_ws_url("batch")) as w:
            await w.send(json.dumps([
                {"title": "批量1"},
                {"title": "批量2", "level": "warn"},
            ]))
            resp = json.loads(await asyncio.wait_for(w.recv(), timeout=5))
            check(isinstance(resp.get("batch"), list) and len(resp["batch"]) == 2, "数组提交返回 batch 2 条回执")
            check(all(r.get("queued") for r in resp["batch"]), "批量全部入队")
        await asyncio.sleep(1.0)
        qs1 = (await c.get(f"http://127.0.0.1:{QQ_PORT}/_state")).json()["sent"]
        check(qs1 - qs0 == 2, f"QQ mock 收到 2 条 (实际 {qs1 - qs0})")

        print("\n[7] WS 单条同步 _wait")
        async with websockets.connect(_ws_url("syncw")) as w:
            await w.send(json.dumps({"title": "同步WS", "_wait": True}))
            resp = json.loads(await asyncio.wait_for(w.recv(), timeout=10))
            res = resp.get("results", {}).get("qq:default", {})
            check(resp.get("queued") is False and res.get("ok") is True, "单条消息可要求同步回执")

        print("\n[8] 二进制帧容错")
        async with websockets.connect(_ws_url("bin")) as w:
            await w.send("二进制测试".encode("utf-8"))
            resp = json.loads(await asyncio.wait_for(w.recv(), timeout=5))
            check(resp.get("queued") is True, "二进制帧按 UTF-8 文本处理")

        print("\n[9] SSE 订阅流")
        got_submitted = got_receipt = None
        async with c.stream(
            "GET", f"{base}/stream",
            params={"types": "submitted,receipt"},
            headers=_sign("GET", "/stream", b""),
            timeout=httpx.Timeout(20),
        ) as resp_stream:
            check(resp_stream.status_code == 200, "SSE 建立 (200)")
            lines = resp_stream.aiter_lines()
            # 等到 ": connected" 确认订阅已生效再触发事件
            connected = False
            poster = None
            ev_name = None
            ev_data = None
            deadline = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < deadline:
                if poster is None and connected:
                    sse_body = json.dumps({"title": "SSE测试", "level": "info"}, ensure_ascii=False).encode("utf-8")
                    poster = asyncio.create_task(
                        c.post(f"{base}/n/sse", content=sse_body,
                               headers={**_sign("POST", "/n/sse", sse_body),
                                        "X-Source": "sse", "content-type": "application/json"})
                    )
                try:
                    line = await asyncio.wait_for(lines.__anext__(), timeout=2)
                except (StopAsyncIteration, asyncio.TimeoutError):
                    continue
                if line.startswith(":"):
                    if "connected" in line:
                        connected = True
                    continue
                if line.startswith("event:"):
                    ev_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and ev_name:
                    ev_data = json.loads(line.split(":", 1)[1])
                    if ev_name == "submitted" and ev_data.get("title") == "SSE测试":
                        got_submitted = ev_data
                    if ev_name == "receipt" and got_submitted and ev_data.get("event_id") == got_submitted["event_id"] and ev_data.get("ok"):
                        got_receipt = ev_data
                if got_submitted and got_receipt:
                    break
            if poster is not None:
                await poster
        check(got_submitted is not None, "SSE 收到 submitted 事件")
        check(got_receipt is not None, "SSE 收到匹配 event_id 的成功 receipt")
        await asyncio.sleep(0.3)
        st = (await c.get(f"{base}/_stats", headers=_sign("GET", "/_stats", b""))).json()
        check(st["ingest"]["sse_clients"] == 0, "SSE 断开后订阅计数归零")

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
