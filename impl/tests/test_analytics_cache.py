"""分析库（1 亿条事实库）与 Redis L2 缓存测试。

覆盖：
- AnalyticsStore：规模/概览/小时吞吐/时间线/审计分布/库不可用降级
- RedisL2：读写/TTL/前缀失效（fakeredis 注入，不依赖外部服务）
- MultiLevelCache：L1/L2 命中分别计数、**冷启动预热命中**（warm start）
- 分析端点：注入分析库后 HTTP 契约与缓存命中
"""

from __future__ import annotations

import sqlite3
import time

import fakeredis
import pytest
from litestar.testing import TestClient

from app import server as server_mod
from app.analytics import AnalyticsStore
from app.cache import MultiLevelCache, RedisL2

BULK_SCHEMA = """
CREATE TABLE wo_fact (order_id INTEGER PRIMARY KEY, site INTEGER, kind INTEGER,
    status INTEGER, created_ts INTEGER, duration_ms INTEGER, robot INTEGER,
    success INTEGER);
CREATE TABLE task_fact (task_id INTEGER PRIMARY KEY, order_id INTEGER, kind INTEGER,
    robot INTEGER, skill INTEGER, status INTEGER, start_ts INTEGER,
    duration_ms INTEGER, retries INTEGER);
CREATE TABLE episode_fact (episode_id INTEGER PRIMARY KEY, task_id INTEGER,
    robot INTEGER, skill INTEGER, success INTEGER, ts INTEGER, duration_ms INTEGER);
CREATE TABLE audit_fact (id INTEGER PRIMARY KEY, ts INTEGER, actor INTEGER,
    action INTEGER, target INTEGER);
"""


@pytest.fixture
def bulk_db(tmp_path):
    """小样本分析库（结构与 1 亿条库一致）。"""
    path = tmp_path / "bulk.db"
    conn = sqlite3.connect(path)
    conn.executescript(BULK_SCHEMA)
    now = int(time.time())
    day = 86400
    # 3 张作业单：DONE / DONE / FAILED；两单在 7 天内，一单在 20 天前
    conn.executemany("INSERT INTO wo_fact VALUES (?,?,?,?,?,?,?,?)", [
        (1, 0, 0, 5, now - 3600, 12000, 0, 1),
        (2, 0, 0, 5, now - 2 * day, 9000, 1, 1),
        (3, 1, 1, 6, now - 20 * day, 30000, 2, 0),
    ])
    conn.executemany("INSERT INTO task_fact VALUES (?,?,?,?,?,?,?,?,?)", [
        (10, 1, 0, 0, 0, 5, now - 3600, 4000, 0),
        (11, 1, 0, 0, 0, 5, now - 3500, 3000, 1),
        (12, 2, 0, 1, 1, 5, now - 2 * day, 5000, 0),
        (13, 3, 1, 2, 2, 6, now - 20 * day, 8000, 2),
    ])
    conn.executemany("INSERT INTO episode_fact VALUES (?,?,?,?,?,?,?)", [
        (100, 10, 0, 0, 1, now - 3600, 1200),
        (101, 11, 0, 0, 1, now - 3500, 1500),
        (102, 12, 1, 1, 0, now - 2 * day, 2200),
        (103, 13, 2, 2, 1, now - 20 * day, 5000),
    ])
    conn.executemany("INSERT INTO audit_fact VALUES (?,?,?,?,?)", [
        (1, now - 3600, 0, 0, 1),
        (2, now - 3500, 1, 1, 2),
        (3, now - 3000, 1, 6, 3),      # SAFETY_HALT
        (4, now - 2000, 2, 5, 4),      # RED_LINE_BLOCK
        (5, now - 20 * day, 0, 0, 5),
    ])
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def bulk_db_rollup(bulk_db):
    """同一份小样本，另建预聚合表（覆盖 rollup 读路径）。"""
    conn = sqlite3.connect(bulk_db)
    day = int(time.time()) // 86400
    conn.executescript("""
    CREATE TABLE agg_daily (day INTEGER, site INTEGER, kind INTEGER, orders INTEGER,
        done INTEGER, failed INTEGER, aborted INTEGER, orders_ms_sum INTEGER,
        tasks INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
        PRIMARY KEY (day, site, kind));
    CREATE TABLE agg_daily_task (day INTEGER PRIMARY KEY, tasks INTEGER, retries INTEGER);
    CREATE TABLE agg_daily_skill (day INTEGER, skill INTEGER, episodes INTEGER,
        success INTEGER, ms_sum INTEGER, b0 INTEGER, b1 INTEGER, b2 INTEGER,
        b3 INTEGER, b4 INTEGER, b5 INTEGER, PRIMARY KEY (day, skill));
    CREATE TABLE agg_daily_action (day INTEGER, action INTEGER, cnt INTEGER,
        PRIMARY KEY (day, action));
    CREATE TABLE agg_hourly (hour_ts INTEGER PRIMARY KEY, orders INTEGER);
    """)
    conn.executemany("INSERT INTO agg_daily VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (day, 0, 0, 2, 2, 0, 0, 21000, 0, 0),
        (day, 1, 1, 1, 0, 1, 0, 30000, 0, 0),
    ])
    conn.execute("INSERT INTO agg_daily_task VALUES (?,?,?)", (day, 4, 1))
    conn.execute("INSERT INTO agg_daily_skill VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (day, 0, 3, 2, 4800, 1, 2, 0, 0, 0, 0))
    conn.executemany("INSERT INTO agg_daily_action VALUES (?,?,?)",
                     [(day, 6, 1), (day, 5, 1)])
    conn.execute("INSERT INTO agg_hourly VALUES (?,?)", (day * 86400, 3))
    conn.commit()
    conn.close()
    return bulk_db


# ================================================================ 分析库
class TestAnalyticsStore:
    def test_scale(self, bulk_db):
        s = AnalyticsStore(bulk_db)
        d = s.scale()
        assert d["available"] and d["total_rows"] == 3 + 4 + 4 + 5
        assert d["tables"]["wo_fact"] == 3
        assert d["db_size_gib"] >= 0

    def test_summary_window(self, bulk_db):
        s = AnalyticsStore(bulk_db)
        d = s.summary(days=7)
        assert d["available"] and d["days"] == 7
        # 20 天前那单不在 7 天窗口内
        assert d["orders"]["total"] == 2
        assert d["orders"]["by_status"]["DONE"] == 2
        assert d["episodes"]["total"] == 3          # episode 三单都在 7 天内
        assert 0 < d["episodes"]["success_rate"] <= 1
        assert d["tasks"]["total"] == 3
        assert d["by_kind"][0]["kind"] == "TRANSPORT"
        assert d["safety"]["halt"] == 1
        assert d["safety"]["red_line_block"] == 1
        assert d["query_ms"] >= 0

    def test_summary_wider_window_includes_old(self, bulk_db):
        s = AnalyticsStore(bulk_db)
        assert s.summary(days=30)["orders"]["total"] == 3

    def test_hourly(self, bulk_db):
        s = AnalyticsStore(bulk_db)
        d = s.hourly(days=1)
        assert d["available"] and sum(b["orders"] for b in d["buckets"]) == 1

    def test_timeline_found(self, bulk_db):
        s = AnalyticsStore(bulk_db)
        d = s.timeline(1)
        assert d["found"] and d["order"]["status"] == "DONE"
        assert len(d["tasks"]) == 2 and d["tasks"][0]["skill"] == "transport"

    def test_timeline_not_found(self, bulk_db):
        s = AnalyticsStore(bulk_db)
        d = s.timeline(999)
        assert d["available"] and d["found"] is False

    def test_audit_stats(self, bulk_db):
        s = AnalyticsStore(bulk_db)
        d = s.audit_stats(days=7)
        actions = {x["action"]: x["count"] for x in d["by_action"]}
        assert actions["SAFETY_HALT"] == 1 and actions["RED_LINE_BLOCK"] == 1

    def test_missing_db_degrades(self, tmp_path):
        s = AnalyticsStore(tmp_path / "nope.db")
        assert not s.available
        assert s.scale() == {"available": False}
        assert s.summary(7)["available"] is False
        assert s.timeline(1)["available"] is False

    def test_no_arg_degrades(self):
        assert not AnalyticsStore("").available

    # -------------------------------------------------- 预聚合（rollup）路径
    def test_rollup_detected(self, bulk_db_rollup):
        s = AnalyticsStore(bulk_db_rollup)
        assert s.rollup and s.scale()["rollup"] is True

    def test_summary_from_rollup(self, bulk_db_rollup):
        s = AnalyticsStore(bulk_db_rollup)
        d = s.summary(7)
        assert d["source"] == "rollup"
        assert d["orders"]["total"] == 3
        assert d["orders"]["by_status"] == {"DONE": 2, "FAILED": 1, "ABORTED": 0}
        assert d["orders"]["avg_duration_ms"] == 17000.0
        assert d["tasks"] == {"total": 4, "avg_retries": 0.25}
        assert d["episodes"]["total"] == 3
        assert d["episodes"]["success_rate"] == 0.6667
        assert d["episodes"]["p95_duration_ms"] == 5000.0
        assert d["episodes"]["p95_basis"] == "bucket_estimate"
        assert d["safety"] == {"halt": 1, "red_line_block": 1}
        assert d["by_kind"][0]["kind"] == "TRANSPORT"

    def test_hourly_from_rollup(self, bulk_db_rollup):
        d = AnalyticsStore(bulk_db_rollup).hourly(7)
        assert d["source"] == "rollup"
        assert sum(b["orders"] for b in d["buckets"]) == 3

    def test_audit_from_rollup(self, bulk_db_rollup):
        d = AnalyticsStore(bulk_db_rollup).audit_stats(7)
        assert d["source"] == "rollup"
        acts = {x["action"]: x["count"] for x in d["by_action"]}
        assert acts["SAFETY_HALT"] == 1


# ================================================================ Redis L2
@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


class TestRedisL2:
    def test_set_get_with_prefix(self, fake_redis):
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        l2.set("an:summary:7", {"orders": 3}, ttl=30)
        assert l2.get("an:summary:7") == {"orders": 3}
        assert fake_redis.exists("tg:an:summary:7") == 1
        assert 0 < fake_redis.ttl("tg:an:summary:7") <= 30

    def test_missing_key_returns_none(self, fake_redis):
        assert RedisL2(client=fake_redis).get("nope") is None

    def test_delete_prefix_scans(self, fake_redis):
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        l2.set("an:timeline:1", {"a": 1})
        l2.set("an:timeline:2", {"a": 2})
        l2.set("an:scale", {"b": 1})
        assert l2.delete_prefix("an:timeline:") == 2
        assert l2.get("an:timeline:1") is None
        assert l2.get("an:scale") == {"b": 1}

    def test_ping_and_dbsize(self, fake_redis):
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        l2.set("k", 1)
        assert l2.ping() and l2.dbsize() == 1


# ================================================================ 多级缓存
class TestMultiLevelCacheWithRedis:
    def test_l1_then_l2_accounting(self, fake_redis):
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        c = MultiLevelCache(l2=l2, default_ttl=60)
        c.set("k", {"v": 1})
        ok, v = c.get("k")
        assert ok and v == {"v": 1}
        assert c.stats.l1_hits == 1 and c.stats.l2_hits == 0
        assert c.l2_available

    def test_warm_start_hits_l2(self, fake_redis):
        """预热场景：进程重启（新 cache 实例）后首个请求直接命中 L2。"""
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        l2.set("an:summary:7", {"orders": 42}, ttl=600)
        c = MultiLevelCache(l2=l2, default_ttl=60)
        ok, v = c.get("an:summary:7")
        assert ok and v["orders"] == 42
        assert c.stats.l2_hits == 1 and c.stats.l1_hits == 0
        # 回填 L1 后第二次走 L1
        ok, _ = c.get("an:summary:7")
        assert ok and c.stats.l1_hits == 1

    def test_invalidate_prefix_clears_l2(self, fake_redis):
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        c = MultiLevelCache(l2=l2, default_ttl=60)
        c.set("an:timeline:1", {"a": 1})
        c.set("an:timeline:2", {"a": 2})
        assert c.invalidate_prefix("an:timeline:") == 2
        assert l2.get("an:timeline:1") is None

    def test_l2_hit_does_not_shorten_l2_ttl(self, fake_redis):
        """回归：L2 命中回填 L1 时，不得把 L2 的长 TTL 改成 L1 的短 TTL。"""
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        l2.set("an:summary:7", {"orders": 1}, ttl=600)
        c = MultiLevelCache(l2=l2, default_ttl=5)
        ok, _ = c.get("an:summary:7")
        assert ok and c.stats.l2_hits == 1
        assert fake_redis.ttl("tg:an:summary:7") > 500

    def test_get_or_load_populates_both_levels(self, fake_redis):
        l2 = RedisL2(client=fake_redis, prefix="tg:")
        c = MultiLevelCache(l2=l2, default_ttl=60)
        calls = []

        def loader():
            calls.append(1)
            return {"x": 1}

        assert c.get_or_load("an:scale", loader, ttl=60) == {"x": 1}
        assert l2.get("an:scale") == {"x": 1}
        assert len(calls) == 1


# ================================================================ 端点
class TestAnalyticsEndpoints:
    def test_endpoints_with_store(self, bulk_db, monkeypatch):
        store = AnalyticsStore(bulk_db)
        monkeypatch.setattr(server_mod, "analytics", store)
        with TestClient(server_mod.app) as client:
            r = client.get("/tg-api/v1/analytics/scale")
            assert r.status_code == 200 and r.json()["total_rows"] == 16

            r = client.get("/tg-api/v1/analytics/summary?days=7")
            body = r.json()
            assert r.status_code == 200 and body["available"]
            assert body["orders"]["total"] == 2

            r = client.get("/tg-api/v1/analytics/orders/1/timeline")
            assert r.status_code == 200 and r.json()["found"]

            r = client.get("/tg-api/v1/analytics/hourly?days=1")
            assert r.status_code == 200

            r = client.get("/tg-api/v1/analytics/audit?days=7")
            assert r.status_code == 200

    def test_second_call_hits_cache(self, bulk_db, monkeypatch):
        store = AnalyticsStore(bulk_db)
        monkeypatch.setattr(server_mod, "analytics", store)
        with TestClient(server_mod.app) as client:
            client.get("/tg-api/v1/analytics/summary?days=30")
            before = server_mod.cache.stats.hits
            client.get("/tg-api/v1/analytics/summary?days=30")
            assert server_mod.cache.stats.hits == before + 1
