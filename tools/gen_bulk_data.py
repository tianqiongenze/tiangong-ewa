"""生成天工分析库（bulk）合成数据：默认 1 亿条。

设计要点：
- **紧凑整数模型**：全表用 INTEGER 编码（kind/status/robot 等均为小整数码），
  单行约 25–40 字节，1 亿条约 3.5–4.5 GB，可在普通开发机落盘。
- **分批写入**：每批 10 万行 executemany，单事务；装载期 `synchronous=OFF`
  且 `journal_mode=MEMORY`（离线灌数，可接受断电重跑），装载后建索引并切回 WAL。
- **时间窗**：默认近 90 天，便于「近 7 天/30 天」聚合查询与缓存预热。
- **可复现**：固定随机种子，同一 `--rows/--seed` 生成结果一致。

表（四张，合计 = --rows）：
    wo_fact       作业单事实      15%
    task_fact     任务事实        40%
    episode_fact  技能执行事实    35%
    audit_fact    审计事实        10%

用法：
    python tools/gen_bulk_data.py                    # 1 亿条 → data/tiangong_bulk.db
    python tools/gen_bulk_data.py --rows 1000000     # 100 万条（CI/冒烟）
    python tools/gen_bulk_data.py --rows 100000000 --db data/tiangong_bulk.db --force
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "tiangong_bulk.db"

# 与领域枚举对齐的整数码（分析库只存码，语义在 analytics.py 还原）
KIND_CODES = ["TRANSPORT", "INSPECT", "LOAD_UNLOAD", "CLEAN", "CHARGE"]
STATUS_CODES = ["PENDING", "APPROVED", "ASSIGNED", "EXECUTING", "VERIFYING",
                "DONE", "FAILED", "ABORTED", "SUPERSEDED"]
SKILL_CODES = ["transport", "pick_place", "scan", "photo", "clean",
               "dock_charge", "inspect_thermal", "inspect_gauge"]
ACTION_CODES = ["SUBMIT", "APPROVE", "ASSIGN", "EXECUTE", "VERIFY",
                "RED_LINE_BLOCK", "SAFETY_HALT", "ESTOP", "SKILL_APPROVE",
                "ROLLBACK"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS wo_fact (
    order_id     INTEGER PRIMARY KEY,
    site         INTEGER NOT NULL,
    kind         INTEGER NOT NULL,
    status       INTEGER NOT NULL,
    created_ts   INTEGER NOT NULL,
    duration_ms  INTEGER NOT NULL,
    robot        INTEGER NOT NULL,
    success      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_fact (
    task_id      INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL,
    kind         INTEGER NOT NULL,
    robot        INTEGER NOT NULL,
    skill        INTEGER NOT NULL,
    status       INTEGER NOT NULL,
    start_ts     INTEGER NOT NULL,
    duration_ms  INTEGER NOT NULL,
    retries      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS episode_fact (
    episode_id   INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL,
    robot        INTEGER NOT NULL,
    skill        INTEGER NOT NULL,
    success      INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    duration_ms  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_fact (
    id           INTEGER PRIMARY KEY,
    ts           INTEGER NOT NULL,
    actor        INTEGER NOT NULL,
    action       INTEGER NOT NULL,
    target       INTEGER NOT NULL
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_wo_ts ON wo_fact(created_ts)",
    "CREATE INDEX IF NOT EXISTS idx_wo_site_ts ON wo_fact(site, created_ts)",
    "CREATE INDEX IF NOT EXISTS idx_task_order ON task_fact(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_ts ON task_fact(start_ts)",
    "CREATE INDEX IF NOT EXISTS idx_ep_ts ON episode_fact(ts)",
    "CREATE INDEX IF NOT EXISTS idx_ep_skill_ts ON episode_fact(skill, ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_fact(ts)",
]

# 权重（贴近真实产线：绝大多数成功，少量失败/中止）
STATUS_W = [1, 2, 3, 2, 2, 84, 4, 1, 1]
KIND_W = [52, 18, 18, 7, 5]
SKILL_W = [30, 26, 16, 8, 6, 6, 4, 4]


def _split(total: int) -> tuple[int, int, int, int]:
    """按比例切分（保证合计等于 total）。"""
    wo = int(total * 0.15)
    task = int(total * 0.40)
    ep = int(total * 0.35)
    audit = total - wo - task - ep
    return wo, task, ep, audit


def _progress(tag: str, done: int, total: int, t0: float) -> None:
    elapsed = max(time.time() - t0, 1e-6)
    rate = done / elapsed
    eta = (total - done) / rate if rate else 0
    pct = done / total * 100
    print(f"  [{tag}] {done:>12,}/{total:,} ({pct:5.1f}%)  "
          f"{rate:,.0f} 行/s  ETA {eta:,.0f}s", flush=True)


def gen_wo(conn: sqlite3.Connection, n: int, base_ts: int, span: int,
           rnd: random.Random, batch: int = 100_000) -> None:
    t0 = time.time()
    sql = "INSERT INTO wo_fact VALUES (?,?,?,?,?,?,?,?)"
    for start in range(1, n + 1, batch):
        end = min(start + batch, n + 1)
        rows = []
        for i in range(start, end):
            status = rnd.choices(range(9), weights=STATUS_W, k=1)[0]
            rows.append((
                i,
                rnd.randrange(3),
                rnd.choices(range(5), weights=KIND_W, k=1)[0],
                status,
                base_ts - rnd.randrange(span),
                rnd.randrange(3_000, 900_000),
                rnd.randrange(12),
                1 if status == 5 else 0,
            ))
        conn.executemany(sql, rows)
        conn.commit()
        _progress("wo_fact", end - 1, n, t0)


def gen_task(conn: sqlite3.Connection, n: int, wo_n: int, base_ts: int,
             span: int, rnd: random.Random, batch: int = 100_000) -> None:
    t0 = time.time()
    sql = "INSERT INTO task_fact VALUES (?,?,?,?,?,?,?,?,?)"
    for start in range(1, n + 1, batch):
        end = min(start + batch, n + 1)
        rows = []
        for i in range(start, end):
            status = rnd.choices(range(9), weights=STATUS_W, k=1)[0]
            rows.append((
                i,
                rnd.randrange(1, wo_n + 1),
                rnd.choices(range(5), weights=KIND_W, k=1)[0],
                rnd.randrange(12),
                rnd.choices(range(8), weights=SKILL_W, k=1)[0],
                status,
                base_ts - rnd.randrange(span),
                rnd.randrange(800, 300_000),
                rnd.choices([0, 1, 2], weights=[88, 10, 2], k=1)[0],
            ))
        conn.executemany(sql, rows)
        conn.commit()
        _progress("task_fact", end - 1, n, t0)


def gen_episode(conn: sqlite3.Connection, n: int, task_n: int, base_ts: int,
                span: int, rnd: random.Random, batch: int = 100_000) -> None:
    t0 = time.time()
    sql = "INSERT INTO episode_fact VALUES (?,?,?,?,?,?,?)"
    for start in range(1, n + 1, batch):
        end = min(start + batch, n + 1)
        rows = []
        for i in range(start, end):
            ok = 1 if rnd.random() < 0.968 else 0
            rows.append((
                i,
                rnd.randrange(1, task_n + 1),
                rnd.randrange(12),
                rnd.choices(range(8), weights=SKILL_W, k=1)[0],
                ok,
                base_ts - rnd.randrange(span),
                rnd.randrange(500, 240_000),
            ))
        conn.executemany(sql, rows)
        conn.commit()
        _progress("episode_fact", end - 1, n, t0)


def gen_audit(conn: sqlite3.Connection, n: int, base_ts: int, span: int,
              rnd: random.Random, batch: int = 100_000) -> None:
    t0 = time.time()
    sql = "INSERT INTO audit_fact VALUES (?,?,?,?,?)"
    for start in range(1, n + 1, batch):
        end = min(start + batch, n + 1)
        rows = []
        for i in range(start, end):
            rows.append((
                i,
                base_ts - rnd.randrange(span),
                rnd.randrange(6),
                rnd.choices(range(10), weights=[30, 22, 12, 20, 8, 3, 2, 1, 1, 1],
                            k=1)[0],
                rnd.randrange(1, 1_000_000),
            ))
        conn.executemany(sql, rows)
        conn.commit()
        _progress("audit_fact", end - 1, n, t0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100_000_000,
                    help="总行数（四表合计），默认 1 亿")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--days", type=int, default=90, help="时间窗天数")
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--batch", type=int, default=100_000)
    ap.add_argument("--force", action="store_true", help="删除已有库重建")
    args = ap.parse_args()

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    if args.force and db.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()
        print(f"已删除旧库: {db}")

    wo_n, task_n, ep_n, audit_n = _split(args.rows)
    rnd = random.Random(args.seed)
    base_ts = int(time.time())
    span = args.days * 86400

    print(f"目标库: {db}")
    print(f"总行数: {args.rows:,} → wo {wo_n:,} / task {task_n:,} / "
          f"episode {ep_n:,} / audit {audit_n:,}")
    print(f"时间窗: 近 {args.days} 天 | 种子: {args.seed} | 批大小: {args.batch:,}")

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-262144")      # 256 MiB
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.executescript(SCHEMA)
    conn.commit()

    t_all = time.time()
    gen_wo(conn, wo_n, base_ts, span, rnd, args.batch)
    gen_task(conn, task_n, wo_n, base_ts, span, rnd, args.batch)
    gen_episode(conn, ep_n, task_n, base_ts, span, rnd, args.batch)
    gen_audit(conn, audit_n, base_ts, span, rnd, args.batch)

    print("建索引 ...")
    t_idx = time.time()
    for sql in INDEXES:
        conn.execute(sql)
    conn.commit()
    print(f"索引完成，用时 {time.time() - t_idx:,.0f}s")

    total = (wo_n + task_n + ep_n + audit_n)
    print(f"装载完成：{total:,} 行，用时 {time.time() - t_all:,.0f}s")

    print("校验 ...")
    for t in ("wo_fact", "task_fact", "episode_fact", "audit_fact"):
        c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<14} {c:>12,}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()

    size = sum(Path(str(db) + s).stat().st_size
               for s in ("", "-wal") if Path(str(db) + s).exists())
    print(f"库文件: {size / 1024 ** 3:.2f} GiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
