"""私聊消息归档（chatdownload）：QQ 官方机器人单聊消息实时落盘。

布局（有意极简，不分子文件夹、不滚动）：

- chat.txt     —— 所有文本，一行一条，直接放 data_dir 下
- media/       —— 图片/视频/文件同一个子文件夹（文件名消毒防路径穿越）
- events.jsonl —— 收到即写的原始事件流水（append-only，防丢账本）
- error.jsonl  —— 附件下载失败记录

**复用同一个 QQ 机器人**：凭证取自 `qq` 配置段，access_token 走
QQTokenCache 共享缓存（与发送通道同一份）。收消息走 QQ 开放平台
WebSocket 网关（C2C_MESSAGE_CREATE），发送侧不需要维持任何连接。

按需求裁剪：不补历史、不做去重、不回消息。
硬限制（QQ 开放平台无对应 API，不是没做）：

- 没有历史消息补抓：离线期间的消息永久丢失
- 没有合并转发展开（get_forward_msg 等价物不存在）

配置段见 config.example.yaml 的 chatdownload 一节。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import ChatdownloadCfg, QQCfg
from .targets import QQTokenCache

try:  # websockets >= 14 的 asyncio 实现
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - 老版本兜底
    from websockets.client import connect as ws_connect  # type: ignore

log = logging.getLogger("noti.chatdownload")

TZ_CN = timezone(timedelta(hours=8))

# GROUP_AND_C2C_EVENT：单聊(C2C_MESSAGE_CREATE) + 群聊消息事件
QQ_INTENTS_C2C = 1 << 25


class _Reconnect(Exception):
    """服务端要求重连（op7）或会话失效（op9）。"""


# --------------------------------------------------------------------------
# 文件名工具（沿用 chatdownload 插件 downloader）
# --------------------------------------------------------------------------
_SAFE_NAME = re.compile(r"[^\w.\-]")
_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "voice": ".wav",
}


def sanitize(filename: str) -> str:
    """去掉路径分隔符和非法字符，防路径穿越。"""
    name = Path(filename).name
    name = _SAFE_NAME.sub("_", name)
    return name or "unnamed"


def ensure_ext(filename: str, fallback: str) -> str:
    return filename if Path(filename).suffix else filename + fallback


def unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        new_path = directory / f"{stem}_{i}{suffix}"
        if not new_path.exists():
            return new_path
        i += 1


def parse_ts(s: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(TZ_CN)


class ChatDownloader:
    """单聊消息归档器：WS 收事件 -> 队列 -> 归档/下载。

    生命周期随 FastAPI app：start() 建任务，stop() 取消并收尾。
    """

    def __init__(
        self,
        cfg: ChatdownloadCfg,
        qq: QQCfg,
        tokens: QQTokenCache,
        client: httpx.AsyncClient,
    ) -> None:
        self.cfg = cfg
        self.qq = qq
        self.tokens = tokens
        self.client = client  # 复用网关共享 client（取 /gateway 地址用）

        self.data_dir = Path(cfg.data_dir)
        self.media_dir = self.data_dir / "media"
        self.chat_path = self.data_dir / "chat.txt"
        self.events_path = self.data_dir / "events.jsonl"
        self.error_path = self.data_dir / "error.jsonl"

        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._dl_client: httpx.AsyncClient | None = None
        self._dl_sem = asyncio.Semaphore(max(1, cfg.download_concurrency))
        self._closing = False

        # WS 会话状态（Resume 用）
        self._session_id: str | None = None
        self._seq: int | None = None
        self._attempt = 0

        # 观测字段
        self.state = "idle"
        self.received = 0
        self.archived = 0
        self.connected_at: float | None = None
        self.last_error = ""

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._closing = False
        self._dl_client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        self._worker_task = asyncio.create_task(self._worker(), name="chatdownload-worker")
        self._task = asyncio.create_task(self._run(), name="chatdownload-ws")
        log.info(
            "chatdownload 启动 data_dir=%s target_users=%s",
            self.data_dir,
            self.cfg.target_users or "全部",
        )

    async def stop(self) -> None:
        self._closing = True
        for task in (self._task, self._worker_task):
            if task is not None:
                task.cancel()
        for task in (self._task, self._worker_task):
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        if self._dl_client is not None:
            await self._dl_client.aclose()
        log.info("chatdownload 已停止 archived=%d", self.archived)

    def health(self) -> bool:
        return bool(
            self._task is not None and not self._task.done() and self.state == "open"
        )

    def info(self) -> dict:
        return {
            "state": self.state,
            "session_id": self._session_id,
            "received": self.received,
            "archived": self.archived,
            "queue": self._queue.qsize(),
            "uptime_s": round(time.time() - self.connected_at, 1) if self.connected_at else 0,
            "last_error": self.last_error,
            "data_dir": str(self.data_dir),
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
            log.debug("chatdownload 获取网关失败: %r", e)
        return "wss://api.bot.qq.com/websocket"

    def _ident(self, token: str) -> dict:
        if self._session_id and self._seq is not None:
            return {
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
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
                log.info("chatdownload %s，立即重连", e)
                self._attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = repr(e)
                log.warning("chatdownload 网关异常: %r", e)
                await asyncio.sleep(self._backoff())
            finally:
                self.state = "down"
                self.connected_at = None

    async def _session(self) -> None:
        url = await self._gateway_url()
        token = await self.tokens.get()
        log.info("chatdownload 连接网关: %s", url)
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
            log.info(
                "chatdownload READY session=%s user=%s",
                self._session_id,
                (d.get("user") or {}).get("username"),
            )
        elif t == "RESUMED":
            self._attempt = 0
            log.info("chatdownload RESUMED")
        elif t == "C2C_MESSAGE_CREATE":
            self.received += 1
            try:
                self._queue.put_nowait(d)
            except asyncio.QueueFull:
                log.warning("chatdownload 队列已满，丢弃一条消息")

    # ---------------- 归档 ----------------

    async def _worker(self) -> None:
        while True:
            d = await self._queue.get()
            try:
                await self._save(d)
            except Exception:
                log.exception("chatdownload 归档失败")
            finally:
                self._queue.task_done()

    def _want_user(self, user: str) -> bool:
        users = self.cfg.target_users
        return not users or "*" in users or user in users

    async def _save(self, d: dict) -> None:
        author = d.get("author") or {}
        user = str(author.get("user_openid") or author.get("id") or "")
        mid = d.get("id")

        if not self._want_user(user):
            log.info("chatdownload 跳过非白名单用户 openid=%s", user)
            return

        # 防丢关键：原始事件先落盘，再做下载等耗时操作
        self._write_event({"event_type": "C2C_MESSAGE_CREATE", **d})

        text = (d.get("content") or "").strip()
        if text:
            self._append_chat(text)

        atts = [a for a in (d.get("attachments") or []) if isinstance(a, dict)]
        for i, att in enumerate(atts):
            await self._download_attachment(att, mid, i)

        self.archived += 1
        log.info(
            "chatdownload 归档 openid=%s msg_id=%s 文本=%d字符 附件=%d",
            user, mid, len(text), len(atts),
        )

    # ---------------- 存储 ----------------

    def _write_event(self, raw: dict) -> None:
        """原始事件流水：append-only 防丢账本，一行一条 JSON。"""
        try:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(raw, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("chatdownload events.jsonl 写入失败: %r", e)

    def _append_chat(self, text: str) -> None:
        """所有文本进同一个 chat.txt，一行一条消息（消息内换行原样保留）。"""
        try:
            with open(self.chat_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            log.warning("chatdownload chat.txt 写入失败: %r", e)

    def _log_error(self, message_id: Any, url: str, ftype: str, error: str) -> None:
        entry = {
            "time": datetime.now(TZ_CN).isoformat(),
            "message_id": message_id,
            "url": url,
            "type": ftype,
            "error": error,
        }
        try:
            with open(self.error_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------------- 下载 ----------------

    async def _download_attachment(self, att: dict, message_id: Any, idx: int) -> None:
        ctype = (att.get("content_type") or "").lower()
        url = att.get("url") or ""
        if ctype == "voice":
            return  # 语音不下载（与原插件一致，仅存在于 all 日志）
        if not url:
            self._log_error(message_id, "", ctype or "unknown", "附件缺少 url")
            return
        if ctype.startswith("image/"):
            ftype, ext = "image", ".jpg"
        elif ctype.startswith("video/"):
            ftype, ext = "video", ".mp4"
        else:
            ftype, ext = "file", ".bin"
        target_dir = self.media_dir  # 图片和文件同一个子文件夹
        ext = _EXT_BY_TYPE.get(ctype, ext)
        name = ensure_ext(att.get("filename") or f"{message_id}_{idx}", ext)
        await self._download_file(url, target_dir, name, message_id, ftype, att.get("size"))

    async def _download_file(
        self, url: str, directory: Path, filename: str,
        message_id: Any, ftype: str, size: Any = None,
    ) -> None:
        filename = sanitize(filename)
        async with self._dl_sem:
            # 图片同名同大小视为同一内容，跳过重复下载（检查放信号量内防并发重复下载）
            target = directory / filename
            if ftype == "image" and target.exists():
                if target.stat().st_size > 0 and (size is None or target.stat().st_size == size):
                    log.debug("chatdownload 图片已存在，跳过: %s", target.name)
                    return
                if target.stat().st_size == 0:
                    target.unlink(missing_ok=True)  # 0 字节残留重新下载
            assert self._dl_client is not None
            path: Path | None = None
            try:
                async with self._dl_client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    path = unique_path(directory, filename)
                    with open(path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
                log.info("chatdownload 下载成功: %s", path.name)
            except Exception as e:
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)  # 清掉半截文件
                    except OSError:
                        pass
                self._log_error(message_id, url, ftype, str(e))
                log.warning("chatdownload 下载失败: %s - %r", url, e)
