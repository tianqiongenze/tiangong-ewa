"""tiangong_core 包级测试（pytest）。"""

import math

import pytest

from tiangong_core import (
    Assignment,
    Counterfactual,
    DegradationLadder,
    DegradeLevel,
    Episode,
    EpisodeRecorder,
    EvidenceChain,
    IllegalTransition,
    PlanError,
    RedLineError,
    Robot,
    RobotCapability,
    SafetyZone,
    Skill,
    SkillRegistry,
    Task,
    TaskGraph,
    TaskStatus,
    WorkOrder,
    WorkOrderKind,
    ZoneKind,
    check_action,
    check_geofence,
    decompose,
    match_robots,
    topological_order,
    transition,
    verify_evidence,
)

# ---------- fixtures ----------

@pytest.fixture
def amr() -> Robot:
    return Robot(robot_id="AMR-01", kind="AMR", model="P200",
                 capability=RobotCapability(
                     payload_kg=200, max_speed=1.5,
                     sensors=("camera", "lidar"), skills=("transport", "docking")))


@pytest.fixture
def arm() -> Robot:
    return Robot(robot_id="ARM-01", kind="ARM", model="S6",
                 capability=RobotCapability(
                     payload_kg=6, max_speed=2.0, reach_m=1.2,
                     sensors=("camera", "force"), skills=("pick_place",)),
                 x=5.0, y=5.0)


@pytest.fixture
def zones() -> list[SafetyZone]:
    return [
        SafetyZone("Z-FORBID", ZoneKind.FORBIDDEN, 50, 50, 60, 60),
        SafetyZone("Z-STOP", ZoneKind.STOP, 0, 0, 10, 10),
        SafetyZone("Z-SLOW", ZoneKind.SLOW, 20, 0, 30, 10, speed_limit=0.3),
    ]


# ---------- 任务分解 ----------

class TestTaskGraph:
    def test_transport_decomposes_to_three_steps(self):
        wo = WorkOrder("WO-1", WorkOrderKind.TRANSPORT,
                       {"from": "A区", "to": "B区", "what": "料箱"})
        g = decompose(wo)
        assert set(g.tasks) == {"WO-1-T1", "WO-1-T2", "WO-1-T3"}
        assert g.tasks["WO-1-T2"].depends_on == ("WO-1-T1",)
        assert topological_order(g)[0] == "WO-1-T1"
        assert topological_order(g)[-1] == "WO-1-T3"

    def test_inspect_chain_and_photo(self):
        wo = WorkOrder("WO-2", WorkOrderKind.INSPECT,
                       {"points": [{"name": "1号表"}, {"name": "2号表"}], "asset": "M-1"})
        g = decompose(wo)
        assert len(g.tasks) == 3
        assert g.tasks["WO-2-T3"].kind == "PHOTO"
        assert g.tasks["WO-2-T2"].depends_on == ("WO-2-T1",)

    def test_ready_set(self):
        wo = WorkOrder("WO-3", WorkOrderKind.TRANSPORT, {"from": "A", "to": "B"})
        g = decompose(wo)
        assert g.ready(set()) == ["WO-3-T1"]
        assert g.ready({"WO-3-T1"}) == ["WO-3-T2"]
        assert g.ready({"WO-3-T1", "WO-3-T2"}) == ["WO-3-T3"]

    def test_cycle_rejected(self):
        t1 = Task("T1", "WO", "PICK", {}, depends_on=("T2",))
        t2 = Task("T2", "WO", "PLACE", {}, depends_on=("T1",))
        with pytest.raises(PlanError):
            TaskGraph({"T1": t1, "T2": t2})

    def test_missing_dependency_rejected(self):
        t1 = Task("T1", "WO", "PICK", {}, depends_on=("NOPE",))
        with pytest.raises(PlanError):
            TaskGraph({"T1": t1})

    def test_clean_and_charge_and_load_unload(self):
        g = decompose(WorkOrder("W4", WorkOrderKind.CLEAN, {"area": "车间A"}))
        assert g.tasks["W4-T2"].kind == "CLEAN"
        g = decompose(WorkOrder("W5", WorkOrderKind.CHARGE, {}))
        assert g.tasks["W5-T2"].kind == "CHARGE"
        g = decompose(WorkOrder("W6", WorkOrderKind.LOAD_UNLOAD,
                                {"station": "S1", "machine": "CNC-1"}))
        assert g.tasks["W6-T1"].kind == "APPROACH"
        assert g.tasks["W6-T3"].depends_on == ("W6-T2",)

    def test_duplicate_ids_rejected(self):
        class DupPlanner:
            def plan(self, order):
                return [Task("X", "WO", "PICK", {}), Task("X", "WO", "PLACE", {})]
        with pytest.raises(PlanError):
            decompose(WorkOrder("W7", WorkOrderKind.TRANSPORT, {}), planner=DupPlanner())


# ---------- 能力匹配 ----------

class TestCapability:
    def test_overload_rejected(self, amr):
        t = Task("T", "WO", "TRANSPORT", {"weight_kg": 300})
        cands = match_robots(t, [amr])
        assert cands[0].rejected and "超载" in cands[0].reject_reason

    def test_pick_requires_reach(self, amr, arm):
        t = Task("T", "WO", "PICK", {"station": "CNC-1", "weight_kg": 2})
        cands = {c.robot_id: c for c in match_robots(t, [amr, arm])}
        assert cands["AMR-01"].rejected          # 无臂展
        assert not cands["ARM-01"].rejected

    def test_scan_requires_camera(self, amr):
        t = Task("T", "WO", "SCAN", {"point": "p1"})
        assert not match_robots(t, [amr])[0].rejected
        blind = Robot("B", "AMR", "x", RobotCapability(100, 1.0, sensors=("lidar",)))
        assert match_robots(t, [blind])[0].rejected

    def test_low_battery_rejected(self, amr):
        low = Robot(robot_id=amr.robot_id, kind=amr.kind, model=amr.model,
                    capability=amr.capability, battery_pct=8.0)
        t = Task("T", "WO", "TRANSPORT", {"weight_kg": 10})
        assert match_robots(t, [low])[0].rejected

    def test_nearer_robot_scores_higher(self, amr):
        far = Robot("AMR-99", "AMR", "P200", amr.capability, x=190.0)
        near = Robot("AMR-01", "AMR", "P200", amr.capability, x=2.0)
        t = Task("T", "WO", "TRANSPORT", {"weight_kg": 10})
        cands = match_robots(t, [far, near], target=(0.0, 0.0))
        assert cands[0].robot_id == "AMR-01"


# ---------- 安全 ----------

class TestSafety:
    def test_forbidden_zone_blocks_geofence(self, zones):
        v = check_geofence(55, 55, zones)
        assert not v.allowed and "R1" in v.reason

    def test_stop_zone_halts(self, zones):
        v = check_geofence(5, 5, zones)
        assert v.allowed and v.speed_limit == 0.0

    def test_slow_zone_limits(self, zones):
        v = check_geofence(25, 5, zones)
        assert v.allowed and v.speed_limit == 0.3

    def test_action_in_forbidden_raises(self, zones):
        from tiangong_core import Action
        a = Action(kind="MOVE", robot_id="R1", x=55, y=55)
        with pytest.raises(RedLineError):
            check_action(a, zones)

    def test_heavy_payload_needs_human_token(self, zones):
        from tiangong_core import Action
        a = Action(kind="TRANSPORT", robot_id="R1", x=100, y=100, payload_kg=80)
        with pytest.raises(RedLineError):
            check_action(a, zones)

    def test_heavy_payload_with_token_passes(self, zones):
        from tiangong_core import Action
        a = Action(kind="TRANSPORT", robot_id="R1", x=100, y=100,
                   payload_kg=80, human_token="HT-2026-0001")
        v = check_action(a, zones)
        assert v.allowed

    def test_person_in_stop_zone_halts(self, zones):
        from tiangong_core import Action
        a = Action(kind="MOVE", robot_id="R1", x=100, y=100)
        v = check_action(a, zones, person_in_stop_zone=True)
        assert v.speed_limit == 0.0 and "R4" in v.reason


# ---------- 证据链 ----------

class TestEvidence:
    def _done_transport(self) -> Task:
        ch = EvidenceChain("TR-1")
        ev = ch.start()
        ev = ev.add(ch.telemetry("tel-001", "P95=0.8s"))
        ev = ev.add(ch.skill_version("transport@1.2.0"))
        return Task("T", "WO", "TRANSPORT", {}, status=TaskStatus.VERIFYING, evidence=ev)

    def test_complete_evidence_passes(self):
        r = verify_evidence(self._done_transport())
        assert r.ok and r.trace_id == "TR-1"

    def test_missing_evidence_fails(self):
        t = Task("T", "WO", "TRANSPORT", {}, evidence=EvidenceChain("TR-2").start())
        r = verify_evidence(t)
        assert not r.ok and set(r.missing) == {"telemetry", "skill_version"}

    def test_photo_needs_snapshot(self):
        ch = EvidenceChain("TR-3")
        t = Task("T", "WO", "PHOTO", {}, evidence=ch.start())
        assert not verify_evidence(t).ok


# ---------- 状态机 ----------

class TestLifecycle:
    def test_happy_path(self):
        t = Task("T", "WO", "PICK", {})
        for s in (TaskStatus.APPROVED, TaskStatus.ASSIGNED, TaskStatus.EXECUTING,
                  TaskStatus.VERIFYING, TaskStatus.DONE):
            t = transition(t, s)
        assert t.status is TaskStatus.DONE

    def test_illegal_jump(self):
        t = Task("T", "WO", "PICK", {})
        with pytest.raises(IllegalTransition):
            transition(t, TaskStatus.DONE)

    def test_failed_returns_to_approved(self):
        t = Task("T", "WO", "PICK", {}, status=TaskStatus.FAILED)
        assert transition(t, TaskStatus.APPROVED).status is TaskStatus.APPROVED

    def test_verifying_can_bounce_back(self):
        t = Task("T", "WO", "PICK", {}, status=TaskStatus.VERIFYING)
        assert transition(t, TaskStatus.EXECUTING).status is TaskStatus.EXECUTING

    def test_terminal_no_exit(self):
        t = Task("T", "WO", "PICK", {}, status=TaskStatus.DONE)
        assert not tiangong_allowed(t)


def tiangong_allowed(t: Task) -> bool:
    from tiangong_core.lifecycle import allowed_next
    return len(allowed_next(t.status)) > 0


# ---------- 降级 ----------

class TestDegrade:
    def test_ladder_progresses(self):
        d = DegradationLadder()
        assert d.level is DegradeLevel.NONE
        d.on_failure()                      # L1 第 1 次
        assert d.level is DegradeLevel.L1_RETRY and d.degraded
        d.on_failure()                      # L1 第 2 次 → 升 L2
        assert d.level is DegradeLevel.L2_RULE_FALLBACK
        d.on_failure()                      # L2 → L3
        assert d.level is DegradeLevel.L3_TELEOP
        d.on_failure()                      # L3 → L4
        assert d.level is DegradeLevel.L4_HOLD
        d.on_failure()                      # 到顶保持
        assert d.level is DegradeLevel.L4_HOLD

    def test_l3_requires_human(self):
        d = DegradationLadder()
        d.on_failure(); d.on_failure()
        assert d.step().requires_human or d.level is DegradeLevel.L2_RULE_FALLBACK
        d.on_failure()
        assert d.step().requires_human


# ---------- 技能治理 ----------

class TestSkills:
    def _reg(self) -> SkillRegistry:
        reg = SkillRegistry()
        reg.register(Skill("pick_place", "取放"))
        return reg

    def test_publish_starts_pending(self):
        reg = self._reg()
        v = reg.publish("pick_place", "1.0.0", "vla://pp/1.0.0")
        assert v.state == "PENDING" and reg.active("pick_place") is None

    def test_approve_supersedes_old(self):
        reg = self._reg()
        reg.publish("pick_place", "1.0.0", "e1")
        reg.publish("pick_place", "1.1.0", "e2")
        reg.approve("pick_place", "1.0.0", "zhang")
        assert reg.active("pick_place").version == "1.0.0"
        reg.approve("pick_place", "1.1.0", "li")
        assert reg.active("pick_place").version == "1.1.0"
        old = next(v for v in reg.get("pick_place").versions if v.version == "1.0.0")
        assert old.state == "SUPERSEDED"

    def test_rollback(self):
        reg = self._reg()
        reg.publish("pick_place", "1.0.0", "e1")
        reg.publish("pick_place", "1.1.0", "e2")
        reg.approve("pick_place", "1.0.0", "z")
        reg.approve("pick_place", "1.1.0", "l")
        v = reg.rollback("pick_place", "1.0.0")
        assert v.state == "APPROVED" and reg.active("pick_place").version == "1.0.0"

    def test_duplicate_version_rejected(self):
        reg = self._reg()
        reg.publish("pick_place", "1.0.0", "e1")
        with pytest.raises(Exception):
            reg.publish("pick_place", "1.0.0", "e2")

    def test_cannot_rollback_to_rejected(self):
        reg = self._reg()
        reg.publish("pick_place", "1.0.0", "e1")
        reg.reject("pick_place", "1.0.0")
        with pytest.raises(Exception):
            reg.rollback("pick_place", "1.0.0")


# ---------- 调度 ----------

class TestFleet:
    def test_assigns_best(self, amr, arm, zones):
        from tiangong_core import FleetScheduler
        s = FleetScheduler(zones)
        t1 = Task("T1", "WO", "TRANSPORT", {"weight_kg": 100, "x": 0.0, "y": 0.0},
                  status=TaskStatus.APPROVED)
        t2 = Task("T2", "WO", "PICK", {"station": "CNC-1", "weight_kg": 2},
                  status=TaskStatus.APPROVED)
        r = s.schedule([t1, t2], [amr, arm])
        assert r.ok
        by_task = {a.task_id: a for a in r.assignments}
        assert by_task["T1"].robot_id == "AMR-01"
        assert by_task["T2"].robot_id == "ARM-01"

    def test_one_robot_one_task(self, amr, zones):
        from tiangong_core import FleetScheduler
        s = FleetScheduler(zones)
        tasks = [Task(f"T{i}", "WO", "TRANSPORT", {"weight_kg": 10},
                      status=TaskStatus.APPROVED) for i in range(3)]
        r = s.schedule(tasks, [amr])
        assert len(r.assignments) == 1 and len(r.unassigned) == 2

    def test_forbidden_target_unassigned(self, amr, zones):
        from tiangong_core import FleetScheduler
        s = FleetScheduler(zones)
        t = Task("T", "WO", "TRANSPORT", {"weight_kg": 10, "x": 55.0, "y": 55.0},
                 status=TaskStatus.APPROVED)
        r = s.schedule([t], [amr])
        assert not r.ok and "禁入区" in r.rejections["T"]

    def test_slow_zone_speed_limit(self, amr, zones):
        from tiangong_core import FleetScheduler
        s = FleetScheduler(zones)
        t = Task("T", "WO", "TRANSPORT", {"weight_kg": 10, "x": 25.0, "y": 5.0},
                 status=TaskStatus.APPROVED)
        r = s.schedule([t], [amr])
        assert r.assignments[0].speed_limit == 0.3


# ---------- 世界模型 ----------

class TestWorldModel:
    def test_baseline_feasible(self, amr, arm):
        from tiangong_core import counterfactual
        tasks = [Task("T1", "WO", "TRANSPORT", {"weight_kg": 10}),
                 Task("T2", "WO", "PICK", {"station": "C", "weight_kg": 1})]
        cf = counterfactual(tasks, [amr, arm])
        assert cf.feasible

    def test_drop_arm_breaks_pick(self, amr, arm):
        from tiangong_core import counterfactual
        tasks = [Task("T2", "WO", "PICK", {"station": "C", "weight_kg": 1})]
        cf = counterfactual(tasks, [amr, arm], drop_robot_id="ARM-01")
        assert not cf.feasible and "ARM-01" in cf.reason


# ---------- 数据飞轮 ----------

class TestEpisode:
    def _ep(self, ok=True) -> Episode:
        return Episode("E-1", "T-1", "AMR-01", "pick_place", "1.0.0", "vla-0.4",
                       ok, 12.5, consent_id="CONSENT-2026-001")

    def test_requires_consent(self):
        rec = EpisodeRecorder()
        with pytest.raises(ValueError):
            rec.record(self._ep().__class__(**{**self._ep().__dict__, "consent_id": ""}))

    def test_success_rate(self):
        rec = EpisodeRecorder()
        rec.record(self._ep(True))
        rec.record(self._ep(False))
        assert rec.success_rate("pick_place") == 0.5

    def test_shard_export(self):
        rec = EpisodeRecorder()
        rec.record(self._ep(True))
        out = rec.export_shard("pick_place/ok")
        assert "E-1" in out and "CONSENT-2026-001" in out

    def test_checksum_stable(self):
        a, b = self._ep(), self._ep()
        assert a.checksum() == b.checksum()
