"""限流（令牌桶）与聚合去重。"""

from __future__ import annotations

import time
from collections import OrderedDict


class TokenBucket:
    """线程/协程安全的简单令牌桶（单事件循环内无需加锁）。"""

    def __init__(self, per_minute: int, per_day: int | None = None) -> None:
        self.per_minute = max(1, per_minute)
        self.per_day = per_day
        self._tokens = float(self.per_minute)
        self._last = time.monotonic()
        self._day_count = 0
        self._day_key = self._today()

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.per_minute, self._tokens + elapsed * self.per_minute / 60.0)
            self._last = now

    def allow(self, cost: float = 1.0) -> tuple[bool, str]:
        day = self._today()
        if day != self._day_key:
            self._day_key = day
            self._day_count = 0
        if self.per_day is not None and self._day_count + int(cost) > self.per_day:
            return False, f"daily quota exhausted ({self.per_day}/day)"
        self._refill()
        if self._tokens < cost:
            return False, f"rate limited ({self.per_minute}/min)"
        self._tokens -= cost
        self._day_count += int(cost)
        return True, ""


class RateLimiter:
    """按 key 维护令牌桶，带 LRU 淘汰避免无限增长。"""

    def __init__(self, per_minute: int, per_day: int | None = None, max_keys: int = 4096):
        self.per_minute = per_minute
        self.per_day = per_day
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, str]:
        b = self._buckets.get(key)
        if b is None:
            if len(self._buckets) >= self.max_keys:
                self._buckets.popitem(last=False)
            b = TokenBucket(self.per_minute, self.per_day)
            self._buckets[key] = b
        self._buckets.move_to_end(key)
        return b.allow(cost)


class Deduper:
    """时间窗内同 key 只放行一次，用于压掉告警风暴。"""

    def __init__(self, window: float, max_keys: int = 8192) -> None:
        self.window = window
        self.max_keys = max_keys
        self._seen: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str) -> bool:
        """返回 True 表示这是窗口内重复，应被抑制。"""
        if self.window <= 0:
            return False
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < self.window:
            return True
        self._seen[key] = now
        self._seen.move_to_end(key)
        while len(self._seen) > self.max_keys:
            self._seen.popitem(last=False)
        return False

    def purge(self) -> int:
        """清掉过期条目。"""
        now = time.monotonic()
        stale = [k for k, v in self._seen.items() if now - v >= self.window]
        for k in stale:
            self._seen.pop(k, None)
        return len(stale)
