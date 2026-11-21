"""私聊消息归档插件（chatdownload）：QQ 单聊消息实时落盘。

本插件是**纯订阅者**：不持有任何 QQ 连接（WS 长连接由 qqws 插件独占），
订阅 ctx.event_listeners 里的 C2C_MESSAGE_CREATE 事件，过滤白名单后落盘。

布局（有意极简，不分子文件夹、不滚动）：

- chat.txt     —— 所有文本，一行一条，直接放 data_dir 下
- media/       —— 图片/视频/文件同一个子文件夹（文件名消毒防路径穿越）
- events.jsonl —— 收到即写的原始事件流水（append-only，防丢账本）
- error.jsonl  —— 附件下载失败记录

防丢关键：**原始事件先落盘，再做附件下载等耗时操作**；指令处理（reminders 等）
由框架在归档之后另行分发，与本插件互不影响。

按需求裁剪：不补历史、不做去重、不回消息。
硬限制（QQ 开放平台无对应 API，不是没做）：

- 没有历史消息补抓：离线/重启期间的消息永久丢失
- 没有合并转发展开（get_forward_msg 等价物不存在）

配置段见 config.example.yaml 的 chatdownload 一节。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import ChatdownloadCfg
from . import PluginMeta

log = logging.getLogger("noti.chatdownload")

META = PluginMeta(name="chatdownload", desc="单聊消息实时归档（订阅 qqws 事件流）", version="1.1.0")

TZ_CN = timezone(timedelta(hours=8))

# 文件名工具（沿用 chatdownload 插件 downloader）
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


class ChatDownloader:
    """单聊消息归档器：事件队列 -> 归档/下载。生命周期随 app。"""

    def __init__(self, cfg: ChatdownloadCfg) -> None:
        self.cfg = cfg

        self.data_dir = Path(cfg.data_dir)
        self.media_dir = self.data_dir / "media"
        self.chat_path = self.data_dir / "chat.txt"
        self.events_path = self.data_dir / "events.jsonl"
        self.error_path = self.data_dir / "error.jsonl"

        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._worker_task: asyncio.Task | None = None
        self._dl_client: object | None = None
        self._dl_sem = asyncio.Semaphore(max(1, cfg.download_concurrency))

        # 观测字段
        self.received = 0
        self.archived = 0

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._dl_client = await _new_dl_client()
        self._worker_task = asyncio.create_task(self._worker(), name="chatdownload-worker")
        log.info("chatdownload 归档器启动 data_dir=%s target_users=%s",
                 self.data_dir, self.cfg.target_users or "全部")

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await asyncio.wait_for(self._worker_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._worker_task = None
        if self._dl_client is not None:
            await self._dl_client.aclose()  # type: ignore[attr-defined]
            self._dl_client = None
        log.info("chatdownload 已停止 archived=%d", self.archived)

    def health(self) -> bool:
        return bool(self._worker_task is not None and not self._worker_task.done())

    def info(self) -> dict:
        return {
            "received": self.received,
            "archived": self.archived,
            "queue": self._queue.qsize(),
            "data_dir": str(self.data_dir),
        }

    # ---------------- 事件订阅 ----------------

    async def on_event(self, type_: str, d: dict) -> None:
        """注册进 ctx.event_listeners；qqws 广播的每个 dispatch 事件都到这里。"""
        if type_ != "C2C_MESSAGE_CREATE":
            return
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

        # 防丢关键：原始事件先落盘，再做下载等耗时操作（指令分发由框架另行处理）
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


async def _new_dl_client():
    """附件下载用独立 client（CDN 直链，与网关共享 client 的超时策略不同）。"""
    import httpx

    return httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )


def setup(ctx: Any) -> None:
    """注册 chatdownload：订阅 qqws 广播的事件流，归档白名单用户的单聊消息。"""
    cfg: ChatdownloadCfg = ctx.cfg.chatdownload
    if not cfg.enabled:
        log.info("chatdownload 插件未启用（chatdownload.enabled=false）")
        return
    if "qqws" not in ctx.cfg.plugins or not ctx.cfg.qqws.enabled:
        log.warning("chatdownload 已启用但事件流不可用（qqws 未启用），归档器不会收到任何消息")
    cd = ChatDownloader(cfg)
    ctx.event_listeners.append(cd.on_event)
    ctx.on_startup.append(cd.start)
    ctx.on_shutdown.append(cd.stop)
    ctx.health_providers["chatdownload"] = cd.health
    ctx.stats_providers["chatdownload"] = cd.info
