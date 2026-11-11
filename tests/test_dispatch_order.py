"""派发语义回归：纯文本「到了就发」不被文件上传堵住；同目标富媒体严格 FIFO 保序。

场景（同一目标 qq:default，mock QQ 每个接口 sleep 模拟慢上传链路）：
  A = 富媒体事件（上传链路 4 个接口 × 0.15s，> 0.5s 才发出文件消息）
  B = 纯文本通知（在 A 投递途中提交）
  C、D = 富媒体事件（紧跟 B 提交）
断言：
  [1] B 的消息早于 A 的文件消息送达（任务完成通知不被文件卡）
  [2] A/C/D 的文件消息严格按提交顺序送达（分卷 1/3 -> 2/3 -> 3/3 不乱序）
  [3] 4 条事件全部 done、无死信

用法：python tests\test_dispatch_order.py
"""

from __future__ import annotations

import asyncio
import base64
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
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KEY_ID = "order-test"
SECRET = "order-test-secret-do-not-use-000000000000000000000000000"
PORT = 10103
QQ_PORT = 10104

PNG_B64 = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c626001000000ffff03000006000557bfabd4000000"
    "0049454e44ae426082")).decode()

# 消息送达顺序：text:<文本> / media:<file_info>
ORDER: list[str] = []


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


async def _post(c: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return await c.post(url, content=body,
                        headers={**_sign("POST", "/n/x", body),
                                 "content-type": "application/json"})


def build_mock_qq() -> FastAPI:
    app = FastAPI()
    sl = 0.15  # 每个上传接口 0.15s，制造"文件慢、文本快"的窗口

    @app.post("/app/getAppAccessToken")
    async def token():
        return {"access_token": "FAKE", "expires_in": 7200}

    @app.post("/v2/users/{uid}/messages")
    async def messages(uid: str, request: Request):
        await asyncio.sleep(0.05)
        body = await request.json()
        if body.get("msg_type") == 7:
            ORDER.append(f"media:{body.get('media', {}).get('file_info', '')}")
        else:
            content = body.get("content", "")
            try:
                txt = json.loads(content).get("content", "")
            except Exception:
                txt = str(content)
            ORDER.append(f"text:{txt}")
        return {"id": f"M{len(ORDER)}"}

    @app.post("/v2/users/{uid}/upload_prepare")
    async def prep(uid: str, request: Request):
        await asyncio.sleep(sl)
        body = await request.json()
        return {
            "upload_id": f"up-{body.get('file_name', '')}",
            "block_size": str(int(body["file_size"])),
            "parts": [{"index": 1, "presigned_url": f"http://127.0.0.1:{QQ_PORT}/cos/x"}],
        }

    @app.put("/cos/x")
    async def cos(request: Request):
        await asyncio.sleep(sl)
        await request.body()
        return {"ok": True}

    @app.post("/v2/users/{uid}/upload_part_finish")
    async def fin(uid: str, request: Request):
        await asyncio.sleep(sl)
        await request.json()
        return {}

    @app.post("/v2/users/{uid}/files")
    async def files(uid: str, request: Request):
        await asyncio.sleep(sl)
        body = await request.json()
        return {"file_info": f"FIC:{body.get('file_name', '')}", "ttl": 86400, "id": "m"}

    return app


def make_config(keys_path: str) -> str:
    return f"""
server: {{ host: 127.0.0.1, port: {PORT} }}
auth: {{ keys_file: "{keys_path}" }}
limits: {{ per_source_per_minute: 600, per_target_per_minute: 600, per_target_per_day: 100000 }}
dedup: {{ enabled: false }}
dispatch: {{ poll_interval: 0.05, concurrency: 4, max_attempts: 3, retry_base: 0.2, retry_max: 1.0 }}
qq: {{ app_id: "T", client_secret: "S", api_base: "http://127.0.0.1:{QQ_PORT}" }}

targets:
  - id: qq:default
    type: qq
    user_openid: "u1"
    rate_per_minute: 600

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


async def wait_until(cond, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="noti-order-"))
    keys_path = tmp / "keys.conf"
    keys_path.write_text(f"{KEY_ID} {SECRET} kind=hmac source=order-test\n", encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(make_config(str(keys_path).replace("\\", "/")), encoding="utf-8")
    os.environ["NOTI_DB"] = str(tmp / "order.db")

    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    app = create_app(load_config(str(cfg_path)))
    servers = [
        uvicorn.Server(uvicorn.Config(build_mock_qq(), host="127.0.0.1", port=QQ_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")),
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]

    base = f"http://127.0.0.1:{PORT}"
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
        print("\n[1] 先提交慢文件 A")
        r = await _post(c, f"{base}/n/x", {
            "title": "A-文件1", "image": {"b64": PNG_B64, "mime": "image/png", "name": "1.png"}})
        check(r.status_code == 202, f"A 受理 (实际 {r.status_code})")
        await asyncio.sleep(0.15)  # A 进入上传链路（约 0.7s 才发完）

        print("[2] A 上传途中提交：纯文本 B + 文件 C/D")
        r = await _post(c, f"{base}/n/x", {"body": "B-通知内容"})
        check(r.status_code == 202, f"B 受理 (实际 {r.status_code})")
        await _post(c, f"{base}/n/x", {
            "title": "C-文件2", "image": {"b64": PNG_B64, "mime": "image/png", "name": "2.png"}})
        await _post(c, f"{base}/n/x", {
            "title": "D-文件3", "image": {"b64": PNG_B64, "mime": "image/png", "name": "3.png"}})

        print("[3] 断言送达顺序")
        check(await wait_until(lambda: len(ORDER) >= 7, timeout=20), f"7 条消息送达 (实际 {len(ORDER)})")

        def find(prefix: str, contains: str = "") -> int:
            for i, k in enumerate(ORDER):
                if k.startswith(prefix) and contains in k:
                    return i
            return -1

        i_b = find("text:", "B-通知")
        i_a = find("media:", "FIC:1.png")
        i_c = find("media:", "FIC:2.png")
        i_d = find("media:", "FIC:3.png")
        check(i_b >= 0 and i_a >= 0, f"B 与 A 的文件消息都送达 (B#{i_b} A#{i_a})")
        check(0 <= i_b < i_a, f"通知不被文件卡：B#{i_b} 早于 A文件#{i_a}")
        check(0 <= i_a < i_c < i_d, f"文件严格按提交顺序：A#{i_a} < C#{i_c} < D#{i_d}")

        st = (await c.get(f"{base}/_stats", headers=_sign("GET", "/_stats", b""))).json()
        check(st["counters"]["sent"] == 4, f"4 条事件全部发送 (实际 {st['counters']['sent']})")
        check(st["counters"]["dead"] == 0, "无死信")
        print(f"\n  送达顺序: {ORDER}")

    for t in tasks:
        t.cancel()
    if failures:
        print(f"\nFAILED: {len(failures)} 项")
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
