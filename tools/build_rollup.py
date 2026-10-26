"""构建预聚合表（rollup）：1 亿条事实库的"读模型"。

为什么必须预聚合（实测教训）：
- 直接对 1 亿条做 `WHERE created_ts >= ?` 聚合，走 `*_ts` 索引会退化成
  **海量随机回表**：实测 1 天 61.6s、7 天 173.5s（5.03 GiB 库，本机 SSD）。
- 聚合口径固定（按天/小时/类型/技能/动作），完全可以离线算好——
  这是数仓的标准做法（物化视图/宽表），把随机 I/O 换成一次顺序扫描。

产出四张读模型表（行数极小，全量 90 天约 3 万行）：
    agg_daily          按天 × 基地 × 类型：单量/成功/失败/中止/时长和/任务/重试
    agg_daily_skill    按天 × 技能：episode 数/成功数/时长和/时长分桶（用于 p95 估算）
    agg_daily_action   按天 × 动作：审计动作分布（安全运营口径）
    agg_hourly         按小时：作业单吞吐（趋势图）

用法：
    python tools/build_rollup.py                       # 默认 data/tiangong_bulk.db
    python tools/build_rollup.py --db path --rebuild   # 重算（先 DROP）
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "tiangong_bulk.db"

DDL = """
CREATE TABLE IF NOT EXISTS agg_daily (
    day INTEGER NOT NULL,
    site INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    orders INTEGER NOT NULL,
    done INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    aborted INTEGER NOT NULL,
    orders_ms_sum INTEGER NOT NULL,
    tasks INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, site, kind)
);
CREATE TABLE IF NOT EXISTS agg_daily_skill (
    day INTEGER NOT NULL,
    skill INTEGER NOT NULL,
    episodes INTEGER NOT NULL,
    success INTEGER NOT NULL,
    ms_sum INTEGER NOT NULL,
    b0 INTEGER NOT NULL, b1 INTEGER NOT NULL, b2 INTEGER NOT NULL,
    b3 INTEGER NOT NULL, b4 INTEGER NOT NULL, b5 INTEGER NOT NULL,
    PRIMARY KEY (day, skill)
);
CREATE TABLE IF NOT EXISTS agg_daily_action (
    day INTEGER NOT NULL,
    action INTEGER NOT NULL,
    cnt INTEGER NOT NULL,
    PRIMARY KEY (day, action)
);
CREATE TABLE IF NOT EXISTS agg_daily_task (
    day INTEGER PRIMARY KEY,
    tasks INTEGER NOT NULL,
    retries INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agg_hourly (
    hour_ts INTEGER PRIMARY KEY,
    orders INTEGER NOT NULL
);
"""

# 时长分桶（毫秒）：<1s, 1-5s, 5-30s, 30-60s, 60-120s, >=120s
BUCKETS = """
    SUM(CASE WHEN duration_ms < 1000 THEN 1 ELSE 0 END),
    SUM(CASE WHEN duration_ms >= 1000 AND duration_ms < 5000 THEN 1 ELSE 0 END),
    SUM(CASE WHEN duration_ms >= 5000 AND duration_ms < 30000 THEN 1 ELSE 0 END),
    SUM(CASE WHEN duration_ms >= 30000 AND duration_ms < 60000 THEN 1 ELSE 0 END),
    SUM(CASE WHEN duration_ms >= 60000 AND duration_ms < 120000 THEN 1 ELSE 0 END),
    SUM(CASE WHEN duration_ms >= 120000 THEN 1 ELSE 0 END)
"""


def _run(conn: sqlite3.Connection, name: str, sql: str) -> float:
    t0 = time.perf_counter()
    conn.execute(sql)
    conn.commit()
    dt = time.perf_counter() - t0
    print(f"  {name:<22} {dt:7.2f}s", flush=True)
    return dt


def build(db: Path, rebuild: bool) -> int:
    if not db.exists():
        print(f"库不存在: {db}")
        return 2
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-524288")     # 512 MiB：让 GROUP BY 尽量在内存排序
    conn.execute("PRAGMA temp_store=MEMORY")

    if rebuild:
        for t in ("agg_daily", "agg_daily_skill", "agg_daily_action",
                  "agg_daily_task", "agg_hourly"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        print("已删除旧聚合表")
    conn.executescript(DDL)

    print("构建预聚合（一次顺序扫描，替代海量随机回表）...")
    _run(conn, "agg_daily", f"""
        INSERT OR REPLACE INTO agg_daily
        SELECT created_ts/86400 AS day, site, kind,
               COUNT(*),
               SUM(CASE WHEN status=5 THEN 1 ELSE 0 END),
               SUM(CASE WHEN status=6 THEN 1 ELSE 0 END),
               SUM(CASE WHEN status=7 THEN 1 ELSE 0 END),
               CAST(SUM(duration_ms) AS INTEGER), 0, 0
        FROM wo_fact GROUP BY day, site, kind
    """)

    # 任务按「天」单行聚合（任务表无 site 维度，若按 (day,site,kind) 存会重复计数）
    _run(conn, "agg_daily_task", """
        INSERT OR REPLACE INTO agg_daily_task
        SELECT (start_ts/86400) AS day, COUNT(*), SUM(retries)
        FROM task_fact GROUP BY day
    """)

    _run(conn, "agg_daily_skill", f"""
        INSERT OR REPLACE INTO agg_daily_skill
        SELECT ts/86400 AS day, skill, COUNT(*), SUM(success),
               CAST(SUM(duration_ms) AS INTEGER), {BUCKETS}
        FROM episode_fact GROUP BY day, skill
    """)

    _run(conn, "agg_daily_action", """
        INSERT OR REPLACE INTO agg_daily_action
        SELECT ts/86400 AS day, action, COUNT(*)
        FROM audit_fact GROUP BY day, action
    """)

    _run(conn, "agg_hourly", """
        INSERT OR REPLACE INTO agg_hourly
        SELECT (created_ts/3600)*3600 AS hour_ts, COUNT(*)
        FROM wo_fact GROUP BY hour_ts
    """)

    print("统计：")
    for t in ("agg_daily", "agg_daily_skill", "agg_daily_action",
              "agg_daily_task", "agg_hourly"):
        c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<18} {c:>10,} 行")
    conn.close()
    size = db.stat().st_size
    print(f"库文件: {size / 1024 ** 3:.2f} GiB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    return build(Path(args.db), args.rebuild)


if __name__ == "__main__":
    raise SystemExit(main())
