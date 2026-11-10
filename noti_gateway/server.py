"""FastAPI 服务：同端口同时提供 HTTP、curl、webhook、WebSocket 上行、SSE 订阅。"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .auth import AuthError, check_request, check_websocket
from .chatdownload import ChatDownloader
from .config import Config, load_config
from .dailylog import DailyFileHandler
from .dispatcher import Dispatcher, SubmitError
from .hub import Hub
from .ingest import MAX_BODY, build_notification, parse_http
from .store import Store
from .targets import QQTokenCache, build_targets

log = logging.getLogger("noti.server")

DB_PATH = os.environ.get("NOTI_DB", "noti-gateway.db")


def _truthy(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()

    logging.basicConfig(
        level=os.environ.get("NOTI_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 同时落文件：项目目录 log/noti-gateway-YYYY-MM-DD.log，一天一个文件
    # （相对路径基于工作目录；NOTI_LOG_DIR 可覆盖，置空则关闭文件日志）
    log_dir = os.environ.get("NOTI_LOG_DIR", "log")
    if log_dir:
        _fh = DailyFileHandler(log_dir)
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(_fh)

    app = FastAPI(title="noti-gateway", version="0.1.0", docs_url="/_docs", openapi_url=None)

    client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        timeout=httpx.Timeout(15.0),
    )
    # QQ access_token 共享缓存：发送通道与 chatdownload 复用同一个机器人
    tokens = QQTokenCache(cfg.qq, client)
    reg = build_targets(cfg, client, tokens)
    store = Store(DB_PATH, cfg.dispatch)
    hub = Hub(buffer=cfg.ingest.sse_buffer)
    disp = Dispatcher(cfg, store, reg, hub=hub)
    # 私聊消息归档（chatdownload）：复用同一 QQ 机器人，WS 收 C2C 消息落盘
    cd = (
        ChatDownloader(cfg.chatdownload, cfg.qq, tokens, client)
        if cfg.chatdownload.enabled and cfg.qq.app_id and cfg.qq.client_secret
        else None
    )

    app.state.cfg = cfg
    app.state.client = client
    app.state.registry = reg
    app.state.store = store
    app.state.dispatcher = disp
    app.state.hub = hub
    app.state.chatdownload = cd
    # 入站长连接注册表：WS 按连接 id 记账，SSE 只计数
    app.state.ws_conns: dict[int, dict[str, Any]] = {}
    app.state.ws_seq = itertools.count(1)
    app.state.sse = {"clients": 0}

    # ---------------- 生命周期 ----------------

    @app.on_event("startup")
    async def _startup() -> None:
        recovered = await store.recover()
        if recovered:
            log.info("recovered %d inflight task(s) from previous run", recovered)
        await reg.start_all()
        if cd is not None:
            await cd.start()
        app.state.worker = asyncio.create_task(disp.run_worker(), name="noti-worker")
        log.info(
            "noti-gateway ready  port=%s  targets=%s",
            cfg.server.port, ", ".join(reg.targets),
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        log.info("draining queue before shutdown ...")
        await disp.stop()
        worker = getattr(app.state, "worker", None)
        if worker is not None:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(worker, timeout=3)
        await disp.drain(timeout=10)
        await reg.close_all()
        if cd is not None:
            await cd.stop()
        await client.aclose()
        store.close()
        log.info("bye")

    # ---------------- 入站：HTTP / curl / webhook ----------------

    async def _handle(req: Request, route: str) -> JSONResponse:
        # 先单次读出原始请求体：HMAC 对原始字节签名，解析也复用它（stream 只能消费一次）
        try:
            raw = await req.body()
        except Exception:
            raw = b""
        if len(raw) > MAX_BODY:
            # 超限直接拒（不能截断，否则 HMAC 对不上客户端签名的完整体）
            return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            source = check_request(req, cfg.auth, raw)
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)

        n = await parse_http(req, source, route, raw)
        wait = _truthy(req.query_params.get("wait", "")) or _truthy(req.headers.get("x-wait", ""))
        try:
            result = await disp.submit(n, wait=wait)
        except SubmitError as e:
            return JSONResponse(
                {"error": str(e), "event_id": n.event_id, "level": n.level},
                status_code=e.status,
            )
        return JSONResponse(result, status_code=200 if wait or result.get("suppressed") else 202)

    @app.post("/n/{route:path}")
    async def post_notify(route: str, req: Request) -> JSONResponse:
        return await _handle(req, route)

    @app.get("/n/{route:path}")
    async def get_notify(route: str, req: Request) -> JSONResponse:
        return await _handle(req, route)

    # 兼容常见 webhook 习惯的裸路径
    @app.post("/hook/{route:path}")
    @app.post("/webhook/{route:path}")
    @app.post("/")
    async def alt_notify(route: str = "default", req: Request = None) -> JSONResponse:  # type: ignore
        if req is None:
            return PlainTextResponse("ok", status_code=200)
        return await _handle(req, route)

    # ---------------- 入站：WebSocket 上行（多客户端并发） ----------------

    async def _ws_send(ws: WebSocket, payload: dict) -> bool:
        """带超时的回包：慢/死客户端只影响自己，超时即断开。"""
        try:
            await asyncio.wait_for(
                ws.send_text(json.dumps(payload, ensure_ascii=False)),
                timeout=cfg.ingest.ws_send_timeout,
            )
            return True
        except Exception:
            return False

    @app.websocket("/ws")
    async def ws_in(ws: WebSocket) -> None:
        try:
            source = check_websocket(ws, cfg.auth)
        except AuthError as e:
            await ws.close(code=4401, reason=e.message[:120])
            return

        # 连接数上限（单事件循环内 check+注册之间无 await，无竞态）
        max_all = cfg.ingest.max_ws_clients
        max_src = cfg.ingest.max_ws_per_source
        if (max_all and len(app.state.ws_conns) >= max_all) or (
            max_src and sum(1 for v in app.state.ws_conns.values() if v["source"] == source) >= max_src
        ):
            await ws.accept()
            await _ws_send(ws, {"error": "too many websocket clients"})
            await ws.close(code=4429, reason="too many clients")
            return

        await ws.accept()
        cid = next(app.state.ws_seq)
        route = ws.query_params.get("route", "default")
        conn_wait = _truthy(ws.query_params.get("wait", ""))
        app.state.ws_conns[cid] = {
            "id": cid,
            "source": source,
            "route": route,
            "addr": f"{ws.client.host}:{ws.client.port}" if ws.client else "",
            "connected_at": time.time(),
            "sent": 0,
            "received": 0,
        }
        log.info("ws client #%d connected (source=%s, total=%d)", cid, source, len(app.state.ws_conns))

        try:
            while True:
                # 空闲超过 ws_keepalive 则发应用层心跳，维持 NAT/反向代理链路
                try:
                    if cfg.ingest.ws_keepalive > 0:
                        msg = await asyncio.wait_for(ws.receive(), timeout=cfg.ingest.ws_keepalive)
                    else:
                        msg = await ws.receive()
                except asyncio.TimeoutError:
                    if not await _ws_send(ws, {"type": "ping"}):
                        break
                    continue

                if msg.get("type") == "websocket.disconnect":
                    break
                raw = msg.get("text")
                if raw is None and msg.get("bytes") is not None:
                    raw = msg["bytes"].decode("utf-8", "replace")  # 二进制帧按文本容错
                if not raw or not raw.strip():
                    continue

                # 心跳控制帧不进入通知流水线
                low = raw.strip().lower()
                if low in ("ping", "pong"):
                    if low == "ping" and not await _ws_send(ws, {"type": "pong"}):
                        break
                    continue
                try:
                    payload: Any = json.loads(raw)
                except json.JSONDecodeError:
                    payload = raw

                want_wait = conn_wait
                if isinstance(payload, dict) and str(payload.get("type", "")).lower() in ("ping", "pong"):
                    if str(payload.get("type")).lower() == "ping" and not await _ws_send(ws, {"type": "pong"}):
                        break
                    continue

                # 批量：JSON 数组一次提交多条，逐条回执合并返回
                items = payload if isinstance(payload, list) else [payload]
                replies: list[dict] = []
                for item in items:
                    if isinstance(item, dict):
                        if item.pop("_wait", False):
                            want_wait = True
                    n = build_notification(item, source, route, meta={"via": "ws"})
                    try:
                        res = await disp.submit(n, wait=want_wait)
                        replies.append({"event_id": n.event_id, **res})
                    except SubmitError as e:
                        replies.append({"event_id": n.event_id, "error": str(e), "status": e.status})
                app.state.ws_conns[cid]["received"] += 1
                out = replies[0] if len(replies) == 1 else {"batch": replies}
                if not await _ws_send(ws, out):
                    break
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning("ws #%d error: %r", cid, e)
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
        finally:
            app.state.ws_conns.pop(cid, None)
            log.info("ws client #%d disconnected (source=%s, total=%d)",
                     cid, source, len(app.state.ws_conns))

    # ---------------- 订阅：SSE 事件流 ----------------

    @app.get("/stream")
    async def sse_stream(req: Request):
        try:
            check_request(req, cfg.auth)
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)

        types_wanted = {
            t.strip() for t in (req.query_params.get("types") or "submitted,receipt,dead,suppressed").split(",")
            if t.strip()
        }
        source_filter = req.query_params.get("source") or None
        q = hub.subscribe()
        app.state.sse["clients"] += 1

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        m = await asyncio.wait_for(q.get(), timeout=cfg.ingest.sse_keepalive)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if m["type"] not in types_wanted:
                        continue
                    if source_filter and m["data"].get("source") != source_filter:
                        continue
                    data = json.dumps(m["data"], ensure_ascii=False)
                    yield f"event: {m['type']}\nid: {m['seq']}\ndata: {data}\n\n"
            finally:
                hub.unsubscribe(q)
                app.state.sse["clients"] -= 1

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------------- 观测 ----------------

    @app.get("/_health")
    async def health() -> JSONResponse:
        out: dict[str, Any] = {
            "ok": True,
            "targets": {k: await v.health() for k, v in reg.targets.items()},
        }
        if cd is not None:
            out["chatdownload"] = cd.health()
        return JSONResponse(out)

    @app.get("/_stats")
    async def stats() -> JSONResponse:
        return JSONResponse(
            {
                "counters": disp.stats(),
                "queue": await store.stats(),
                "targets": reg.info(),
                "ingest": {
                    "ws_clients": len(app.state.ws_conns),
                    "ws": list(app.state.ws_conns.values()),
                    "sse_clients": app.state.sse["clients"],
                },
                "chatdownload": cd.info() if cd is not None else None,
            }
        )

    @app.get("/_receipt/{event_id}")
    async def receipt(event_id: str, req: Request) -> JSONResponse:
        try:
            check_request(req, cfg.auth)
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        return JSONResponse({"event_id": event_id, "results": await store.receipts(event_id)})

    @app.get("/_failures")
    async def failures(req: Request) -> JSONResponse:
        try:
            check_request(req, cfg.auth)
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        return JSONResponse({"dead_letters": await store.recent_failures(50)})

    return app


app = create_app() if os.environ.get("NOTI_EAGER_APP", "0") == "1" else None


def get_app() -> FastAPI:
    """uvicorn --factory 入口：只有真正启动时才构建 app。"""
    return app or create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "noti_gateway.server:get_app",
        factory=True,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=os.environ.get("NOTI_LOGLEVEL", "info").lower(),
        # 关闭前给足时间排空队列
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
