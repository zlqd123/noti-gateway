"""WebChat REST API 与 WebSocket 服务（挂载到 NoneBot 主 FastAPI 应用）。

路由挂在主应用（PORT=28080）下 /webchat 前缀，不另起端口。
认证流程：
1. 浏览器提交 6 位 TOTP 动态码到 /webchat/api/login；
2. 校验通过签发无状态会话令牌（HMAC 签名，重启不失效）；
3. 后续所有 API/WS 请求带会话令牌（X-Webchat-Token 头 / ?token= 查询参数）。

其他安全策略与 nonebot_plugin_maestro 同思路：
- 带 Origin 的请求校验同源，挡 CSRF/DNS rebinding；
- 登录接口限速，防动态码在线爆破；
- 签名比较用常数时间，防时序侧信道。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import logging

log = logging.getLogger("noti.webchat")

from . import totp as totp_mod
from .store import ChatStore

STATIC_DIR = Path(__file__).parent / "static"

# 富媒体上传上限：官方分块上传通道可承载较大文件，这里限制面板单文件 100MB
MEDIA_MAX_BYTES = 100 * 1024 * 1024

# 发送实现由 __init__ 注入（依赖 QQ 适配器），本模块只管协议层
SendFunc = Callable[[str, str, str | None], Awaitable[dict[str, Any]]]
SendMediaFunc = Callable[[str, bytes, str, str, str, str | None], Awaitable[dict[str, Any]]]
StatusFunc = Callable[[], dict[str, bool]]
# 图片转存：url -> 本地缓存路径（下载失败/黑名单返回 None）
MediaFetchFunc = Callable[[str], Awaitable[Any]]
UrlAllowedFunc = Callable[[str], bool]

class PanelCtx:
    """单个面板实例的状态：注入实现 + 登录限速计数（实例作用域，非模块全局）。"""

    def __init__(
        self,
        store: ChatStore,
        secret: str,
        session_days: int,
        send: SendFunc,
        send_media: SendMediaFunc,
        status: StatusFunc,
        media_fetch: MediaFetchFunc,
        url_allowed: UrlAllowedFunc,
    ) -> None:
        self.store = store
        self.secret = secret
        self.session_days = session_days
        self.send = send
        self.send_media = send_media
        self.status = status
        self.media_fetch = media_fetch
        self.url_allowed = url_allowed
        # 登录限速：失败计数，连续失败后锁定一段时间
        self.login_fails = 0
        self.login_locked_until = 0.0


def _sniff_image(data: bytes) -> str:
    """按魔数嗅探图片类型，供代理响应设置 Content-Type。"""
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"

# 登录限速：连续失败后锁定一段时间（计数在 PanelCtx 实例里）
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SECONDS = 60


class SendRequest(BaseModel):
    """手动发送请求体。"""

    chat: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5000)
    reply_msg_id: str | None = Field(default=None, max_length=200)
    active: bool = False  # True = 主动发送（不带 msg_id）


class LoginRequest(BaseModel):
    """TOTP 登录请求体。"""

    code: str = Field(min_length=1, max_length=16)


def _check_origin(origin: str | None, host: str) -> None:
    if not origin:
        return  # 非浏览器客户端（curl 等）无 Origin，交给会话令牌兜底
    if urlparse(origin).netloc != host:
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")


class NoCacheStaticFiles(StaticFiles):
    """静态资源禁用启发式缓存：更新前端后刷新即生效，避免旧 JS 继续运行。"""

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def setup(
    app: FastAPI,
    store: ChatStore,
    secret: str,
    session_days: int,
    send: SendFunc,
    send_media: SendMediaFunc,
    status: StatusFunc,
    media_fetch: MediaFetchFunc,
    url_allowed: UrlAllowedFunc,
) -> None:
    """注册路由与静态资源；在插件加载期调用一次。

    所有状态收在 PanelCtx 实例里（闭包引用），不占模块全局——支持同一进程
    挂多个面板实例（测试/多 bot），互不串扰。
    """
    st = PanelCtx(store, secret, session_days, send, send_media, status,
                  media_fetch, url_allowed)

    def check_session(request: Request) -> None:
        supplied = (
            request.headers.get("X-Webchat-Token")
            or request.query_params.get("token")
            or ""
        )
        if not totp_mod.verify_session(st.secret, supplied, st.session_days):
            raise HTTPException(status_code=401, detail="会话无效或已过期，请重新登录")

    def auth_http(request: Request) -> None:
        _check_origin(request.headers.get("origin"), request.headers.get("host", ""))
        check_session(request)

    AUTH = Depends(auth_http)

    @app.post("/webchat/api/login")
    async def api_login(body: LoginRequest) -> dict[str, Any]:
        now = time.monotonic()
        if now < st.login_locked_until:
            return {
                "ok": False,
                "error": f"尝试过于频繁，请 {st.login_locked_until - now:.0f} 秒后再试",
            }
        if not totp_mod.verify_totp(st.secret, body.code):
            st.login_fails += 1
            if st.login_fails >= _LOGIN_MAX_FAILS:
                st.login_fails = 0
                st.login_locked_until = now + _LOGIN_LOCK_SECONDS
                log.warning("[WebChat] TOTP 连续校验失败，登录已临时锁定")
            return {"ok": False, "error": "动态码不正确"}
        st.login_fails = 0
        token, expires = totp_mod.issue_session(st.secret, st.session_days)
        log.info("[WebChat] TOTP 登录成功，会话已签发")
        return {"ok": True, "token": token, "expires_in": expires}

    @app.get("/webchat/api/chats", dependencies=[AUTH])
    async def api_chats() -> dict[str, Any]:
        return {"chats": st.store.summary()}

    @app.get("/webchat/api/messages", dependencies=[AUTH])
    async def api_messages(
        chat: str,
        before: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return st.store.history(chat, before=before, limit=limit)

    @app.get("/webchat/api/reply_target", dependencies=[AUTH])
    async def api_reply_target(chat: str) -> dict[str, Any]:
        msg = st.store.latest_incoming(chat)
        if not msg:
            return {"msg": None}
        return {
            "msg": {
                "msg_id": msg["msg_id"],
                "user": msg["user"],
                "nickname": msg["nickname"],
                "text": msg["text"][:100],
                "ts": msg["ts"],
            }
        }

    @app.post("/webchat/api/send", dependencies=[AUTH])
    async def api_send(body: SendRequest) -> dict[str, Any]:
        assert st.send is not None
        reply_msg_id = None if body.active else body.reply_msg_id
        return await st.send(body.chat, body.text, reply_msg_id)

    @app.post("/webchat/api/send_media", dependencies=[AUTH])
    async def api_send_media(
        chat: str = Form(min_length=1, max_length=200),
        file: UploadFile = File(...),
        caption: str = Form(default="", max_length=5000),
        reply_msg_id: str | None = Form(default=None, max_length=200),
        active: bool = Form(default=False),
    ) -> dict[str, Any]:
        assert st.send_media is not None
        file_name = Path(file.filename or "").name or "unnamed"
        mime = file.content_type or "application/octet-stream"
        data = await file.read()
        if not data:
            return {"ok": False, "error": "文件内容为空"}
        if len(data) > MEDIA_MAX_BYTES:
            return {
                "ok": False,
                "error": f"文件过大（{len(data) / 1048576:.1f}MB），上限 100MB",
            }
        reply_msg_id = None if active else reply_msg_id
        return await st.send_media(chat, data, file_name, mime, caption.strip(), reply_msg_id)

    @app.get("/webchat/api/media", dependencies=[AUTH])
    async def api_media(u: str) -> Response:
        """图片代理：服务端无 Referer 回源抓取，内存中转不落盘。

        <img> 标签通过 ?token= 携带会话令牌；URL 限 QQ 域名白名单防 SSRF。
        """
        if not st.url_allowed(u):
            raise HTTPException(status_code=404, detail="不支持的图片地址")
        result = await st.media_fetch(u)
        if result is None:
            raise HTTPException(status_code=404, detail="图片不可用（已过期或下载失败）")
        data, content_type = result
        if content_type == "application/octet-stream":
            content_type = _sniff_image(data)
        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.websocket("/webchat/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        _check_origin(ws.headers.get("origin"), ws.headers.get("host", ""))
        supplied = ws.query_params.get("token") or ""
        if not totp_mod.verify_session(st.secret, supplied, st.session_days):
            await ws.close(code=4401, reason="会话无效或已过期")
            return
        await _run_ws(ws, st)

    app.mount(
        "/webchat/static",
        NoCacheStaticFiles(directory=str(STATIC_DIR)),
        name="webchat_static",
    )

    # 面板首页（bison 版注册在 __init__；此处收拢进 setup，带不带尾斜杠都可达）
    for path in ("/webchat", "/webchat/"):
        @app.get(path, include_in_schema=False)
        async def webchat_index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    log.info("[WebChat] 路由已挂载: /webchat/（TOTP 登录）")


async def _run_ws(ws: WebSocket, st: PanelCtx) -> None:
    await ws.accept()
    queue = st.store.subscribe()
    try:
        hello: dict[str, Any] = {"type": "hello"}
        if st.status is not None:
            hello["bots"] = st.status()
        await ws.send_json(hello)
        while True:
            record = await queue.get()
            await ws.send_json(record)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("[WebChat] WebSocket 连接异常")
    finally:
        st.store.unsubscribe(queue)
