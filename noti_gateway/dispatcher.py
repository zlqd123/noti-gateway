"""调度：路由扇出、限流、投递（同步/异步）、后台重试 worker。"""

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
        self.sem = asyncio.Semaphore(cfg.dispatch.concurrency)
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

    async def _deliver_one_target(self, n: Notification, tid: str) -> SendResult:
        target = self.reg.get(tid)
        if target is None:
            return fail(tid, 0, "target not found", retryable=False)
        limiter = self.target_limiters.get(tid)
        if limiter is not None:
            allowed, reason = limiter.allow(tid)
            if not allowed:
                return fail(tid, 0, f"throttled: {reason}", retryable=True)
        async with self.sem:
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
        try:
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
        batch = max(1, self.cfg.dispatch.concurrency)
        while not self._closing:
            try:
                tasks = await self.store.claim(batch)
                if not tasks:
                    await asyncio.sleep(self.cfg.dispatch.poll_interval)
                    continue
                await asyncio.gather(*(self._handle_task(t) for t in tasks))
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
