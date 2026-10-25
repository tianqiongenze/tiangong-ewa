"""v1.1 重构模块测试：缓存 / 事件 / 可观测 / 仓储 / 适配器 / 记忆 / A2A。"""

from __future__ import annotations

import pytest
from litestar.testing import TestClient

from app.a2a import AGENT_CARD, delegate
from app.cache import MultiLevelCache
from app.events import EventBus, MemoryTransport
from app.memory import EpisodicMemory, HermesMemory, WorkingMemory
from app.otel import Tracer, gen_ai_attrs
from app.repository import Repository
from app.robots import AdapterRegistry, VDA5050Adapter
from app.seed import DEMO_ROBOTS, DEMO_ZONES, seed_skills
from app.server import app
from tiangong_core import (
    Episode,
    Robot,
    RobotCapability,
    SafetyZone,
    Task,
    TaskStatus,
    WorkOrder,
    WorkOrderKind,
    ZoneKind,
)


# ================================================================ 缓存
class TestCache:
    def test_set_get_and_ttl(self):
        c = MultiLevelCache(default_ttl=10)
        c.set("k", "v")
        ok, v = c.get("k")
        assert ok and v == "v"
        assert c.stats.hits == 1

    def test_ttl_expiry(self):
        c = MultiLevelCache(default_ttl=0.01)
        c.set("k", "v")
        import time; time.sleep(0.02)
        ok, _ = c.get("k")
        assert not ok and c.stats.misses == 1 and c.stats.evictions == 1

    def test_get_or_load(self):
        c = MultiLevelCache()
        calls = []
        def loader():
            calls.append(1)
            return 42
        assert c.get_or_load("k", loader) == 42
        assert c.get_or_load("k", loader) == 42
        assert len(calls) == 1          # 第二次命中缓存

    def test_invalidate_prefix(self):
        c = MultiLevelCache()
        c.set("robots:1", "a"); c.set("robots:2", "b"); c.set("zones", "c")
        n = c.invalidate_prefix("robots:")
        assert n == 2
        ok, _ = c.get("zones")
        assert ok

    def test_l2_failure_does_not_break(self):
        class BadL2:
            def get(self, k): raise RuntimeError("l2 down")
            def set(self, k, v, ttl): raise RuntimeError("l2 down")
        c = MultiLevelCache(l2=BadL2())
        c.set("k", "v")
        ok, v = c.get("k")
        assert ok and v == "v"          # L1 仍可用

    def test_hit_rate(self):
        c = MultiLevelCache()
        c.set("k", 1)
        c.get("k"); c.get("nope")
        assert c.stats.hit_rate == 0.5

    def test_max_entries_eviction(self):
        c = MultiLevelCache(default_ttl=100, max_entries=3)
        for i in range(5):
            c.set(f"k{i}", i)
        assert c.stats.evictions >= 1
        assert len(c._l1) <= 3


# ================================================================ 事件
class TestEvents:
    def test_publish_subscribe(self):
        bus = EventBus()
        got = []
        bus.subscribe("task", lambda e: got.append(e))
        ev = bus.publish("task", "done", {"task_id": "T1"}, trace_id="TR-1")
        assert ev.topic == "task" and len(got) == 1 and got[0].trace_id == "TR-1"
        assert bus.stats["published"] == 1

    def test_history_filter(self):
        bus = EventBus(MemoryTransport())
        bus.publish("a", "x", {})
        bus.publish("b", "y", {})
        assert len(bus.transport.history("a")) == 1

    def test_subscriber_failure_isolated(self):
        bus = EventBus()
        def bad(e): raise RuntimeError("boom")
        bus.subscribe("t", bad)
        ok = []
        bus.subscribe("t", lambda e: ok.append(e))
        assert bus.publish("t", "x", {})
        assert len(ok) == 1 and bus.stats["failed"] == 1

    def test_event_immutable(self):
        bus = EventBus()
        ev = bus.publish("t", "x", {"a": 1})
        with pytest.raises(Exception):
            ev.topic = "hacked"


# ================================================================ 可观测
class TestOtel:
    def test_session_trace_span(self):
        t = Tracer("test")
        with t.session("wo-run") as trace:
            assert trace.startswith("TR-")
            with t.span("safety_check", zone="Z1") as sp:
                sp.attributes["result"] = "allow"
        spans = t.spans(trace)
        assert len(spans) == 1 and spans[0].attributes["zone"] == "Z1"
        assert spans[0].duration_ms() >= 0

    def test_error_status_on_exception(self):
        t = Tracer("test")
        with pytest.raises(RuntimeError):
            with t.span("boom"):
                raise RuntimeError("x")
        assert t.spans()[0].status == "ERROR"

    def test_gen_ai_attrs(self):
        attrs = gen_ai_attrs("vla-0.4", input_tokens=10, output_tokens=5)
        assert attrs["gen_ai.system"] == "vla"
        assert attrs["gen_ai.request.model"] == "vla-0.4"
        assert attrs["gen_ai.usage.input_tokens"] == 10

    def test_metrics(self):
        t = Tracer("test")
        with t.span("a"):
            pass
        m = t.metrics()
        assert m["spans_total"] == 1 and m["service"] == "test"

    def test_export_shape(self):
        t = Tracer("test")
        with t.session("s") as tr:
            with t.span("x"):
                pass
        out = t.export(tr)
        assert set(out[0]) >= {"name", "trace_id", "span_id", "duration_ms", "attributes"}


# ================================================================ 仓储
class TestRepository:
    def test_roundtrip_order(self):
        repo = Repository(":memory:")
        repo.save_order("WO-1", "TRANSPORT", {"from": "A", "to": "B"}, 3, "laozhou", "TR-1")
        d = repo.get_order("WO-1")
        assert d["kind"] == "TRANSPORT" and d["params"]["to"] == "B"
        repo.set_order_status("WO-1", "DONE")
        assert repo.get_order("WO-1")["status"] == "DONE"

    def test_tasks_and_query(self):
        repo = Repository(":memory:")
        repo.save_order("WO-2", "TRANSPORT", {}, 5, "x", "TR-2")
        t = Task("T1", "WO-2", "PICK", {"a": 1}, status=TaskStatus.DONE)
        repo.save_task(t)
        tasks = repo.get_tasks("WO-2")
        assert tasks[0]["task_id"] == "T1" and tasks[0]["params"]["a"] == 1

    def test_episode_success_rate(self):
        repo = Repository(":memory:")
        for i, ok in enumerate([True, True, False]):
            ep = Episode(f"E{i}", "T", "R1", "transport", "1.0", "vla",
                         ok, 1.0, consent_id="C1")
            repo.save_episode(ep)
        assert repo.episode_success_rate("transport") == 0.6667
        assert repo.count_episodes() == 3

    def test_audit_trail(self):
        repo = Repository(":memory:")
        repo.audit("laozhou", "approve", "WO-1", "ok", "TR-1")
        trail = repo.audit_trail()
        assert trail[0]["actor"] == "laozhou" and trail[0]["action"] == "approve"

    def test_skills_and_zones(self):
        repo = Repository(":memory:")
        repo.save_skill("transport", "1.0.0", "APPROVED", "e1", 10, "z")
        assert repo.active_skills()[0]["skill_id"] == "transport"
        repo.save_zone(SafetyZone("Z1", ZoneKind.STOP, 0, 0, 1, 1))
        assert repo.get_order("none") is None

    def test_rw_separation_connections(self):
        repo = Repository(":memory:")
        assert repo._w is not repo._r      # 写主读从：两个连接


# ================================================================ 适配器
class TestAdapters:
    def test_vda5050_topics(self):
        a = VDA5050Adapter("seer", serials=["SN-1"])
        assert a.topic("SN-1", "order") == "uagv/v2/seer/SN-1/order"
        assert a.protocol == "vda5050"

    def test_dry_run_publish(self):
        a = VDA5050Adapter("seer", serials=["SN-1"])
        r = a.execute("SN-1", {"kind": "MOVE", "x": 1, "y": 2})
        assert r["ok"] and r["dry_run"]
        topic, payload = a.drain_outbox()[0]
        assert topic.endswith("/order") and "orderId" in payload

    def test_estop_cancels(self):
        a = VDA5050Adapter("seer", serials=["SN-1"])
        assert a.estop("SN-1")
        topic, payload = a.drain_outbox()[0]
        assert topic.endswith("/instantActions") and "cancelOrder" in payload

    def test_parse_state(self):
        a = VDA5050Adapter("seer")
        st = a.parse_state('{"serialNumber":"SN-1","x":3,"y":4,'
                           '"batteryState":{"batteryCharge":78},"driving":true}')
        assert st["robot_id"] == "SN-1" and st["battery_pct"] == 78.0

    def test_registry_multi_brand(self):
        reg = AdapterRegistry()
        reg.register(VDA5050Adapter("seer", serials=["SN-1"]))
        reg.register(VDA5050Adapter("kuka", serials=["SN-2"]))
        assert len(reg.all()) == 2
        robots = reg.discover_all()
        assert {r.robot_id for r in robots} == {"SN-1", "SN-2"}
        h = reg.health_all()
        assert not h["seer"].ok          # dry-run 未注入 MQTT

    def test_injected_mqtt_publishes(self):
        class FakeMqtt:
            def __init__(self): self.sent = []
            def publish(self, topic, payload): self.sent.append((topic, payload))
            def ping(self): return True
        mq = FakeMqtt()
        a = VDA5050Adapter("seer", serials=["SN-1"], mqtt_client=mq)
        r = a.execute("SN-1", {"kind": "MOVE", "x": 1, "y": 2})
        assert not r["dry_run"] and len(mq.sent) == 1
        assert a.health().ok


# ================================================================ 记忆
class TestMemory:
    def test_working_overflow_freeze(self):
        wm = WorkingMemory(max_items=2)
        wm.put("A", {}); wm.put("B", {}); wm.put("C", {})
        assert wm.active() == ["B", "C"]   # A 被下沉

    def test_episodic_recall_and_rate(self):
        em = EpisodicMemory()
        for i, ok in enumerate([True, True, False]):
            em.remember(Episode(f"E{i}", "T", "R", "scan", "1.0", "vla",
                                ok, 1.0, consent_id="C"))
        assert em.success_rate("scan") == 0.6667
        assert len(em.recall("scan", success=False)) == 1

    def test_hermes_stats(self):
        reg = seed_skills()
        m = HermesMemory(reg)
        m.working.put("WO-1", {})
        s = m.stats()
        assert s["working"] == 1 and "transport" in s["skill_states"]
        assert m.skill.active("transport") == "transport@1.0.0"

    def test_consolidate(self):
        m = HermesMemory()
        m.working.put("WO-1", {"x": 1})
        snap = m.consolidate("WO-1")
        assert snap["snapshot"]["x"] == 1 and len(m.working) == 0


# ================================================================ A2A
class TestA2A:
    def test_card(self):
        assert AGENT_CARD["capabilities"]["work_order_kinds"]

    def test_delegate_creates_pending_order(self):
        r = delegate("INSPECT", {"asset": "M-1", "points": []},
                     source_agent="diting-pdm")
        assert r["ok"] and r["order_id"].startswith("WO-A2A-")
        assert r["source_agent"] == "diting-pdm"

    def test_delegate_rejects_unknown(self):
        r = delegate("WORLD_DOMINATION", {})
        assert not r["ok"] and "supported" in r


# ================================================================ API 新端点
class TestNewEndpoints:
    def test_healthz_and_new_observability(self):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            r = client.get("/tg-api/v1/observability/spans")
            assert r.status_code == 200 and "spans" in r.json()
            r = client.get("/tg-api/v1/observability/metrics")
            assert "p95_ms" in r.json()
            r = client.get("/tg-api/v1/adapters/health")
            assert r.status_code == 200
            r = client.get("/a2a/v1/card")
            assert r.json()["name"] == "tiangong-ewa"

    def test_a2a_delegate_over_http(self):
        with TestClient(app) as client:
            r = client.post("/a2a/v1/tasks", json={
                "intent": "TRANSPORT",
                "params": {"from": "A", "to": "B", "weight_kg": 5, "x": 25, "y": 14},
                "source_agent": "diting-pdm"})
            assert r.status_code == 200 and r.json()["ok"]

    def test_events_stream(self):
        with TestClient(app) as client:
            r = client.get("/tg-api/v1/events")
            assert r.status_code == 200 and "published" in r.json()["stats"]
