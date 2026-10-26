"""分析查询（只读，独立于业务库）。

背景：业务库（work_orders/tasks/…）保持小而快；**作业事实数据量极大**
（默认 1 亿条，见 tools/gen_bulk_data.py），单独落在分析库，走只读连接。

两层读模型（关键设计）：
1. **预聚合表**（`agg_daily*` / `agg_hourly`，由 tools/build_rollup.py 离线构建）
   —— 聚合口径固定，离线算好；在线只读几千行，毫秒级。
   实测教训：直接对 1 亿条按时间窗聚合会退化为海量随机回表（1 天 61.6s / 7 天 173.5s）。
2. **明细点查**（`timeline`）—— 走主键/二级索引，本来就是毫秒级。

纪律：
- 只读连接（`mode=ro`），永不写入；库不存在时优雅降级为 `available=false`
- 有 rollup 走 rollup，无 rollup 自动回退明细扫描（保证小样本库/测试可用）
- 整数码 → 语义名在本层还原（存储紧凑，展示可读）
- 查询结果由上层 `MultiLevelCache` 缓存（Redis 预热见 tools/warmup_cache.py）
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

KIND_CODES = ["TRANSPORT", "INSPECT", "LOAD_UNLOAD", "CLEAN", "CHARGE"]
STATUS_CODES = ["PENDING", "APPROVED", "ASSIGNED", "EXECUTING", "VERIFYING",
                "DONE", "FAILED", "ABORTED", "SUPERSEDED"]
SKILL_CODES = ["transport", "pick_place", "scan", "photo", "clean",
               "dock_charge", "inspect_thermal", "inspect_gauge"]
ACTION_CODES = ["SUBMIT", "APPROVE", "ASSIGN", "EXECUTE", "VERIFY",
                "RED_LINE_BLOCK", "SAFETY_HALT", "ESTOP", "SKILL_APPROVE",
                "ROLLBACK"]

# 时长分桶上界（毫秒），与 tools/build_rollup.py 的 BUCKETS 一一对应
BUCKET_UPPER = [1_000, 5_000, 30_000, 60_000, 120_000, 240_000]


def _name(codes: list[str], idx: int) -> str:
    return codes[idx] if 0 <= idx < len(codes) else f"UNKNOWN_{idx}"


def _p95_from_buckets(buckets: list[int]) -> float:
    """由分桶累计分布估算 p95（返回桶上界）。"""
    total = sum(buckets)
    if not total:
        return 0.0
    target = total * 0.95
    cum = 0
    for i, b in enumerate(buckets):
        cum += b
        if cum >= target:
            return float(BUCKET_UPPER[i])
    return float(BUCKET_UPPER[-1])


class AnalyticsStore:
    """分析库只读仓储（rollup 优先，明细回退）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path) if path else ""
        self._available = bool(self._path) and Path(self._path).exists()
        self._conn: sqlite3.Connection | None = None
        self._rollup = False
        if self._available:
            try:
                # 只读打开：不可能误写；WAL 下读不阻塞
                self._conn = sqlite3.connect(f"file:{self._path}?mode=ro",
                                             uri=True, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA query_only=ON")
                self._conn.execute("PRAGMA cache_size=-131072")   # 128 MiB
                self._rollup = self._has_rollup()
            except sqlite3.Error:
                self._available = False
                self._conn = None

    def _has_rollup(self) -> bool:
        assert self._conn is not None
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' "
                "AND name IN ('agg_daily','agg_daily_skill','agg_hourly')"
            ).fetchone()
            return int(row["c"]) == 3
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------ 元信息
    @property
    def available(self) -> bool:
        return self._available and self._conn is not None

    @property
    def rollup(self) -> bool:
        return self._rollup

    def scale(self) -> dict[str, Any]:
        """数据规模（行数/文件大小），用于看板与报告。"""
        if not self.available:
            return {"available": False}
        assert self._conn is not None
        counts: dict[str, int] = {}
        for t in ("wo_fact", "task_fact", "episode_fact", "audit_fact"):
            counts[t] = int(self._conn.execute(
                f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        size = 0
        for suffix in ("", "-wal"):
            p = Path(self._path + suffix)
            if p.exists():
                size += p.stat().st_size
        return {"available": True, "tables": counts,
                "total_rows": sum(counts.values()),
                "db_size_gib": round(size / 1024 ** 3, 2),
                "rollup": self._rollup, "path": self._path}

    # ------------------------------------------------------------ 聚合
    def summary(self, days: int = 7) -> dict[str, Any]:
        """近 N 天作业概览（rollup 优先）。"""
        if not self.available:
            return {"available": False, "days": days}
        return self._summary_rollup(days) if self._rollup else self._summary_raw(days)

    def _summary_rollup(self, days: int) -> dict[str, Any]:
        assert self._conn is not None
        t0 = time.perf_counter()
        since_day = (int(time.time()) - days * 86400) // 86400
        rows = self._conn.execute(
            "SELECT kind, SUM(orders) o, SUM(done) d, SUM(failed) f, "
            "SUM(aborted) a, SUM(orders_ms_sum) ms "
            "FROM agg_daily WHERE day>=? GROUP BY kind",
            (since_day,)).fetchall()
        orders = sum(int(x["o"]) for x in rows)
        done = sum(int(x["d"]) for x in rows)
        failed = sum(int(x["f"]) for x in rows)
        aborted = sum(int(x["a"]) for x in rows)
        ms_sum = sum(int(x["ms"]) for x in rows)
        task_row = self._conn.execute(
            "SELECT COALESCE(SUM(tasks),0) t, COALESCE(SUM(retries),0) r "
            "FROM agg_daily_task WHERE day>=?", (since_day,)).fetchone()
        tasks, retries = int(task_row["t"]), int(task_row["r"])

        by_kind = [{"kind": _name(KIND_CODES, int(x["kind"])),
                    "orders": int(x["o"]),
                    "success_rate": round(int(x["d"]) / int(x["o"]), 4)
                    if x["o"] else 0.0}
                   for x in sorted(rows, key=lambda r: -int(r["o"]))]

        sk = self._conn.execute(
            "SELECT skill, SUM(episodes) e, SUM(success) s, SUM(ms_sum) ms, "
            "SUM(b0) b0, SUM(b1) b1, SUM(b2) b2, SUM(b3) b3, SUM(b4) b4, "
            "SUM(b5) b5 FROM agg_daily_skill WHERE day>=? GROUP BY skill",
            (since_day,)).fetchall()
        episodes = sum(int(x["e"]) for x in sk)
        ep_ok = sum(int(x["s"]) for x in sk)
        ep_ms = sum(int(x["ms"]) for x in sk)
        buckets = [sum(int(x[f"b{i}"]) for x in sk) for i in range(6)]

        by_skill = [{"skill": _name(SKILL_CODES, int(x["skill"])),
                     "episodes": int(x["e"]),
                     "success_rate": round(int(x["s"]) / int(x["e"]), 4)
                     if x["e"] else 0.0}
                    for x in sorted(sk, key=lambda r: -int(r["e"]))]

        ac = self._conn.execute(
            "SELECT action, SUM(cnt) c FROM agg_daily_action "
            "WHERE day>=? GROUP BY action", (since_day,)).fetchall()
        acts = {int(x["action"]): int(x["c"]) for x in ac}

        return {
            "available": True,
            "days": days,
            "source": "rollup",
            "orders": {"total": orders,
                       "by_status": {"DONE": done, "FAILED": failed,
                                     "ABORTED": aborted},
                       "avg_duration_ms": round(ms_sum / orders, 1) if orders else 0.0},
            "tasks": {"total": tasks,
                      "avg_retries": round(retries / tasks, 3) if tasks else 0.0},
            "episodes": {"total": episodes,
                         "success_rate": round(ep_ok / episodes, 4) if episodes else 0.0,
                         "avg_duration_ms": round(ep_ms / episodes, 1) if episodes else 0.0,
                         "p95_duration_ms": _p95_from_buckets(buckets),
                         "p95_basis": "bucket_estimate"},
            "by_kind": by_kind,
            "by_skill": by_skill,
            "safety": {"halt": acts.get(6, 0) + acts.get(7, 0),
                       "red_line_block": acts.get(5, 0)},
            "query_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    def _summary_raw(self, days: int) -> dict[str, Any]:
        """回退：无 rollup 时直接扫明细（小样本库/测试用）。"""
        assert self._conn is not None
        since = int(time.time()) - days * 86400
        t0 = time.perf_counter()

        by_status = {_name(STATUS_CODES, int(r["status"])): int(r["c"]) for r in
                     self._conn.execute(
                         "SELECT status, COUNT(*) c FROM wo_fact "
                         "WHERE created_ts>=? GROUP BY status", (since,))}
        orders = sum(by_status.values())

        row = self._conn.execute(
            "SELECT COUNT(*) c, AVG(duration_ms) d FROM wo_fact "
            "WHERE created_ts>=?", (since,)).fetchone()
        avg_order_ms = round(float(row["d"] or 0), 1)

        by_kind = [{"kind": _name(KIND_CODES, int(r["kind"])),
                    "orders": int(r["c"]),
                    "success_rate": round(float(r["s"] or 0), 4)}
                   for r in self._conn.execute(
                       "SELECT kind, COUNT(*) c, AVG(success) s FROM wo_fact "
                       "WHERE created_ts>=? GROUP BY kind ORDER BY c DESC",
                       (since,))]

        ep = self._conn.execute(
            "SELECT COUNT(*) c, AVG(success) s, AVG(duration_ms) d "
            "FROM episode_fact WHERE ts>=?", (since,)).fetchone()
        # p95 用抽样估计（全量排序代价过高）
        sample = [float(r[0]) for r in self._conn.execute(
            "SELECT duration_ms FROM episode_fact WHERE ts>=? LIMIT 20000",
            (since,))]
        sample.sort()
        p95 = round(sample[int(len(sample) * 0.95)] if sample else 0.0, 1)

        by_skill = [{"skill": _name(SKILL_CODES, int(r["skill"])),
                     "episodes": int(r["c"]),
                     "success_rate": round(float(r["s"] or 0), 4)}
                    for r in self._conn.execute(
                        "SELECT skill, COUNT(*) c, AVG(success) s "
                        "FROM episode_fact WHERE ts>=? GROUP BY skill "
                        "ORDER BY c DESC", (since,))]

        task_row = self._conn.execute(
            "SELECT COUNT(*) c, AVG(retries) r FROM task_fact WHERE start_ts>=?",
            (since,)).fetchone()

        halted = self._conn.execute(
            "SELECT COUNT(*) FROM audit_fact WHERE ts>=? AND action IN (6,7)",
            (since,)).fetchone()[0]
        blocked = self._conn.execute(
            "SELECT COUNT(*) FROM audit_fact WHERE ts>=? AND action=5",
            (since,)).fetchone()[0]

        return {
            "available": True,
            "days": days,
            "source": "raw",
            "orders": {"total": orders, "by_status": by_status,
                       "avg_duration_ms": avg_order_ms},
            "tasks": {"total": int(task_row["c"]),
                      "avg_retries": round(float(task_row["r"] or 0), 3)},
            "episodes": {"total": int(ep["c"]),
                         "success_rate": round(float(ep["s"] or 0), 4),
                         "avg_duration_ms": round(float(ep["d"] or 0), 1),
                         "p95_duration_ms": p95},
            "by_kind": by_kind,
            "by_skill": by_skill,
            "safety": {"halt": int(halted), "red_line_block": int(blocked)},
            "query_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    def hourly(self, days: int = 1) -> dict[str, Any]:
        """按小时吞吐（rollup 优先）。"""
        if not self.available:
            return {"available": False}
        assert self._conn is not None
        since = int(time.time()) - days * 86400
        t0 = time.perf_counter()
        if self._rollup:
            rows = self._conn.execute(
                "SELECT hour_ts h, orders c FROM agg_hourly WHERE hour_ts>=? "
                "ORDER BY hour_ts", (since,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT (created_ts/3600)*3600 h, COUNT(*) c FROM wo_fact "
                "WHERE created_ts>=? GROUP BY h ORDER BY h", (since,)).fetchall()
        return {"available": True, "days": days,
                "source": "rollup" if self._rollup else "raw",
                "query_ms": round((time.perf_counter() - t0) * 1000, 2),
                "buckets": [{"ts": int(r["h"]), "orders": int(r["c"])}
                            for r in rows]}

    def timeline(self, order_id: int) -> dict[str, Any]:
        """单作业单时间线（明细点查，主键 + order_id 索引）。"""
        if not self.available:
            return {"available": False}
        assert self._conn is not None
        t0 = time.perf_counter()
        o = self._conn.execute(
            "SELECT * FROM wo_fact WHERE order_id=?", (order_id,)).fetchone()
        if o is None:
            return {"available": True, "found": False, "order_id": order_id}
        tasks = [{"task_id": int(t["task_id"]),
                  "kind": _name(KIND_CODES, int(t["kind"])),
                  "skill": _name(SKILL_CODES, int(t["skill"])),
                  "robot": int(t["robot"]),
                  "status": _name(STATUS_CODES, int(t["status"])),
                  "start_ts": int(t["start_ts"]),
                  "duration_ms": int(t["duration_ms"]),
                  "retries": int(t["retries"])}
                 for t in self._conn.execute(
                     "SELECT * FROM task_fact WHERE order_id=? "
                     "ORDER BY start_ts LIMIT 50", (order_id,))]
        return {"available": True, "found": True, "order_id": order_id,
                "query_ms": round((time.perf_counter() - t0) * 1000, 2),
                "order": {"site": int(o["site"]),
                          "kind": _name(KIND_CODES, int(o["kind"])),
                          "status": _name(STATUS_CODES, int(o["status"])),
                          "created_ts": int(o["created_ts"]),
                          "duration_ms": int(o["duration_ms"]),
                          "success": bool(o["success"])},
                "tasks": tasks}

    def audit_stats(self, days: int = 7) -> dict[str, Any]:
        """审计动作分布（安全运营口径，rollup 优先）。"""
        if not self.available:
            return {"available": False}
        assert self._conn is not None
        t0 = time.perf_counter()
        if self._rollup:
            since_day = (int(time.time()) - days * 86400) // 86400
            rows = self._conn.execute(
                "SELECT action, SUM(cnt) c FROM agg_daily_action "
                "WHERE day>=? GROUP BY action ORDER BY c DESC",
                (since_day,)).fetchall()
        else:
            since = int(time.time()) - days * 86400
            rows = self._conn.execute(
                "SELECT action, COUNT(*) c FROM audit_fact WHERE ts>=? "
                "GROUP BY action ORDER BY c DESC", (since,)).fetchall()
        return {"available": True, "days": days,
                "source": "rollup" if self._rollup else "raw",
                "query_ms": round((time.perf_counter() - t0) * 1000, 2),
                "by_action": [{"action": _name(ACTION_CODES, int(r["action"])),
                               "count": int(r["c"])} for r in rows]}
