"""调度：路由扇出、限流、投递（同步/异步）、后台重试 worker。

派发语义（2026-11-11 起）：
- 双通道：富媒体（文件/图片，上传耗时长）与纯文本各走各的信号量与目标锁，
  纯文本「到了就发」，绝不会被文件上传堵住；
- 同一目标的富媒体严格按入队顺序 FIFO（1/3 -> 2/3 -> 3/3 不乱序）；
- worker 按任务粒度异步派发，单个慢任务不再阻塞后续任务的领取与投递。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from .config import Config
from .models import Notification, SendResult, fail
from .ratelimit import Deduper, RateLimiter
from .store import Store, Task
from .targets import Registry

log = logging.getLogger("noti.dispatcher")


class SubmitError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Dispatcher:
    def __init__(self, cfg: Config, store: Store, reg: Registry, hub=None) -> None:
        self.cfg = cfg
        self.store = store
        self.reg = reg
        self.hub = hub  # 可选事件总线：有则向 SSE /stream 订阅者广播
        self.source_limiter = RateLimiter(cfg.limits.per_source_per_minute)
        self.target_limiters: dict[str, RateLimiter] = {
            t.id: RateLimiter(t.limit(cfg.limits.per_target_per_minute), cfg.limits.per_target_per_day)
            for t in cfg.targets
        }
        self.deduper = Deduper(cfg.dedup.window_seconds) if cfg.dedup.enabled else None
        # 双通道并发上限：媒体与文本互不挤占，文本不会排队等文件上传
        self.sem_media = asyncio.Semaphore(cfg.dispatch.concurrency)
        self.sem_text = asyncio.Semaphore(cfg.dispatch.concurrency)
        # 按目标的通道锁：同通道内 FIFO 保序（asyncio.Lock 等待者按到达顺序放行）
        self._media_locks: dict[str, asyncio.Lock] = {}
        self._text_locks: dict[str, asyncio.Lock] = {}
        self._closing = False
        self.counters = {
            "received": 0,
            "queued": 0,
            "sent": 0,
            "failed": 0,
            "dead": 0,
            "suppressed": 0,
            "rate_limited": 0,
            "deferred": 0,
        }

    # ---------------- 路由 ----------------

    def resolve_targets(self, n: Notification) -> list[str]:
        for r in self.cfg.routes:
            if r.match(n):
                return list(dict.fromkeys(r.targets))  # 去重保序
        return []

    # ---------------- 提交 ----------------

    def _pub(self, type_: str, data: dict) -> None:
        if self.hub is not None:
            try:
                self.hub.publish(type_, data)
            except Exception:  # 广播绝不能影响主流程
                log.exception("hub publish failed")

    def _submitted(self, n: Notification, targets: list[str], sync: bool) -> None:
        self._pub("submitted", {
            "event_id": n.event_id, "source": n.source, "level": n.level,
            "title": n.title, "targets": targets, "sync": sync,
        })

    async def submit(self, n: Notification, wait: bool = False) -> dict:
        self.counters["received"] += 1

        if self.deduper and self.deduper.seen(n.dedup_key):
            self.counters["suppressed"] += 1
            self._pub("suppressed", {
                "event_id": n.event_id, "source": n.source, "title": n.title,
                "reason": f"dedup window {self.cfg.dedup.window_seconds}s",
            })
            return {
                "queued": False,
                "suppressed": True,
                "event_id": n.event_id,
                "reason": f"dedup window {self.cfg.dedup.window_seconds}s",
            }

        allowed, reason = self.source_limiter.allow(n.source)
        if not allowed:
            self.counters["rate_limited"] += 1
            raise SubmitError(429, f"source '{n.source}' {reason}")

        targets = self.resolve_targets(n)
        if not targets:
            raise SubmitError(404, f"no route matched (source={n.source}, level={n.level}, title={n.title!r})")

        if wait:
            results = await asyncio.gather(
                *(self._deliver_one_target(n, t) for t in targets), return_exceptions=True
            )
            payload = {}
            for t, r in zip(targets, results):
                if isinstance(r, BaseException):
                    payload[t] = SendResult(False, 0, True, repr(r), t).to_dict()
                else:
                    payload[t] = r.to_dict()
            all_ok = all(v["ok"] for v in payload.values())
            self._submitted(n, targets, sync=True)
            for t, v in payload.items():
                self._pub("receipt", {"event_id": n.event_id, "source": n.source, "target": t, **v})
            return {"queued": False, "ok": all_ok, "event_id": n.event_id, "results": payload}

        await self.store.enqueue(n, targets)
        self.counters["queued"] += 1
        self._submitted(n, targets, sync=False)
        return {"queued": True, "event_id": n.event_id, "targets": targets}

    # ---------------- 投递 ----------------

    @staticmethod
    def _is_media(n: Notification) -> bool:
        return bool(n.image or n.file)

    async def _deliver_one_target(self, n: Notification, tid: str) -> SendResult:
        target = self.reg.get(tid)
        if target is None:
            return fail(tid, 0, "target not found", retryable=False)
        limiter = self.target_limiters.get(tid)
        if limiter is not None:
            allowed, reason = limiter.allow(tid)
            if not allowed:
                return fail(tid, 0, f"throttled: {reason}", retryable=True)
        sem = self.sem_media if self._is_media(n) else self.sem_text
        async with sem:
            try:
                return await target.send(n)
            except Exception as e:
                log.exception("target %s raised", tid)
                return fail(tid, 0, f"{e!r}", retryable=True)

    def _backoff(self, attempts: int) -> float:
        d = min(
            self.cfg.dispatch.retry_max,
            self.cfg.dispatch.retry_base * (2 ** min(attempts, 10)),
        )
        return d * (0.5 + random.random() * 0.5)

    async def _handle_task(self, task: Task) -> None:
        # 限流 -> 延后而不是失败，不消耗重试次数
        limiter = self.target_limiters.get(task.target_id)
        if limiter is not None:
            allowed, _ = limiter.allow(task.target_id)
            if not allowed:
                self.counters["deferred"] += 1
                await self.store.defer(task, time.time() + 1.5)
                return

        n = Notification(**task.payload)
        # 同通道按目标 FIFO：拿到任务的顺序就是加锁顺序（锁前无 await），顺序有保证
        lane = self._media_locks if self._is_media(n) else self._text_locks
        lock = lane.setdefault(task.target_id, asyncio.Lock())
        try:
            async with lock:
                result = await self._deliver_one_target(n, task.target_id)
        except Exception as e:  # 兜底，绝不让 worker 崩
            log.exception("deliver crashed")
            result = fail(task.target_id, 0, repr(e), retryable=True)

        next_at: float | None = None
        if not result.ok:
            if result.retryable and task.attempts + 1 < self.cfg.dispatch.max_attempts:
                next_at = time.time() + self._backoff(task.attempts)
                self.counters["failed"] += 1
            else:
                self.counters["dead"] += 1
                log.warning(
                    "dead-letter event=%s target=%s attempts=%d code=%s detail=%s",
                    task.event_id, task.target_id, task.attempts + 1, result.code, result.detail,
                )
                self._pub("dead", {
                    "event_id": task.event_id, "source": n.source, "target": task.target_id,
                    "attempts": task.attempts + 1, **result.to_dict(),
                })
        else:
            self.counters["sent"] += 1

        await self.store.settle(task, result, next_at)
        self._pub("receipt", {
            "event_id": task.event_id, "source": n.source, "target": task.target_id,
            "attempts": task.attempts + 1, "retry_scheduled": next_at is not None,
            **result.to_dict(),
        })

    async def run_worker(self) -> None:
        # 每个任务独立异步投递（无批次栅栏）：慢文件只挡同通道后续文件，
        # 不再挡任何文本通知的领取与发送
        inflight: set[asyncio.Task] = set()
        max_inflight = max(8, self.cfg.dispatch.concurrency * 4)

        def _done(t: asyncio.Task) -> None:
            inflight.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("handle_task crashed: %r", t.exception())

        while not self._closing:
            try:
                room = max_inflight - len(inflight)
                tasks = await self.store.claim(room) if room > 0 else []
                if not tasks:
                    await asyncio.sleep(self.cfg.dispatch.poll_interval)
                    continue
                for t in tasks:
                    at = asyncio.create_task(self._handle_task(t))
                    inflight.add(at)
                    at.add_done_callback(_done)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("worker loop error: %r", e)
                await asyncio.sleep(1.0)

    async def drain(self, timeout: float = 15.0) -> None:
        """关闭前把已入队的任务发完，避免每次部署都丢消息。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tasks = await self.store.claim(self.cfg.dispatch.concurrency * 2)
            if not tasks:
                return
            await asyncio.gather(*(self._handle_task(t) for t in tasks))

    async def stop(self) -> None:
        self._closing = True

    def stats(self) -> dict[str, Any]:
        return dict(self.counters)
