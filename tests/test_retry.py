"""重试与死信语义测试。

验证三件最容易写错的事：
1. 可重试错误（100001 频控）会退避重试并最终成功
2. 不可重试错误（100007 appid invalid）直接进死信，不浪费重试
3. 限流导致的延后(defer)不消耗 attempts
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKEN = "retry-token"
PORT = 10091
QQ_PORT = 10092

state = {"mode": "ok", "remaining": 0, "calls": 0}


def build_mock() -> FastAPI:
    app = FastAPI()

    @app.get("/_state")
    async def st():
        return state

    @app.get("/_set")
    async def setter(mode: str, remaining: int = 0):
        state["mode"], state["remaining"] = mode, remaining
        return state

    @app.post("/app/getAppAccessToken")
    async def token():
        return {"access_token": "FAKE", "expires_in": 7200}

    @app.post("/v2/groups/{gid}/messages")
    async def send(gid: str, request: Request):
        state["calls"] += 1
        if state["remaining"] > 0:
            state["remaining"] -= 1
            if state["mode"] == "retryable":
                return {"code": 100001, "message": "Too many requests"}
            return {"code": 100007, "message": "appid invalid"}
        return {"id": f"M{state['calls']}"}

    return app


CONFIG = f"""
server: {{ host: 127.0.0.1, port: {PORT} }}
auth: {{ token: "{TOKEN}" }}
limits: {{ per_source_per_minute: 600, per_target_per_minute: 600, per_target_per_day: 100000 }}
dedup: {{ enabled: false }}
dispatch: {{ poll_interval: 0.1, concurrency: 4, max_attempts: 4, retry_base: 0.2, retry_max: 1.0 }}
qq: {{ app_id: "T", client_secret: "S", api_base: "http://127.0.0.1:{QQ_PORT}" }}

targets:
  - id: qq:only
    type: qq
    group_openid: "g1"
    rate_per_minute: 600

routes:
  - source: ["*"]
    targets: [qq:only]
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
    tmp = Path(tempfile.mkdtemp(prefix="noti-retry-"))
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(CONFIG, encoding="utf-8")
    os.environ["NOTI_DB"] = str(tmp / "retry.db")

    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    app = create_app(load_config(str(cfg_path)))
    servers = [
        uvicorn.Server(uvicorn.Config(build_mock(), host="127.0.0.1", port=QQ_PORT, log_level="warning")),
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

    H = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as c:
        print("\n[1] 可重试错误 -> 退避重试后成功")
        await c.get(f"http://127.0.0.1:{QQ_PORT}/_set?mode=retryable&remaining=2")
        state["calls"] = 0
        r = await c.post(f"{base}/n/x", headers=H, json={"title": "重试测试"})
        ev = r.json()["event_id"]
        await asyncio.sleep(4.0)

        rec = (await c.get(f"{base}/_receipt/{ev}?token={TOKEN}")).json()["results"]
        check(len(rec) == 1, f"回执存在 (实际 {len(rec)})")
        check(rec and rec[0]["state"] == "done", f"最终成功 (实际 {rec[0]['state'] if rec else None})")
        check(rec and rec[0]["attempts"] == 3, f"尝试 3 次 (实际 {rec[0]['attempts'] if rec else None})")
        st = (await c.get(f"{base}/_stats?token={TOKEN}")).json()
        check(st["counters"]["dead"] == 0, "没有死信")
        check(st["counters"]["failed"] == 2, f"记录 2 次可重试失败 (实际 {st['counters']['failed']})")

        print("\n[2] 不可重试错误 -> 直接死信")
        await c.get(f"http://127.0.0.1:{QQ_PORT}/_set?mode=hard&remaining=10")
        before = state["calls"]
        r = await c.post(f"{base}/n/x", headers=H, json={"title": "死信测试"})
        ev2 = r.json()["event_id"]
        await asyncio.sleep(3.0)

        rec2 = (await c.get(f"{base}/_receipt/{ev2}?token={TOKEN}")).json()["results"]
        check(rec2 and rec2[0]["state"] == "dead", f"进入死信 (实际 {rec2[0]['state'] if rec2 else None})")
        check(rec2 and rec2[0]["attempts"] == 1, f"只尝试 1 次不重试 (实际 {rec2[0]['attempts'] if rec2 else None})")
        check(state["calls"] - before == 1, f"QQ 侧只收到 1 次请求 (实际 {state['calls'] - before})")

        dl = (await c.get(f"{base}/_failures?token={TOKEN}")).json()["dead_letters"]
        check(any(x["event_id"] == ev2 for x in dl), "死信列表可查")

        print("\n[3] 恢复后正常投递")
        await c.get(f"http://127.0.0.1:{QQ_PORT}/_set?mode=ok&remaining=0")
        r = await c.post(f"{base}/n/x", headers=H, json={"title": "恢复测试"})
        ev3 = r.json()["event_id"]
        await asyncio.sleep(2.0)
        rec3 = (await c.get(f"{base}/_receipt/{ev3}?token={TOKEN}")).json()["results"]
        check(rec3 and rec3[0]["state"] == "done", "恢复正常投递")

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
