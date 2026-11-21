"""WebChat 消息存储：内存环形缓冲 + jsonl 落盘。

消息记录统一为一份 dict：
    seq      自增序号（UI 翻页游标，also 被动回复计数无关）
    dir      "in" 收到 / "out" bot 发出
    chat     会话 key（group_xxx / friend_xxx / guild_xxx_xxx / dms_xxx）
    type     会话类型 group / friend / guild / dms
    user     发送者 id（openid 或 bot 自身 id）
    nickname 发送者昵称（群聊官方接口通常拿不到，可能为空）
    msg_id   平台消息 id（被动回复时引用）
    text     展示文本（非文本段以 [占位] 表示）
    ts       秒级时间戳
    reply_to 被回复的原消息 id（仅 out 且为被动回复时有）
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

# 存储文本截断长度，防止超长消息撑爆内存；
# 聊天记录/合并转发类消息很长且内嵌图片 URL，放宽到 4000
TEXT_MAX = 4000


class ChatStore:
    """按会话分桶的消息仓库，附带监听队列广播与 jsonl 持久化。"""

    def __init__(
        self,
        max_per_chat: int = 300,
        persist_dir: str | Path | None = None,
        persist_max_bytes: int = 10 * 1024 * 1024,
        load_lines: int = 2000,
    ) -> None:
        self._max_per_chat = max_per_chat
        self._chats: dict[str, deque[dict[str, Any]]] = {}
        # 会话显示名（群名由后端拉取官方接口填充），重启后重新拉取
        self._chat_names: dict[str, str] = {}
        self._seq = 0
        # 每条被回复消息的 msg_seq 计数（被动回复序号需自增去重）
        self._reply_seq: dict[str, int] = {}
        # WebSocket 客户端队列，add/broadcast 时投递记录
        self._listeners: list[asyncio.Queue] = []

        self._persist_dir = Path(persist_dir) if persist_dir else None
        self._persist_file = (
            self._persist_dir / "messages.jsonl" if self._persist_dir else None
        )
        self._persist_max_bytes = persist_max_bytes
        self._load_lines = load_lines

        if self._persist_file:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            self._load_persisted()

    # ------------------------------------------------------------------ 写入

    def add(self, record: dict[str, Any]) -> dict[str, Any]:
        """登记一条消息：分配序号、落盘、推给所有在线面板。"""
        record.setdefault("dir", "in")
        record.setdefault("type", record["chat"].split("_", 1)[0])
        record.setdefault("user", "")
        record.setdefault("nickname", "")
        record.setdefault("msg_id", "")
        record.setdefault("reply_to", "")
        record.setdefault("text", "")
        record.setdefault("media_urls", [])
        record.setdefault("error", "")
        record["text"] = str(record["text"])[:TEXT_MAX]
        self._seq += 1
        record["seq"] = self._seq
        record.setdefault("ts", int(time.time()))

        bucket = self._chats.setdefault(record["chat"], deque(maxlen=self._max_per_chat))
        bucket.append(record)

        if self._persist_file:
            self._persist(record)
        self.broadcast(record)
        return record

    def broadcast(self, record: dict[str, Any]) -> None:
        """向所有面板连接广播（消息、状态变更、发送失败等）。"""
        for queue in list(self._listeners):
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                # 面板卡死的客户端不拖累主循环，直接丢弃
                pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._listeners:
            self._listeners.remove(queue)

    # ------------------------------------------------------------------ 读取

    def set_chat_name(self, chat: str, name: str) -> None:
        """登记官方接口查到的会话名（当前仅群名），并广播给在线面板。"""
        if not name or self._chat_names.get(chat) == name:
            return
        self._chat_names[chat] = name
        self.broadcast({"type": "chat_name", "chat": chat, "name": name})

    def chat_name(self, chat: str) -> str:
        return self._chat_names.get(chat, "")

    def group_chats(self) -> list[str]:
        """当前所有群聊会话 key。"""
        return [key for key in self._chats if key.startswith("group_")]

    def summary(self) -> list[dict[str, Any]]:
        """会话列表：按最近活跃倒序。"""
        chats = []
        for key, bucket in self._chats.items():
            last = bucket[-1] if bucket else None
            chats.append(
                {
                    "chat": key,
                    "type": last["type"] if last else key.split("_", 1)[0],
                    "name": self._chat_names.get(key, ""),
                    "count": len(bucket),
                    "last_text": (last["text"] if last else "")[:80],
                    "last_ts": (last["ts"] if last else 0),
                    "last_dir": (last["dir"] if last else ""),
                }
            )
        chats.sort(key=lambda c: c["last_ts"], reverse=True)
        return chats

    def history(
        self,
        chat: str,
        before: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """取某会话的历史消息，before 为更早一页的游标（上一页最小 seq）。"""
        bucket = self._chats.get(chat)
        if not bucket:
            return {"messages": [], "has_more": False}
        limit = max(1, min(limit, 500))
        items = [m for m in bucket if before is None or m["seq"] < before]
        page = list(items[-limit:])
        return {
            "messages": page,
            "has_more": len(items) > limit,
        }

    def latest_incoming(self, chat: str) -> dict[str, Any] | None:
        """最近一条收到的消息（被动回复默认目标）。"""
        bucket = self._chats.get(chat)
        if not bucket:
            return None
        for msg in reversed(bucket):
            if msg["dir"] == "in" and msg.get("msg_id"):
                return msg
        return None

    def next_reply_seq(self, msg_id: str) -> int:
        """为某条被回复消息分配下一个 msg_seq（1 起，官方上限 5）。"""
        current = self._reply_seq.get(msg_id, 0) + 1
        self._reply_seq[msg_id] = current
        # 计数表防膨胀：超过 1000 条时丢弃最早的一半
        if len(self._reply_seq) > 1000:
            for key in list(self._reply_seq)[:500]:
                del self._reply_seq[key]
        return current

    # ------------------------------------------------------------------ 落盘

    def _persist(self, record: dict[str, Any]) -> None:
        assert self._persist_file is not None
        try:
            if (
                self._persist_file.exists()
                and self._persist_file.stat().st_size > self._persist_max_bytes
            ):
                rotated = self._persist_file.with_suffix(".jsonl.1")
                self._persist_file.replace(rotated)
            with self._persist_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 落盘失败不影响消息面板主功能

    def _load_persisted(self) -> None:
        assert self._persist_file is not None
        try:
            if not self._persist_file.exists():
                return
            lines = self._persist_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            for line in lines[-self._load_lines :]:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or "chat" not in record:
                    continue
                bucket = self._chats.setdefault(
                    record["chat"], deque(maxlen=self._max_per_chat)
                )
                bucket.append(record)
            # 续上 seq，避免 UI 翻页游标重叠
            all_msgs = [m for b in self._chats.values() for m in b]
            self._seq = max((m.get("seq", 0) for m in all_msgs), default=0)
        except OSError:
            pass
