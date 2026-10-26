"""SQLite 持久化队列：write-through 入队、重试、死信、投递回执。

设计要点：
- 入队先落盘再返回 202，进程崩溃/重启不丢消息（write-through）
- 每个 target 独立重试，一个渠道挂了不拖累其他
- 可重试错误指数退避，不可重试直接死信
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DispatchCfg
from .models import Notification, SendResult

PENDING = "pending"
INFLIGHT = "inflight"
DONE = "done"
DEAD = "dead"


@dataclass
class Task:
    id: int
    event_id: str
    target_id: str
    payload: dict
    attempts: int


class Store:
    def __init__(self, path: str | Path, cfg: DispatchCfg) -> None:
        self.path = str(path)
        self.cfg = cfg
        self._lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id        TEXT NOT NULL,
                    target_id       TEXT NOT NULL,
                    payload         TEXT NOT NULL,
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL    NOT NULL,
                    state           TEXT    NOT NULL DEFAULT 'pending',
                    last_error      TEXT,
                    created_at      REAL    NOT NULL,
                    updated_at      REAL    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_due
                    ON tasks(state, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_event
                    ON tasks(event_id);

                CREATE TABLE IF NOT EXISTS receipts (
                    event_id  TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    state     TEXT NOT NULL,
                    code      INTEGER NOT NULL DEFAULT 0,
                    detail    TEXT,
                    attempts  INTEGER NOT NULL DEFAULT 0,
                    ts        REAL NOT NULL,
                    PRIMARY KEY (event_id, target_id)
                );
                """
            )
            self._db.commit()

    # ---------------- 同步底层操作 ----------------

    def _enqueue_sync(self, payloads: list[tuple[str, str, dict]]) -> None:
        now = time.time()
        rows = [
            (ev, tid, json.dumps(p, ensure_ascii=False), now, now, now)
            for ev, tid, p in payloads
        ]
        with self._lock:
            self._db.executemany(
                """INSERT INTO tasks
                   (event_id, target_id, payload, next_attempt_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                rows,
            )
            self._db.commit()

    def _claim_sync(self, limit: int) -> list[Task]:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                """SELECT id, event_id, target_id, payload, attempts
                     FROM tasks
                    WHERE state = ? AND next_attempt_at <= ?
                    ORDER BY next_attempt_at ASC, id ASC
                    LIMIT ?""",
                (PENDING, now, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            self._db.executemany(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                [(INFLIGHT, now, i) for i in ids],
            )
            self._db.commit()
        return [
            Task(r["id"], r["event_id"], r["target_id"], json.loads(r["payload"]), r["attempts"])
            for r in rows
        ]

    def _settle_sync(
        self,
        task: Task,
        result: SendResult,
        next_at: float | None,
    ) -> None:
        now = time.time()
        state = DONE if result.ok else (PENDING if next_at is not None else DEAD)
        with self._lock:
            self._db.execute(
                """UPDATE tasks
                      SET state = ?, attempts = attempts + 1, next_attempt_at = ?,
                          last_error = ?, updated_at = ?
                    WHERE id = ?""",
                (state, next_at if next_at is not None else now, result.detail or None, now, task.id),
            )
            self._db.execute(
                """INSERT INTO receipts (event_id, target_id, state, code, detail, attempts, ts)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(event_id, target_id) DO UPDATE SET
                       state = excluded.state, code = excluded.code,
                       detail = excluded.detail, attempts = excluded.attempts, ts = excluded.ts""",
                (
                    task.event_id,
                    task.target_id,
                    state,
                    result.code,
                    result.detail,
                    task.attempts + 1,
                    now,
                ),
            )
            self._db.commit()

    def _defer_sync(self, task: Task, next_at: float) -> None:
        """限流延后：只推迟时间，不增加 attempts、不写回执。"""
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET state = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (PENDING, next_at, time.time(), task.id),
            )
            self._db.commit()

    def _receipts_sync(self, event_id: str) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM receipts WHERE event_id = ?", (event_id,)
            )
            return [dict(r) for r in cur.fetchall()]

    def _stats_sync(self) -> dict:
        with self._lock:
            cur = self._db.execute("SELECT state, COUNT(*) c FROM tasks GROUP BY state")
            by_state = {r["state"]: r["c"] for r in cur.fetchall()}
            cur = self._db.execute(
                "SELECT target_id, COUNT(*) c FROM tasks WHERE state IN (?,?) GROUP BY target_id",
                (PENDING, INFLIGHT),
            )
            backlog = {r["target_id"]: r["c"] for r in cur.fetchall()}
        return {"tasks": by_state, "backlog": backlog}

    def _recent_failures_sync(self, limit: int) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                """SELECT event_id, target_id, attempts, last_error, updated_at
                     FROM tasks WHERE state = ?
                    ORDER BY updated_at DESC LIMIT ?""",
                (DEAD, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def _reset_inflight_sync(self) -> int:
        """进程启动时把上次遗留的 inflight 复位为 pending。"""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE state = ?",
                (PENDING, time.time(), INFLIGHT),
            )
            self._db.commit()
            return cur.rowcount

    # ---------------- 异步包装 ----------------

    async def enqueue(self, n: Notification, target_ids: list[str]) -> int:
        payload = n.to_dict()
        await asyncio.to_thread(
            self._enqueue_sync, [(n.event_id, t, payload) for t in target_ids]
        )
        return len(target_ids)

    async def claim(self, limit: int = 32) -> list[Task]:
        return await asyncio.to_thread(self._claim_sync, limit)

    async def settle(self, task: Task, result: SendResult, next_at: float | None) -> None:
        await asyncio.to_thread(self._settle_sync, task, result, next_at)

    async def defer(self, task: Task, next_at: float) -> None:
        await asyncio.to_thread(self._defer_sync, task, next_at)

    async def receipts(self, event_id: str) -> list[dict]:
        return await asyncio.to_thread(self._receipts_sync, event_id)

    async def stats(self) -> dict:
        return await asyncio.to_thread(self._stats_sync)

    async def recent_failures(self, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self._recent_failures_sync, limit)

    async def recover(self) -> int:
        return await asyncio.to_thread(self._reset_inflight_sync)

    def close(self) -> None:
        with self._lock:
            self._db.close()
