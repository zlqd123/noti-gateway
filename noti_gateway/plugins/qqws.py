"""QQ 官方网关长连接插件（qqws）：全进程唯一的收消息入口。

一个 appid 只有一条事件流（webhook 回调地址仅一个、WS 会话按 shard 管理），
所以连接必须收口：本插件独占 WS 长连接（鉴权 / 心跳 / 重连 / Resume），
收到 dispatch 事件后原样广播给 ctx.event_listeners；其他插件都是纯消费者
（chatdownload 归档、reminders 指令……），彼此互不感知、可插拔。

连接归属：qqws.bot > chatdownload.bot > 第一个带凭据的 bot > 旧式全局 qq 段。
access_token 与发送侧共用同一份 QQTokenCache（Registry.token_caches）。

当前订阅 intents：C2C_MESSAGE_CREATE（单聊）。未来扩展群聊/按钮事件时
在此处加 intents 并广播新事件类型，下游插件各自按类型取用。

观测：/_health 的 ``qqws`` 段、/_stats 的 ``qqws`` 段（state/session/received/uptime）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import httpx

from ..config import QqwsCfg
from ..targets import QQTokenCache
from . import PluginMeta

try:  # websockets >= 14 的 asyncio 实现
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - 老版本兜底
    from websockets.client import connect as ws_connect  # type: ignore

log = logging.getLogger("noti.qqws")

# GROUP_AND_C2C_EVENT：单聊(C2C_MESSAGE_CREATE) + 群聊消息事件
QQ_INTENTS_C2C = 1 << 25


META = PluginMeta(name="qqws", desc="QQ 官方网关长连接（唯一收消息入口，广播事件流）", version="1.1.0")


class _Reconnect(Exception):
    """服务端要求重连（op7）或会话失效（op9）。"""


class QQGateway:
    """WS 网关连接状态机。生命周期随 app：start() 建任务，stop() 取消收尾。"""

    def __init__(self, cfg: QqwsCfg, bot_id: str, qq: Any, tokens: QQTokenCache,
                 client: httpx.AsyncClient, ctx: Any) -> None:
        self.cfg = cfg
        self.bot_id = bot_id
        self.qq = qq
        self.tokens = tokens
        self.client = client  # 复用网关共享 client（取 /gateway 地址用）
        self.ctx = ctx        # PluginCtx：事件广播

        self._task: asyncio.Task | None = None
        self._closing = False

        # WS 会话状态（Resume 用）
        self._session_id: str | None = None
        self._seq: int | None = None
        self._attempt = 0

        # 观测字段
        self.state = "idle"
        self.received = 0
        self.connected_at: float | None = None
        self.last_error = ""

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="qqws")
        log.info("qqws 连接启动 bot=%s", self.bot_id or "(旧式全局段)")

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._task = None
        log.info("qqws 已停止")

    def health(self) -> bool:
        return bool(self._task is not None and not self._task.done() and self.state == "open")

    def info(self) -> dict:
        return {
            "state": self.state,
            "bot": self.bot_id,
            "session_id": self._session_id,
            "received": self.received,
            "uptime_s": round(time.time() - self.connected_at, 1) if self.connected_at else 0,
            "last_error": self.last_error,
        }

    # ---------------- WS 网关 ----------------

    def _backoff(self) -> float:
        d = min(60.0, 2.0 * (2 ** min(self._attempt, 8)))
        self._attempt += 1
        return d * (0.5 + random.random() * 0.5)  # jitter，避免惊群

    async def _gateway_url(self) -> str:
        try:
            token = await self.tokens.get()
            r = await self.client.get(
                self.qq.api_base.rstrip("/") + "/gateway",
                headers={"Authorization": f"QQBot {token}"},
                timeout=15,
            )
            body = r.json() if r.status_code == 200 else {}
            if isinstance(body, dict) and body.get("url"):
                return str(body["url"])
        except Exception as e:
            log.debug("qqws 获取网关地址失败: %r", e)
        return "wss://api.bot.qq.com/websocket"

    def _ident(self, token: str) -> dict:
        if self._session_id and self._seq is not None:
            return {
                "op": 6,
                "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._seq},
            }
        return {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": QQ_INTENTS_C2C,
                "shard": [0, 1],
                "properties": {},
            },
        }

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": 1, "d": self._seq}))
        except asyncio.CancelledError:
            raise
        except Exception:
            return  # 连接已断开，退出即可；_run 负责重连

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self._session()
            except _Reconnect as e:
                log.info("qqws %s，立即重连", e)
                self._attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = repr(e)
                log.warning("qqws 网关异常: %r", e)
                await asyncio.sleep(self._backoff())
            finally:
                self.state = "down"
                self.connected_at = None

    async def _session(self) -> None:
        url = await self._gateway_url()
        token = await self.tokens.get()
        log.info("qqws 连接网关: %s", url)
        async with ws_connect(
            url,
            open_timeout=15,
            close_timeout=3,
            max_size=8 * 1024 * 1024,
            ping_interval=None,  # QQ 有自己的应用层心跳
        ) as ws:
            self.state = "open"
            self.connected_at = time.time()
            self.last_error = ""
            hb: asyncio.Task | None = None
            try:
                while not self._closing:
                    payload = json.loads(await ws.recv())
                    op = payload.get("op")

                    if op == 10:  # Hello
                        interval = (payload.get("d") or {}).get("heartbeat_interval", 45000) / 1000
                        await ws.send(json.dumps(self._ident(token)))
                        hb = asyncio.create_task(self._heartbeat(ws, interval))
                    elif op == 0:  # Dispatch
                        self._seq = payload.get("s") or self._seq
                        await self._dispatch(payload.get("t") or "", payload.get("d") or {})
                    elif op == 1:  # 服务端发起的心跳，立即应答
                        await ws.send(json.dumps({"op": 1, "d": self._seq}))
                    elif op == 7:
                        raise _Reconnect("服务端要求重连 (op7)")
                    elif op == 9:  # 会话失效：清掉 session 重新 identify
                        self._session_id = None
                        self._seq = None
                        raise _Reconnect("会话失效 (op9)，重新鉴权")
                    # op 11 = heartbeat ack，忽略
            finally:
                if hb:
                    hb.cancel()

    async def _dispatch(self, t: str, d: dict) -> None:
        if t == "READY":
            self._session_id = d.get("session_id")
            self._attempt = 0
            log.info("qqws READY session=%s user=%s",
                     self._session_id, (d.get("user") or {}).get("username"))
        elif t == "RESUMED":
            self._attempt = 0
            log.info("qqws RESUMED")
        elif t == "C2C_MESSAGE_CREATE":
            self.received += 1
            # 登记 msg_id：被动回复窗口判断用（回复带 msg_id 不占主动消息配额）
            self.ctx.remember_msg(d.get("id"),
                                  str((d.get("author") or {}).get("user_openid") or ""))
            await self.ctx.dispatch_event(t, d)


def setup(ctx: Any) -> None:
    """注册 qqws：解析连接归属的机器人凭据，挂启停钩子与观测段。"""
    cfg: QqwsCfg = ctx.cfg.qqws
    if not cfg.enabled:
        log.info("qqws 插件未启用（qqws.enabled=false）")
        return
    bot_id, qq = ctx.cfg.qqws_bot()
    cache = ctx.reg.token_caches.get(bot_id) if ctx.reg is not None else None
    if cache is None:
        cache = ctx.reg.token_caches.get("") if ctx.reg is not None else None
    if cache is None or not qq.app_id or not qq.client_secret:
        log.warning("qqws 无可用机器人凭据（bot=%s），连接不启动", bot_id or "(旧式全局段)")
        return
    gw = QQGateway(cfg, bot_id, qq, cache, ctx.client, ctx)
    ctx.on_startup.append(gw.start)
    ctx.on_shutdown.append(gw.stop)
    ctx.health_providers["qqws"] = gw.health
    ctx.stats_providers["qqws"] = gw.info
