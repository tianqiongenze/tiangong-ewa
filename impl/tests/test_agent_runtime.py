"""agent-runtime 测试：编排闭环 + REST API + MCP。

E2E 场景（合成数据，无真实产线）：
1. 下单搬运 → 分解 → 审批 → 执行 → 全部 DONE + 证据齐全
2. 禁入区目标 → 任务 ABORTED（红线 R1）
3. 超载无令牌 → 中止；有令牌 → 放行
4. 混沌注入失败 → 降级阶梯逐级下降
5. 停止区有人 → 停机（speed_limit=0）
6. REST 全链路 + MCP tools/call
"""

from __future__ import annotations

import pytest
from litestar.testing import TestClient

from tiangong_core import (
    Robot,
    RobotCapability,
    SafetyZone,
    Skill,
    SkillRegistry,
    TaskStatus,
    WorkOrder,
    WorkOrderKind,
    ZoneKind,
)

from app.mcp import mcp_endpoint
from app.orchestrator import Orchestrator
from app.robots import MockRobotDriver, SimRobot
from app.seed import DEMO_ROBOTS, DEMO_ZONES, seed_skills
from app.server import app


@pytest.fixture
def orch() -> Orchestrator:
    link = MockRobotDriver.demo_fleet()
    return Orchestrator(link=link, robots=DEMO_ROBOTS, zones=DEMO_ZONES,
                        skills=seed_skills())


def _transport(weight: float = 20.0) -> WorkOrder:
    return WorkOrder("WO-E2E", WorkOrderKind.TRANSPORT,
                     {"from": "原料库", "to": "装配车间", "what": "电池包",
                      "weight_kg": weight, "x": 25.0, "y": 14.0})


# ---------------------------------------------------------------- E2E
class TestOrchestratorE2E:
    def test_happy_path(self, orch: Orchestrator):
        g, rep = orch.submit(_transport())
        g = orch.approve(g, rep, "老周")
        rep = orch.run(g, rep)
        statuses = {t.status for t in rep.tasks.values()}
        assert statuses == {TaskStatus.DONE}, rep.to_dict()
        # 证据齐全
        for t in rep.tasks.values():
            kinds = set(t.evidence.kinds())
            assert {"trace", "telemetry", "skill_version"} <= kinds
        assert not rep.aborted and not rep.degraded
        assert len(orch.recorder) == 3

    def test_forbidden_zone_aborts(self, orch: Orchestrator):
        wo = WorkOrder("WO-F", WorkOrderKind.TRANSPORT,
                       {"from": "A", "to": "B", "weight_kg": 10,
                        "x": 55.0, "y": 55.0})   # 高压配电房
        g, rep = orch.submit(wo)
        g = orch.approve(g, rep)
        rep = orch.run(g, rep)
        assert rep.aborted
        assert any(t.status is TaskStatus.ABORTED for t in rep.tasks.values())
        assert "R1" in rep.abort_reason or any("R1" in t.note for t in rep.tasks.values())

    def test_overload_without_token_aborts(self, orch: Orchestrator):
        wo = WorkOrder("WO-H", WorkOrderKind.TRANSPORT,
                       {"from": "A", "to": "B", "weight_kg": 80, "x": 25.0, "y": 14.0})
        g, rep = orch.submit(wo)
        g = orch.approve(g, rep)
        rep = orch.run(g, rep)
        assert rep.aborted and "R2" in rep.abort_reason

    def test_overload_with_token_passes(self, orch: Orchestrator):
        orch.human_tokens["WO-H2"] = "HT-2026-0001"
        wo = WorkOrder("WO-H2", WorkOrderKind.TRANSPORT,
                       {"from": "A", "to": "B", "weight_kg": 80, "x": 25.0, "y": 14.0})
        g, rep = orch.submit(wo)
        g = orch.approve(g, rep)
        rep = orch.run(g, rep)
        assert not rep.aborted
        assert all(t.status is TaskStatus.DONE for t in rep.tasks.values())

    def test_degradation_ladder_on_chaos(self):
        link = MockRobotDriver([SimRobot("AMR-01", "AMR", x=0, y=0)])
        link.robots["AMR-01"].fail_next = 99      # 一直失败
        orch = Orchestrator(link=link, robots=DEMO_ROBOTS, zones=DEMO_ZONES)
        g, rep = orch.submit(_transport())
        g = orch.approve(g, rep)
        rep = orch.run(g, rep)
        assert rep.degraded
        assert rep.degrade_level in {"L3_TELEOP", "L4_HOLD"}

    def test_person_in_stop_zone_halts(self, orch: Orchestrator):
        g, rep = orch.submit(_transport())
        g = orch.approve(g, rep)
        rep = orch.run(g, rep, person_in_stop_zone=True)
        # 停机：任务失败（无人接管）→ 整单标记降级
        assert rep.degraded

    def test_inspect_order_with_quadruped(self, orch: Orchestrator):
        wo = WorkOrder("WO-I", WorkOrderKind.INSPECT, {
            "asset": "焊装线-01",
            "points": [{"name": "1号焊机", "x": 20.0, "y": 14.0,
                        "readings": ("温度", "异响")}],
        })
        g, rep = orch.submit(wo)
        g = orch.approve(g, rep)
        rep = orch.run(g, rep)
        assert all(t.status is TaskStatus.DONE for t in rep.tasks.values())
        snap = [t for t in rep.tasks.values() if t.kind == "PHOTO"]
        assert snap and "snapshot" in snap[0].evidence.kinds()


# ---------------------------------------------------------------- REST
class TestAPI:
    def test_healthz(self):
        with TestClient(app) as client:
            r = client.get("/healthz")
            assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_full_flow_over_http(self):
        with TestClient(app) as client:
            r = client.post("/tg-api/v1/work-orders", json={
                "kind": "TRANSPORT",
                "params": {"from": "原料库", "to": "装配车间",
                           "weight_kg": 15, "x": 25.0, "y": 14.0},
                "requested_by": "test"})
            assert r.status_code == 201
            oid = r.json()["order_id"]
            assert len(r.json()["tasks"]) == 3

            r = client.post(f"/tg-api/v1/work-orders/{oid}/approve",
                            json={"approved_by": "老周"})
            assert r.status_code == 200 and r.json()["status"] == "APPROVED"

            r = client.post(f"/tg-api/v1/work-orders/{oid}/run", json={})
            assert r.status_code == 200
            body = r.json()
            assert all(v["status"] == "DONE" for v in body["tasks"].values())

            r = client.get(f"/tg-api/v1/work-orders/{oid}")
            assert r.json()["status"] == "DONE"

    def test_run_without_approve_rejected(self):
        with TestClient(app) as client:
            r = client.post("/tg-api/v1/work-orders", json={
                "kind": "CHARGE", "params": {"dock": "充电桩"}})
            oid = r.json()["order_id"]
            r = client.post(f"/tg-api/v1/work-orders/{oid}/run", json={})
            assert r.status_code == 400

    def test_estop(self):
        with TestClient(app) as client:
            r = client.post("/tg-api/v1/robots/AMR-01/estop")
            assert r.status_code == 200 and r.json()["stopped"]

    def test_skill_approve_and_rollback(self):
        with TestClient(app) as client:
            r = client.post("/tg-api/v1/skills/transport/versions",
                            json={"version": "1.1.0", "entry": "vla://t/1.1.0"})
            assert r.status_code == 201 and r.json()["state"] == "PENDING"
            r = client.post("/tg-api/v1/skills/transport/versions/1.1.0/approve",
                            json={"approved_by": "李工"})
            assert r.json()["state"] == "APPROVED"
            r = client.post("/tg-api/v1/skills/transport/rollback?to=1.0.0")
            assert r.json()["active"] == "1.0.0"

    def test_episode_stats_after_run(self):
        with TestClient(app) as client:
            r = client.post("/tg-api/v1/work-orders", json={
                "kind": "TRANSPORT",
                "params": {"from": "A", "to": "B", "weight_kg": 10, "x": 25.0, "y": 14.0}})
            oid = r.json()["order_id"]
            client.post(f"/tg-api/v1/work-orders/{oid}/approve", json={})
            client.post(f"/tg-api/v1/work-orders/{oid}/run", json={})
            r = client.get("/tg-api/v1/episodes/stats")
            assert r.json()["total"] >= 3


# ---------------------------------------------------------------- MCP
class TestMCP:
    def test_mcp_flow(self):
        import json

        from litestar.testing import TestClient

        with TestClient(app) as client:
            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                          "method": "initialize"})
            assert r.json()["result"]["serverInfo"]["name"] == "tiangong-mcp"

            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                          "method": "tools/list"})
            names = {t["name"] for t in r.json()["result"]["tools"]}
            assert "tg_submit_order" in names and "tg_estop" in names

            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3,
                                          "method": "tools/call",
                                          "params": {"name": "tg_submit_order",
                                                     "arguments": {
                                                         "kind": "CHARGE",
                                                         "params": {"dock": "充电桩"},
                                                         "requested_by": "mcp-test"}}})
            oid = json.loads(r.json()["result"]["content"][0]["text"])["order_id"]

            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4,
                                          "method": "tools/call",
                                          "params": {"name": "tg_approve_order",
                                                     "arguments": {"order_id": oid}}})
            assert json.loads(r.json()["result"]["content"][0]["text"])["status"] == "APPROVED"

            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5,
                                          "method": "tools/call",
                                          "params": {"name": "tg_run_order",
                                                     "arguments": {"order_id": oid}}})
            body = json.loads(r.json()["result"]["content"][0]["text"])
            assert all(v["status"] == "DONE" for v in body["tasks"].values())

            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 6,
                                          "method": "tools/call",
                                          "params": {"name": "tg_estop",
                                                     "arguments": {"robot_id": "AMR-02"}}})
            assert json.loads(r.json()["result"]["content"][0]["text"])["stopped"]

            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7,
                                          "method": "tools/call",
                                          "params": {"name": "tg_episode_stats",
                                                     "arguments": {}}})
            assert json.loads(r.json()["result"]["content"][0]["text"])["total"] >= 2
