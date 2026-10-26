"""缓存预热：把分析库的高频聚合结果预写进 Redis（L2）。

为什么需要预热：
- 1 亿条事实库上的 7/30 天聚合是**秒级**查询，若缓存冷启动，
  首屏与首批请求会集体穿透到库（缓存雪崩的经典形态）。
- 预热把「首屏要用的 key」在服务启动前写好，配合错峰 TTL（抖动），
  冷启动首请求即可命中 L2。

key 约定（必须与 app 侧一致，RedisL2 前缀 `tg:`）：
    tg:an:scale             数据规模
    tg:an:summary:{days}    近 N 天概览
    tg:an:hourly:{days}     小时吞吐
    tg:an:audit:{days}      审计动作分布
    tg:an:timeline:{id}     单作业单时间线（热点）

用法：
    python tools/warmup_cache.py --redis-url redis://127.0.0.1:6379/0 \
        --bulk-db data/tiangong_bulk.db --days 1,7,30 --timelines 200
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "impl" / "services" / "agent-runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "impl" / "packages" / "tiangong_core"))

from app.analytics import AnalyticsStore  # noqa: E402

PREFIX = "tg:"


def _put(r, key: str, value, ttl: int) -> int:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    r.set(PREFIX + key, raw, ex=ttl)
    return len(raw.encode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    ap.add_argument("--bulk-db", default=str(Path(__file__).resolve().parents[1]
                                             / "data" / "tiangong_bulk.db"))
    ap.add_argument("--days", default="1,7,30")
    ap.add_argument("--timelines", type=int, default=200,
                    help="预热的单作业单时间线数量（最近热点）")
    ap.add_argument("--ttl", type=int, default=600, help="预热 key 的 TTL 秒")
    ap.add_argument("--jitter", type=int, default=60,
                    help="TTL 抖动上限（避免同时过期导致雪崩）")
    args = ap.parse_args()

    import redis

    r = redis.Redis.from_url(args.redis_url, decode_responses=True,
                             socket_timeout=2)
    try:
        r.ping()
    except Exception as exc:                       # noqa: BLE001
        print(f"无法连接 Redis: {exc}", file=sys.stderr)
        return 2

    store = AnalyticsStore(args.bulk_db)
    if not store.available:
        print(f"分析库不可用: {args.bulk_db}", file=sys.stderr)
        return 2

    days_list = [int(d) for d in args.days.split(",") if d.strip()]
    rnd = random.Random(20261026)
    total_bytes = 0
    total_keys = 0
    t_all = time.perf_counter()

    def ttl() -> int:
        return args.ttl + rnd.randint(0, max(args.jitter, 0))

    print("== 预热开始 ==")
    t0 = time.perf_counter()
    n = _put(r, "an:scale", store.scale(), ttl())
    total_bytes += n; total_keys += 1
    print(f"  an:scale                {n:>8} B  {time.perf_counter()-t0:6.3f}s")

    for d in days_list:
        t0 = time.perf_counter()
        n = _put(r, f"an:summary:{d}", store.summary(d), ttl())
        total_bytes += n; total_keys += 1
        print(f"  an:summary:{d:<3}         {n:>8} B  "
              f"{time.perf_counter()-t0:6.3f}s")

    for d in (1, 7):
        t0 = time.perf_counter()
        n = _put(r, f"an:hourly:{d}", store.hourly(d), ttl())
        total_bytes += n; total_keys += 1
        print(f"  an:hourly:{d:<3}          {n:>8} B  "
              f"{time.perf_counter()-t0:6.3f}s")

    for d in days_list:
        t0 = time.perf_counter()
        n = _put(r, f"an:audit:{d}", store.audit_stats(d), ttl())
        total_bytes += n; total_keys += 1
        print(f"  an:audit:{d:<3}           {n:>8} B  "
              f"{time.perf_counter()-t0:6.3f}s")

    # 热点时间线：取最近 N 个作业单（真实热点通常是「刚下的单」）
    if args.timelines > 0 and store._conn is not None:
        rows = store._conn.execute(
            "SELECT MAX(order_id) FROM wo_fact").fetchone()
        max_id = int(rows[0] or 0)
        ids = list(range(max(max_id - args.timelines + 1, 1), max_id + 1))
        t0 = time.perf_counter()
        hit = 0
        for oid in ids:
            tl = store.timeline(oid)
            if tl.get("found"):
                total_bytes += _put(r, f"an:timeline:{oid}", tl, ttl())
                total_keys += 1
                hit += 1
        print(f"  an:timeline:*           {hit:>8} keys "
              f"{time.perf_counter()-t0:6.3f}s")

    elapsed = time.perf_counter() - t_all
    print("== 预热完成 ==")
    print(f"  写入 key: {total_keys:,}  体积: {total_bytes/1024:.1f} KiB  "
          f"耗时: {elapsed:.2f}s  Redis dbsize: {r.dbsize():,}")

    # 抽样校验（GET + TTL）
    print("== 抽样校验 ==")
    for k in ("an:scale", "an:summary:7", f"an:timeline:{ids[-1] if args.timelines else 1}"):
        full = PREFIX + k
        raw = r.get(full)
        ttl_left = r.ttl(full)
        ok = raw is not None
        print(f"  {full:<28} hit={ok} ttl={ttl_left}s "
              f"bytes={len(raw.encode()) if raw else 0}")
        if not ok:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
