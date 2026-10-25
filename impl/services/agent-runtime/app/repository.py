"""持久化与读写分离（SQLite 双连接）。

架构（对标 CQRS 语义）：
- **写侧（Command）**：主库连接，负责作业单/任务/episode/技能/审计写入
- **读侧（Query）**：从库连接（WAL 模式下同库多读连接），负责看板/查询
- 读写分离的首要价值是**语义清晰**（命令与查询各自演化），其次才是性能

表（六张）：
- work_orders / tasks / episodes / skills / audit_log / zones

纪律：
- 所有写走主连接；读走从连接；禁止跨连接混用（连接即语义边界）
- 迁移内建（CREATE TABLE IF NOT EXISTS），零外部依赖
- 生产可平移至 PostgreSQL（repository 接口不变，见 docs/09）
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    order_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    params TEXT NOT NULL,
    priority INTEGER DEFAULT 5,
    requested_by TEXT DEFAULT '',
    status TEXT DEFAULT 'PENDING',
    trace_id TEXT DEFAULT '',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    params TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_to TEXT,
    attempts INTEGER DEFAULT 0,
    note TEXT DEFAULT '',
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_order ON tasks(order_id);
CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    task_id TEXT, robot_id TEXT, skill_id TEXT, skill_version TEXT,
    model_version TEXT, success INTEGER, duration_s REAL,
    consent_id TEXT, checksum TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT, version TEXT, state TEXT, entry TEXT,
    canary_pct INTEGER DEFAULT 0, approved_by TEXT DEFAULT '',
    updated_at REAL,
    PRIMARY KEY (skill_id, version)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL, actor TEXT, action TEXT, target TEXT, detail TEXT,
    trace_id TEXT
);
CREATE TABLE IF NOT EXISTS zones (
    zone_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
    x_min REAL, y_min REAL, x_max REAL, y_max REAL, speed_limit REAL
);
"""


class Repository:
    """SQLite 仓储：写主读从（WAL）。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        if self._path == ":memory:":
            # 共享缓存内存库：写主/读从两个连接看到同一库（生产为文件库，WAL）；
            # 唯一名保证实例间隔离（测试并行安全）
            self._path = f"file:tiangong_{uuid.uuid4().hex[:8]}?mode=memory&cache=shared"
            self._shared_mem = True
        else:
            self._shared_mem = False
        # 主连接（写）
        self._w = sqlite3.connect(self._path, check_same_thread=False,
                                  uri=self._shared_mem or False)
        self._w.row_factory = sqlite3.Row
        # 从连接（读；WAL 下可与写并发）
        self._r = sqlite3.connect(self._path, check_same_thread=False,
                                  uri=self._shared_mem or False)
        self._r.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        if self._path != ":memory:":
            self._w.execute("PRAGMA journal_mode=WAL")
            self._w.execute("PRAGMA synchronous=NORMAL")
        self._w.executescript(_SCHEMA)
        self._w.commit()

    def close(self) -> None:
        self._w.close()
        self._r.close()

    # ------------------------------------------------------------ 写侧
    def save_order(self, order_id: str, kind: str, params: dict[str, Any],
                   priority: int, requested_by: str, trace_id: str,
                   status: str = "PENDING") -> None:
        with self._lock, self._w:
            self._w.execute(
                "INSERT OR REPLACE INTO work_orders VALUES (?,?,?,?,?,?,?,?)",
                (order_id, kind, json.dumps(params, ensure_ascii=False),
                 priority, requested_by, status, trace_id, time.time()))

    def set_order_status(self, order_id: str, status: str) -> None:
        with self._lock, self._w:
            self._w.execute("UPDATE work_orders SET status=? WHERE order_id=?",
                            (status, order_id))

    def save_task(self, task: Any) -> None:
        with self._lock, self._w:
            self._w.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task.task_id, task.work_order_id, task.kind,
                 json.dumps(dict(task.params), ensure_ascii=False),
                 json.dumps(list(task.depends_on)),
                 task.status.value, task.assigned_to, task.attempts,
                 task.note, time.time()))

    def save_episode(self, ep: Any) -> None:
        with self._lock, self._w:
            self._w.execute(
                "INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ep.episode_id, ep.task_id, ep.robot_id, ep.skill_id,
                 ep.skill_version, ep.model_version, int(ep.success),
                 ep.duration_s, ep.consent_id, ep.checksum(), time.time()))

    def save_skill(self, skill_id: str, version: str, state: str, entry: str,
                   canary_pct: int, approved_by: str) -> None:
        with self._lock, self._w:
            self._w.execute(
                "INSERT OR REPLACE INTO skills VALUES (?,?,?,?,?,?,?)",
                (skill_id, version, state, entry, canary_pct, approved_by,
                 time.time()))

    def audit(self, actor: str, action: str, target: str,
              detail: str = "", trace_id: str = "") -> None:
        with self._lock, self._w:
            self._w.execute(
                "INSERT INTO audit_log (at, actor, action, target, detail, trace_id)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), actor, action, target, detail, trace_id))

    def save_zone(self, zone: Any) -> None:
        with self._lock, self._w:
            self._w.execute(
                "INSERT OR REPLACE INTO zones VALUES (?,?,?,?,?,?,?)",
                (zone.zone_id, zone.kind.value, zone.x_min, zone.y_min,
                 zone.x_max, zone.y_max, zone.speed_limit))

    # ------------------------------------------------------------ 读侧
    def get_order(self, order_id: str) -> dict[str, Any] | None:
        row = self._r.execute(
            "SELECT * FROM work_orders WHERE order_id=?", (order_id,)).fetchone()
        return self._row(row) if row else None

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._r.execute(
            "SELECT * FROM work_orders ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def get_tasks(self, order_id: str) -> list[dict[str, Any]]:
        rows = self._r.execute(
            "SELECT * FROM tasks WHERE order_id=? ORDER BY task_id",
            (order_id,)).fetchall()
        return [self._row(r) for r in rows]

    def count_episodes(self) -> int:
        return self._r.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]

    def episode_success_rate(self, skill_id: str) -> float | None:
        row = self._r.execute(
            "SELECT AVG(success) r FROM episodes WHERE skill_id=?",
            (skill_id,)).fetchone()
        return None if row["r"] is None else round(float(row["r"]), 4)

    def audit_trail(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._r.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def active_skills(self) -> list[dict[str, Any]]:
        rows = self._r.execute(
            "SELECT * FROM skills WHERE state='APPROVED'").fetchall()
        return [self._row(r) for r in rows]

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("params", "depends_on"):
            if k in d and isinstance(d[k], str):
                with suppress(ValueError, TypeError):
                    d[k] = json.loads(d[k])
        return d
