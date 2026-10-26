"""进程内事件总线：把网关内部事件（submitted/receipt/dead/suppressed）
广播给 SSE /stream 等订阅者。

设计约束：
- publish 永不阻塞、永不抛错——慢订阅者丢消息（队列满），不能拖垮投递主流程；
- 单进程单事件循环，无需加锁。
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any


class Hub:
    def __init__(self, buffer: int = 256) -> None:
        self.buffer = buffer
        self._subs: set[asyncio.Queue] = set()
        self._seq = itertools.count(1)

    def publish(self, type_: str, data: dict[str, Any]) -> int:
        seq = next(self._seq)
        msg = {"seq": seq, "type": type_, "data": data}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # 慢订阅者自行承担丢消息
        return seq

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.buffer)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def count(self) -> int:
        return len(self._subs)
