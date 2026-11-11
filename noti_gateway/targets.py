"""出站适配器：QQ 官方机器人 / 通用 Webhook / 出站 WebSocket 连接池。

新增平台只需实现 Target 协议（send/health/close）并在 build_targets 里注册。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import time
from typing import Any, Awaitable, Callable

import httpx

from .config import Config, QQCfg, TargetCfg
from .models import Notification, SendResult, fail, ok

try:  # websockets >= 14 的 asyncio 实现
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - 老版本兜底
    from websockets.client import connect as ws_connect  # type: ignore
    from websockets.exceptions import ConnectionClosed  # type: ignore


# --------------------------------------------------------------------------
# 模板渲染：极简 {{ var }} / {{ var.path }}，避免引入 Jinja2 依赖
# --------------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def build_context(n: Notification, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "title": n.title,
        "body": n.body,
        "level": n.level,
        "source": n.source,
        "route": n.route,
        "event_id": n.event_id,
        "ts": n.ts,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n.ts)),
    }
    ctx.update(n.fields or {})
    if extra:
        ctx.update(extra)
    return ctx


def render(template: str | None, n: Notification, default: str, extra: dict | None = None) -> str:
    """渲染模板；未给模板时用 default。未知占位符原样保留。"""
    if not template:
        return default
    ctx = build_context(n, extra)

    def sub(m: re.Match) -> str:
        cur: Any = ctx
        for part in m.group(1).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return m.group(0)
        if cur is None:
            return ""
        if isinstance(cur, (dict, list)):
            return json.dumps(cur, ensure_ascii=False)
        return str(cur)

    return _PLACEHOLDER.sub(sub, template)


def default_text(n: Notification) -> str:
    """通用可读文本：标题 + 正文 + 字段。"""
    parts = [f"[{n.level.upper()}] {n.title}"]
    if n.body:
        parts.append(n.body)
    if n.fields:
        kv = "  ".join(f"{k}={v}" for k, v in n.fields.items())
        parts.append(kv)
    parts.append(f"-- {n.source} @ {time.strftime('%H:%M:%S', time.localtime(n.ts))}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# QQ 官方机器人
# --------------------------------------------------------------------------
# 文档: https://bot.q.qq.com/wiki/develop/api-v2/
QQ_TOKEN_PATH = "/app/getAppAccessToken"
QQ_RETRYABLE_CODES = {100001}  # Too many requests


class QQTokenCache:
    """access_token 共享缓存。7200 秒有效，提前 5 分钟刷新。"""

    def __init__(self, qq: QQCfg, client: httpx.AsyncClient) -> None:
        self.qq = qq
        self.client = client
        self._token: str | None = None
        self._expire_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.time() < self._expire_at:
                return self._token
            url = self.qq.api_base.rstrip("/") + QQ_TOKEN_PATH
            r = await self.client.post(
                url, json={"appId": self.qq.app_id, "clientSecret": self.qq.client_secret}
            )
            body = r.json()
            # 注意：该接口失败时 HTTP 仍为 200，错误在 body.code
            if body.get("code", 0):
                raise QQAuthError(int(body["code"]), body.get("message", "token failed"))
            token = body.get("access_token")
            if not token:
                raise QQAuthError(0, f"no access_token in response: {body}")
            self._token = token
            self._expire_at = time.time() + max(60, int(body.get("expires_in", 7200)) - 300)
            return token

    def invalidate(self) -> None:
        self._token = None
        self._expire_at = 0.0


class QQAuthError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class QQBotTarget:
    """群聊/单聊/频道三种场景。

    - 群聊/单聊发消息是纯 HTTP，不需要维持 WebSocket
    - 频道(子频道)场景官方要求机器人保持 WebSocket 在线
    """

    def __init__(self, cfg: TargetCfg, qq: QQCfg, tokens: QQTokenCache, client: httpx.AsyncClient):
        self.id = cfg.id
        self.cfg = cfg
        self.tokens = tokens

        if cfg.group_openid:
            self.scene, self.scene_id = "group", cfg.group_openid
        elif cfg.user_openid:
            self.scene, self.scene_id = "c2c", cfg.user_openid
        elif cfg.channel_id:
            self.scene, self.scene_id = "channel", cfg.channel_id
        else:
            raise ValueError(f"qq target {cfg.id} 需要 group_openid / user_openid / channel_id 之一")

    def _path(self) -> str:
        base = self.tokens.qq.api_base.rstrip("/")
        if self.scene == "group":
            return f"{base}/v2/groups/{self.scene_id}/messages"
        if self.scene == "c2c":
            return f"{base}/v2/users/{self.scene_id}/messages"
        return f"{base}/channels/{self.scene_id}/messages"

    def _payload(self, n: Notification) -> dict:
        text = render(self.cfg.template, n, default_text(n))
        if self.cfg.msg_type == 2:
            return {"msg_type": 2, "markdown": text}
        return {"msg_type": 0, "content": text}

    # ---------------- 富媒体（图片 / 文件） ----------------
    # 上传恒走分片通道：upload_prepare(三哈希) -> 逐片 PUT -> 逐片 upload_part_finish
    #   -> files 合并拿 file_info（契约抄自实测可用的 sd_media.py）。
    # 坑位备忘：upload_prepare 必须带全 md5/sha1/md5_10m 三个哈希；
    #   PUT 必须带 Content-Type，否则图能发出去但不渲染；
    #   /files 简单表单上传在 api.bot.qq.com 稳定 40093007（见 _upload_chunked 注释）。

    def _media_base(self) -> str:
        base = self.tokens.qq.api_base.rstrip("/")
        if self.scene == "group":
            return f"{base}/v2/groups/{self.scene_id}"
        if self.scene == "c2c":
            return f"{base}/v2/users/{self.scene_id}"
        return f"{base}/channels/{self.scene_id}"

    def _resolve_media(self, media: dict | None) -> tuple[bytes, str, str]:
        """内联 b64 或 /u/* ref -> (字节, 文件名, mime)。"""
        if not media:
            raise ValueError("空媒体")
        if media.get("ref"):
            from .uploads import get as get_store

            data, meta = get_store().load(str(media["ref"]))
            return (
                data,
                str(meta.get("name") or "file.bin"),
                str(meta.get("mime") or "application/octet-stream"),
            )
        b64 = media.get("b64")
        if not isinstance(b64, str) or not b64:
            raise ValueError("媒体缺 b64/ref")
        data = base64.b64decode(b64)
        return (
            data,
            str(media.get("name") or "image.jpg"),
            str(media.get("mime") or "image/jpeg"),
        )

    def _upload_result(self, r: httpx.Response, how: str) -> tuple[str | None, SendResult | None]:
        """上传响应统一判定：返回 (file_info, None) 或 (None, SendResult)。401 交给调用方刷 token。"""
        if r.status_code == 401:
            return None, fail(self.id, 401, "token expired", retryable=True)
        if r.status_code == 429 or r.status_code >= 500:
            return None, fail(
                self.id, r.status_code, f"upload({how}) http {r.status_code}: {r.text[:200]}", retryable=True
            )
        if r.status_code >= 400:
            return None, fail(
                self.id, r.status_code, f"upload({how}) http {r.status_code}: {r.text[:200]}", retryable=False
            )
        try:
            body = r.json()
        except Exception:
            return None, fail(self.id, r.status_code, f"upload({how}) non-json: {r.text[:200]}", retryable=False)
        code = int(body.get("code") or 0) if isinstance(body, dict) else -1
        if code == 0 and isinstance(body, dict) and body.get("file_info"):
            return str(body["file_info"]), None
        return None, fail(
            self.id, code, f"upload({how}) failed: {str(body)[:200]}", retryable=code in QQ_RETRYABLE_CODES
        )

    async def _upload_chunked(
        self, token: str, data: bytes, name: str, mime: str, file_type: int
    ) -> tuple[str | None, SendResult | None]:
        """分片通道是 QQ 富媒体上传的唯一可用路径（恒走，不论大小）：
        实测（2026-11-11）/files 简单表单上传在 api.bot.qq.com 稳定报
        40093007 富媒体文件下载失败（多组字段变体均失败），而
        upload_prepare(三哈希) -> PUT -> upload_part_finish -> files 合并
        全程可用；小文件由服务端合成单片即可。"""
        headers = {"Authorization": f"QQBot {token}"}
        block_default = 10 * 1024 * 1024
        try:
            r = await self.tokens.client.post(
                self._media_base() + "/upload_prepare",
                headers=headers,
                json={
                    "file_type": file_type,
                    "file_name": name,
                    "file_size": len(data),
                    "md5": hashlib.md5(data).hexdigest(),
                    "sha1": hashlib.sha1(data).hexdigest(),
                    "md5_10m": hashlib.md5(data[:block_default]).hexdigest(),
                },
                timeout=max(self.cfg.timeout, 30.0),
            )
        except Exception as e:
            return None, fail(self.id, 0, f"prepare failed: {e!r}", retryable=True)
        if r.status_code >= 400:
            return None, fail(
                self.id,
                r.status_code,
                f"prepare http {r.status_code}: {r.text[:200]}",
                retryable=r.status_code == 429 or r.status_code >= 500,
            )
        try:
            prep = r.json()
            upload_id = prep["upload_id"]
            parts = prep.get("parts") or []
            block = int(prep.get("block_size") or len(data))
        except Exception as e:
            return None, fail(self.id, 0, f"prepare 解析失败: {e!r}", retryable=False)
        if not parts:
            return None, fail(self.id, 0, f"prepare 未返回分片: {str(prep)[:200]}", retryable=False)

        for i, part in enumerate(parts):
            chunk = data[i * block:(i + 1) * block]
            try:
                put = await self.tokens.client.put(
                    part["presigned_url"],
                    content=chunk,
                    headers={"Content-Type": mime},
                    timeout=max(self.cfg.timeout, 60.0),
                )
            except Exception as e:
                return None, fail(self.id, 0, f"part{i} PUT failed: {e!r}", retryable=True)
            if put.status_code >= 400:
                return None, fail(
                    self.id,
                    put.status_code,
                    f"part{i} PUT http {put.status_code}: {put.text[:120]}",
                    retryable=put.status_code == 429 or put.status_code >= 500,
                )
            try:
                fin = await self.tokens.client.post(
                    self._media_base() + "/upload_part_finish",
                    headers=headers,
                    json={
                        "upload_id": upload_id,
                        "part_index": part["index"],
                        "block_size": len(chunk),
                        "md5": hashlib.md5(chunk).hexdigest(),
                    },
                    timeout=max(self.cfg.timeout, 30.0),
                )
            except Exception as e:
                return None, fail(self.id, 0, f"part{i} finish failed: {e!r}", retryable=True)
            if fin.status_code >= 400:
                return None, fail(
                    self.id,
                    fin.status_code,
                    f"part{i} finish http {fin.status_code}: {fin.text[:120]}",
                    retryable=fin.status_code == 429 or fin.status_code >= 500,
                )

        try:
            done_r = await self.tokens.client.post(
                self._media_base() + "/files",
                headers=headers,
                json={"file_type": file_type, "file_name": name, "upload_id": upload_id, "srv_send_msg": False},
                timeout=max(self.cfg.timeout, 30.0),
            )
        except Exception as e:
            return None, fail(self.id, 0, f"merge failed: {e!r}", retryable=True)
        return self._upload_result(done_r, f"chunked({len(parts)}片)")

    async def _send_media(self, n: Notification, media: dict, kind: str) -> SendResult:
        t0 = time.monotonic()

        def done(r: SendResult) -> SendResult:
            r.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return r

        try:
            data, name, mime = self._resolve_media(media)
        except Exception as e:
            return done(fail(self.id, 0, f"{kind} 解析失败: {e}", retryable=False))
        if self.scene == "channel":
            return done(fail(self.id, 0, "频道场景暂不支持富媒体", retryable=False))
        if kind == "image" and mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            return done(fail(self.id, 0, f"不支持的图片类型: {mime}", retryable=False))
        file_type = 1 if kind == "image" else 4

        for attempt in (1, 2):  # 第二次用于 401 后强制刷新 token 重试
            try:
                token = await self.tokens.get(force=attempt == 2)
            except QQAuthError as e:
                return done(fail(self.id, e.code, f"token error: {e.message}", retryable=False))
            except Exception as e:  # 网络异常
                return done(fail(self.id, 0, f"token request failed: {e!r}", retryable=True))

            file_info, err = await self._upload_chunked(token, data, name, mime, file_type)
            if err is not None:
                if err.code == 401 and attempt == 1:
                    self.tokens.invalidate()
                    continue
                return done(err)

            try:
                r = await self.tokens.client.post(
                    self._path(),
                    headers={"Authorization": f"QQBot {token}"},
                    json={"msg_type": 7, "media": {"file_info": file_info}},
                    timeout=self.cfg.timeout,
                )
            except Exception as e:
                return done(fail(self.id, 0, f"media send failed: {e!r}", retryable=True))

            if r.status_code == 401 and attempt == 1:
                self.tokens.invalidate()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                return done(
                    fail(self.id, r.status_code, f"http {r.status_code}: {r.text[:200]}", retryable=True)
                )
            if r.status_code >= 400:
                return done(
                    fail(self.id, r.status_code, f"http {r.status_code}: {r.text[:200]}", retryable=False)
                )
            try:
                body = r.json()
            except Exception:
                return done(ok(self.id, "non-json response", 0))
            if not isinstance(body, dict):
                return done(ok(self.id, "unexpected body", 0))
            code = int(body.get("code") or 0)
            if code == 0:
                return done(ok(self.id, f"media msg_id={body.get('id', '')} file_info={str(file_info)[:24]}"))
            return done(
                fail(
                    self.id,
                    code,
                    body.get("message") or json.dumps(body, ensure_ascii=False)[:200],
                    retryable=code in QQ_RETRYABLE_CODES,
                )
            )
        return done(fail(self.id, 0, "unreachable"))

    async def _send_text(self, n: Notification) -> SendResult:
        t0 = time.monotonic()

        def done(r: SendResult) -> SendResult:
            r.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return r

        for attempt in (1, 2):  # 第二次用于 401 后强制刷新 token 重试
            try:
                token = await self.tokens.get(force=attempt == 2)
            except QQAuthError as e:
                return done(fail(self.id, e.code, f"token error: {e.message}", retryable=False))
            except Exception as e:  # 网络异常
                return done(fail(self.id, 0, f"token request failed: {e!r}", retryable=True))

            try:
                r = await self.tokens.client.post(
                    self._path(),
                    headers={"Authorization": f"QQBot {token}"},
                    json=self._payload(n),
                    timeout=self.cfg.timeout,
                )
            except Exception as e:
                return done(fail(self.id, 0, f"send failed: {e!r}", retryable=True))

            if r.status_code == 401 and attempt == 1:
                self.tokens.invalidate()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                return done(
                    fail(self.id, r.status_code, f"http {r.status_code}: {r.text[:200]}", retryable=True)
                )
            if r.status_code >= 400:
                return done(
                    fail(self.id, r.status_code, f"http {r.status_code}: {r.text[:200]}", retryable=False)
                )

            # 关键：失败也是 HTTP 200，必须检查 body 里的 code
            try:
                body = r.json()
            except Exception:
                return done(ok(self.id, "non-json response", 0) if r.status_code == 200
                            else fail(self.id, r.status_code, r.text[:200]))

            if not isinstance(body, dict):
                return done(ok(self.id, "unexpected body", 0))
            code = int(body.get("code") or 0)
            if code == 0:
                msg_id = body.get("id", "")
                return done(ok(self.id, f"msg_id={msg_id}"))
            return done(
                fail(
                    self.id,
                    code,
                    body.get("message") or json.dumps(body, ensure_ascii=False)[:200],
                    retryable=code in QQ_RETRYABLE_CODES,
                )
            )
        return done(fail(self.id, 0, "unreachable"))

    async def send(self, n: Notification) -> SendResult:
        """文本 + 富媒体组合发送：image/file 各自一条 msg_type 7，文本按需一条。

        纯媒体（无标题/正文/字段）不发多余文本消息。
        """
        results: list[tuple[str, SendResult]] = []
        if n.image:
            results.append(("image", await self._send_media(n, n.image, "image")))
        if n.file:
            results.append(("file", await self._send_media(n, n.file, "file")))
        if (n.title or n.body or n.fields) or not results:
            results.append(("text", await self._send_text(n)))
        if len(results) == 1:
            return results[0][1]

        ok_all = all(r.ok for _, r in results)
        detail = "; ".join(
            f"{what}: {r.detail or ('ok' if r.ok else 'fail')}" for what, r in results
        )
        retryable = any(not r.ok and r.retryable for _, r in results)
        code = next((r.code for _, r in results if not r.ok), 0)
        elapsed = max((r.elapsed_ms for _, r in results), default=0)
        if ok_all:
            return ok(self.id, detail, elapsed)
        return fail(self.id, code, detail, retryable, elapsed)

    async def health(self) -> bool:
        try:
            await self.tokens.get()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# 通用 Webhook
# --------------------------------------------------------------------------
class WebhookTarget:
    """任意 URL 的 POST。默认兼容企微/钉钉/飞书/TG 的成功判定：

    - HTTP 2xx 且 body 里没有非零的 code/errcode/status_code -> 成功
    """

    def __init__(self, cfg: TargetCfg, client: httpx.AsyncClient) -> None:
        self.id = cfg.id
        self.cfg = cfg
        self.client = client

    async def send(self, n: Notification) -> SendResult:
        t0 = time.monotonic()
        text = render(self.cfg.template, n, default_text(n))
        # 模板是 JSON 就原样发，否则按 form 提交
        body: Any
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                body = json.loads(stripped)
            except json.JSONDecodeError:
                body = {"msgtype": "text", "text": {"content": text}}
        else:
            body = {"msgtype": "text", "text": {"content": text}, "content": text}
        try:
            r = await self.client.request(
                self.cfg.method,
                self.cfg.url,
                json=body,
                headers=self.cfg.headers or None,
                timeout=self.cfg.timeout,
            )
        except Exception as e:
            return fail(self.id, 0, f"webhook failed: {e!r}", retryable=True)

        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 429 or r.status_code >= 500:
            return fail(self.id, r.status_code, f"http {r.status_code}: {r.text[:200]}", True, ms)
        if r.status_code >= 400:
            return fail(self.id, r.status_code, f"http {r.status_code}: {r.text[:200]}", False, ms)

        try:
            data = r.json()
        except Exception:
            return ok(self.id, f"http {r.status_code}", ms)
        if isinstance(data, dict):
            for k in ("code", "errcode", "StatusCode", "error_code"):
                if k in data:
                    try:
                        v = int(data[k])
                    except (TypeError, ValueError):
                        continue
                    if v != 0:
                        detail = data.get("errmsg") or data.get("message") or data.get("msg") or str(data)
                        retryable = v in (100001, 10003, 429, 130101)
                        return fail(self.id, v, str(detail)[:200], retryable, ms)
        return ok(self.id, f"http {r.status_code}", ms)

    async def health(self) -> bool:
        return bool(self.cfg.url)

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# 出站 WebSocket 连接池
# --------------------------------------------------------------------------
class WSConnection:
    """单条出站 WS 连接：独立状态机 + 指数退避重连 + 应用层心跳。"""

    def __init__(self, name: str, url: str, cfg: TargetCfg) -> None:
        self.name = name
        self.url = url
        self.cfg = cfg
        self._ws: Any = None
        self._state = "idle"
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._attempt = 0
        self._closing = False
        self.on_frame: Callable[[Any], Awaitable[None]] | None = None
        self.last_error = ""
        self.sent = 0
        self.received = 0
        self.connected_at: float | None = None

    # ---- 状态 ----
    @property
    def state(self) -> str:
        return self._state

    def is_open(self) -> bool:
        return self._state == "open" and self._ws is not None

    def info(self) -> dict:
        return {
            "name": self.name,
            "state": self._state,
            "sent": self.sent,
            "received": self.received,
            "uptime_s": round(time.time() - self.connected_at, 1) if self.connected_at else 0,
            "last_error": self.last_error,
        }

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"ws-{self.name}")

    async def stop(self) -> None:
        self._closing = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    def _backoff(self) -> float:
        d = min(self.cfg.reconnect_max, self.cfg.reconnect_base * (2 ** min(self._attempt, 10)))
        self._attempt += 1
        return d * (0.5 + random.random() * 0.5)  # jitter，避免多连接同时重连

    def _fail_pending(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()

    async def _run(self) -> None:
        while not self._closing:
            try:
                async with ws_connect(
                    self.url,
                    open_timeout=self.cfg.timeout,
                    close_timeout=3,
                    max_size=self.cfg.max_size,
                    ping_interval=None,  # 自己做应用层心跳
                ) as ws:
                    self._ws = ws
                    self._state = "open"
                    self._attempt = 0
                    self.connected_at = time.time()
                    self.last_error = ""
                    while not self._closing:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.cfg.ping_interval)
                        except asyncio.TimeoutError:
                            await self._app_ping(ws)
                            continue
                        except ConnectionClosed:
                            break
                        except Exception as e:
                            self.last_error = f"recv: {e!r}"
                            break
                        self.received += 1
                        await self._handle_frame(raw)
            except Exception as e:
                self.last_error = repr(e)
            finally:
                self._ws = None
                self._state = "down"
                self.connected_at = None
                self._fail_pending("connection lost")
            if self._closing:
                break
            await asyncio.sleep(self._backoff())

    async def _app_ping(self, ws: Any) -> None:
        """应用层心跳。可在配置里给 ping_payload 适配具体协议（如 OneBot）。"""
        self._state = "open"  # 能走到这里说明连接至少是活的
        payload = getattr(self.cfg, "ping_payload", None)
        if not payload:
            return
        try:
            async with self._send_lock:
                await ws.send(payload)
        except Exception as e:
            self.last_error = f"ping: {e!r}"
            self._state = "down"

    async def _handle_frame(self, raw: Any) -> None:
        data: Any = raw
        if isinstance(raw, (bytes, bytearray)):
            try:
                data = raw.decode("utf-8", "replace")
            except Exception:
                return
        obj: Any = data
        if isinstance(data, str):
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                obj = data
        if isinstance(obj, dict):
            key = obj.get("echo") or obj.get("echo_id")
            if key is not None and key in self._pending:
                fut = self._pending.pop(key)
                if not fut.done():
                    fut.set_result(obj)
                return
            # OneBot 风格：status/retcode 即回执
            if ("status" in obj or "retcode" in obj) and self._pending:
                key = next(iter(self._pending))
                fut = self._pending.pop(key)
                if not fut.done():
                    fut.set_result(obj)
                return
        if self.on_frame:
            try:
                await self.on_frame(obj)
            except Exception as e:
                self.last_error = f"on_frame: {e!r}"

    # ---- 发送 ----
    async def send(self, payload: dict, wait_ack: bool, echo_id: str) -> SendResult:
        t0 = time.monotonic()
        ws = self._ws
        if ws is None or not self.is_open():
            return fail(self.name, 0, "not connected", retryable=True,
                        elapsed_ms=int((time.monotonic() - t0) * 1000))

        loop = asyncio.get_running_loop()
        fut: asyncio.Future | None = None
        if wait_ack:
            # 先注册再发送，否则极快的回执会丢失
            fut = loop.create_future()
            self._pending[echo_id] = fut

        if wait_ack and isinstance(payload, dict) and "echo" not in payload:
            payload = {**payload, "echo": echo_id}

        try:
            async with self._send_lock:
                await ws.send(json.dumps(payload, ensure_ascii=False))
            self.sent += 1
        except Exception as e:
            if fut:
                self._pending.pop(echo_id, None)
            # 发送失败说明连接已死，标记让重连循环接管
            self._state = "down"
            self.last_error = f"send: {e!r}"
            return fail(self.name, 0, f"send failed: {e!r}", retryable=True,
                        elapsed_ms=int((time.monotonic() - t0) * 1000))

        if fut is None:
            return ok(self.name, "", int((time.monotonic() - t0) * 1000))

        try:
            await asyncio.wait_for(fut, timeout=self.cfg.ack_timeout)
            return ok(self.name, "ack", int((time.monotonic() - t0) * 1000))
        except asyncio.TimeoutError:
            # 已发出只是没等到回执；不重试以免重复投递
            return ok(self.name, "ack timeout (sent, unconfirmed)",
                      int((time.monotonic() - t0) * 1000))
        except Exception as e:
            return fail(self.name, 0, f"ack error: {e!r}", retryable=True,
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
        finally:
            self._pending.pop(echo_id, None)


class WSTarget:
    """多个 WSConnection 组成的连接池，round-robin 分配。"""

    def __init__(self, cfg: TargetCfg) -> None:
        self.id = cfg.id
        self.cfg = cfg
        self.connections = [
            WSConnection(f"{cfg.id}#{i}", cfg.url, cfg) for i in range(max(1, cfg.pool))
        ]
        self._rr = 0

    async def start(self) -> None:
        for c in self.connections:
            await c.start()

    async def send(self, n: Notification) -> SendResult:
        # 优先选已连接的；没有就选第一条（它会触发重连）
        conns = self.connections
        open_conns = [c for c in conns if c.is_open()]
        if open_conns:
            conn = open_conns[self._rr % len(open_conns)]
        else:
            conn = conns[0]
        self._rr += 1

        text = render(self.cfg.template, n, default_text(n))
        payload: Any = text
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = text

        wait_ack = self.cfg.ack_timeout > 0
        echo_id = n.event_id
        return await conn.send(payload if isinstance(payload, dict) else {"text": payload},
                               wait_ack, echo_id)

    async def health(self) -> bool:
        return any(c.is_open() for c in self.connections)

    async def close(self) -> None:
        await asyncio.gather(*(c.stop() for c in self.connections), return_exceptions=True)

    def info(self) -> list[dict]:
        return [c.info() for c in self.connections]


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------
class Registry:
    def __init__(self) -> None:
        self.targets: dict[str, Any] = {}
        # 每台机器人一份 access_token 缓存；key 为 bot id，"" = 旧式全局 qq 段
        self.token_caches: dict[str, QQTokenCache] = {}

    def get(self, tid: str) -> Any | None:
        return self.targets.get(tid)

    async def start_all(self) -> None:
        for t in self.targets.values():
            if isinstance(t, WSTarget):
                await t.start()

    async def close_all(self) -> None:
        for t in self.targets.values():
            try:
                await t.close()
            except Exception:
                pass

    def info(self) -> dict:
        out: dict[str, Any] = {}
        for tid, t in self.targets.items():
            if isinstance(t, WSTarget):
                out[tid] = {"type": "ws", "connections": t.info()}
            elif isinstance(t, QQBotTarget):
                out[tid] = {"type": "qq", "scene": t.scene, "scene_id": t.scene_id}
            else:
                out[tid] = {"type": type(t).__name__}
            if getattr(t, "cfg", None) is not None:
                if getattr(t.cfg, "remark", ""):
                    out[tid]["remark"] = t.cfg.remark
                if getattr(t.cfg, "bot", ""):
                    out[tid]["bot"] = t.cfg.bot
        return out


def build_targets(
    cfg: Config, client: httpx.AsyncClient, tokens: QQTokenCache | None = None
) -> Registry:
    reg = Registry()
    # tokens 可外部传入以共享同一份 access_token 缓存（如 chatdownload 复用同一机器人）；
    # 未传入时按机器人各建一份，key 为 bot id（"" = 旧式全局 qq 段）
    if tokens is not None:
        reg.token_caches[""] = tokens
    else:
        reg.token_caches[""] = QQTokenCache(cfg.qq, client)
    for b in cfg.bots:
        reg.token_caches[b.id] = QQTokenCache(b.qq(), client)

    for tc in cfg.targets:
        if not tc.enabled:
            continue
        if tc.type == "qq":
            qq = cfg.qq_for(tc)
            cache = reg.token_caches.get(tc.bot, reg.token_caches[""])
            reg.targets[tc.id] = QQBotTarget(tc, qq, cache, client)
        elif tc.type == "webhook":
            if not tc.url:
                raise SystemExit(f"webhook target {tc.id} 缺少 url")
            reg.targets[tc.id] = WebhookTarget(tc, client)
        elif tc.type == "ws":
            if not tc.url:
                raise SystemExit(f"ws target {tc.id} 缺少 url")
            reg.targets[tc.id] = WSTarget(tc)
        else:
            raise SystemExit(f"未知 target 类型: {tc.type}")
    return reg
