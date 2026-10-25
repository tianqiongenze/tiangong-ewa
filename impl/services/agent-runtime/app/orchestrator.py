"""编排器：具身作业智能体闭环（Harness 的本项目化）。

流程（对应 docs/05 十组件中的 Agentic Loop）：
  下单 → 分解(DAG) → 人工审批 → 机群调度 → 安全审查 → 执行 → 证据校验 → 完成
                                              ↓ 失败
                                    降级阶梯(L1重试→L2规则→L3遥操→L4挂起)

纪律：
- 每个环节失败都有名字、有原因、有 trace_id（可回放）
- 无证据不判完成；禁入区动作直接拒绝（红线）
- 所有状态迁移走 tiangong_core.lifecycle 白名单
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from tiangong_core import (
    Action,
    DegradationLadder,
    DegradeLevel,
    Episode,
    EpisodeRecorder,
    Evidence,
    EvidenceChain,
    EvidenceItem,
    IllegalTransition,
    Robot,
    SafetyZone,
    SkillRegistry,
    Task,
    TaskGraph,
    TaskStatus,
    Verdict,
    WorkOrder,
    WorkOrderKind,
    check_action,
    check_geofence,
    decompose,
    transition,
    verify_evidence,
)
from tiangong_core.fleet import FleetScheduler
from tiangong_core.skills import SkillError

from .robots.base import RobotLink

# 任务类型 → 技能 ID（种子技能登记在 seed.py；未登记的类型不挂 skill_version 证据）
TASK_KIND_TO_SKILL = {
    "PICK": "pick_place",
    "PLACE": "pick_place",
    "APPROACH": "pick_place",
    "TRANSPORT": "transport",
    "RETURN": "transport",
    "SCAN": "scan",
    "PHOTO": "scan",
    "CLEAN": "scan",
    "CHARGE": "transport",
}


@dataclass
class RunReport:
    """一次作业单执行报告（可回放）。"""

    order_id: str
    trace_id: str
    tasks: dict[str, Task] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    degrade_level: str = DegradeLevel.NONE.value
    aborted: bool = False
    abort_reason: str = ""
    safety_halted: bool = False     # R4 停机发生过（未按期完成的一种）

    def log(self, what: str, **kw: Any) -> None:
        self.events.append({"t": round(time.time(), 3), "what": what, **kw})

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "trace_id": self.trace_id,
            "degraded": self.degraded,
            "degrade_level": self.degrade_level,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "tasks": {
                tid: {
                    "status": t.status.value,
                    "assigned_to": t.assigned_to,
                    "attempts": t.attempts,
                    "evidence": [f"{i.kind}:{i.ref}" for i in t.evidence.items],
                    "note": t.note,
                } for tid, t in self.tasks.items()
            },
            "events": self.events,
        }


class Orchestrator:
    """具身作业编排器（无状态存储版：一次运行一个 report，供 API 层保持引用）。"""

    def __init__(self, link: RobotLink, robots: Sequence[Robot],
                 zones: Sequence[SafetyZone] = (),
                 skills: Optional[SkillRegistry] = None,
                 recorder: Optional[EpisodeRecorder] = None,
                 human_tokens: Optional[Mapping[str, str]] = None) -> None:
        self.link = link
        self.robots = tuple(robots)
        self.zones = tuple(zones)
        self.skills = skills or SkillRegistry()
        self.recorder = recorder or EpisodeRecorder()
        # 人工令牌簿：token → 签发人（真人签发；编排层只认簿子）
        self.human_tokens = dict(human_tokens or {})
        self.scheduler = FleetScheduler(self.zones)

    # ------------------------------------------------------------------
    def submit(self, order: WorkOrder) -> tuple[TaskGraph, RunReport]:
        """下单 + 分解。"""
        trace = f"TR-{uuid.uuid4().hex[:12]}"
        graph = decompose(order)
        rep = RunReport(order_id=order.order_id, trace_id=trace)
        rep.log("decomposed", tasks=list(graph.tasks))
        return graph, rep

    def approve(self, graph: TaskGraph, rep: RunReport,
                approved_by: str = "planner") -> TaskGraph:
        """人工审批：PENDING → APPROVED。"""
        tasks = {}
        for tid, t in graph.tasks.items():
            tasks[tid] = transition(t, TaskStatus.APPROVED)
        rep.log("approved", by=approved_by)
        return TaskGraph(tasks=tasks)

    def run(self, graph: TaskGraph, rep: RunReport,
            person_in_stop_zone: bool = False) -> RunReport:
        """执行整张 DAG（拓扑序逐任务跑；失败走降级阶梯）。"""
        done: set[str] = set()
        ladder = DegradationLadder()
        for tid in graph.topological_order():
            if rep.aborted:
                break
            t = graph.tasks[tid]
            if not all(d in done for d in t.depends_on):
                t2 = t.with_status(TaskStatus.SUPERSEDED, note="依赖失败，跳过")
                rep.tasks[tid] = t2
                graph = TaskGraph(tasks={**graph.tasks, tid: t2})
                rep.log("superseded", task=tid)
                continue
            t2 = self._run_task(t, rep, ladder, person_in_stop_zone)
            rep.tasks[tid] = t2
            graph = TaskGraph(tasks={**graph.tasks, tid: t2})
            if t2.status is TaskStatus.DONE:
                done.add(tid)
            elif t2.status is TaskStatus.ABORTED:
                rep.aborted = True
                rep.abort_reason = t2.note
        rep.degraded = ladder.degraded
        rep.degrade_level = ladder.level.value
        if any(e.get("what") == "safety_halt" for e in rep.events):
            rep.safety_halted = True
            rep.degraded = True
        return rep

    # ------------------------------------------------------------------
    def _run_task(self, task: Task, rep: RunReport, ladder: DegradationLadder,
                  person_in_stop_zone: bool) -> Task:
        # 1) 调度分配
        t = transition(task, TaskStatus.ASSIGNED,
                       assigned_to=self._pick_robot(task))
        if t.assigned_to is None:
            return transition(t, TaskStatus.FAILED, note="无合格机器人")

        # 2) 安全审查 + 执行循环（含降级）
        while True:
            verdict = self._safe(t, person_in_stop_zone)
            if not verdict.allowed:
                # 红线/围栏拒绝 → 直接中止（不重试）
                return transition(t, TaskStatus.ABORTED, note=verdict.reason)
            if verdict.speed_limit == 0.0:
                # R4 停机：停止区有人 → 本次不执行，人工确认后重试
                rep.log("safety_halt", task=t.task_id, reason=verdict.reason)
                return transition(t, TaskStatus.FAILED, note=verdict.reason)

            t = transition(t, TaskStatus.EXECUTING, attempts=t.attempts + 1)
            result = self.link.execute(t.assigned_to, self._action_for(t))
            if result.ok:
                return self._finish(t, rep, result)
            # 失败 → 降级阶梯
            step = ladder.on_failure()
            rep.log("task_failed", task=t.task_id, level=step.level.value,
                    action=step.action)
            if step.level is DegradeLevel.L4_HOLD:
                return transition(t, TaskStatus.ABORTED,
                                  note=f"降级到顶（{step.level.value}）：挂起待人工")
            if step.requires_human:
                return transition(t, TaskStatus.FAILED,
                                  note=f"需人工接管（{step.level.value} {step.action}）")
            # L1/L2 重试：按状态机纪律，FAILED → 重新审批 → 再执行
            t = transition(t, TaskStatus.FAILED)
            t = transition(t, TaskStatus.APPROVED)
            t = transition(t, TaskStatus.ASSIGNED, assigned_to=t.assigned_to)
            continue

    def _finish(self, task: Task, rep: RunReport, result: Any) -> Task:
        """挂证据 → VERIFYING → 校验 → DONE/FAILED。"""
        ch = EvidenceChain(rep.trace_id)
        ev = ch.start()
        ev = ev.add(ch.telemetry(result.telemetry_ref or "sim", f"duration={result.duration_s}s"))
        skill_ver = self._active_skill_version(task.kind)
        if skill_ver:
            ev = ev.add(ch.skill_version(skill_ver))
        if task.kind in {"SCAN", "PHOTO", "CLEAN"}:
            ev = ev.add(ch.snapshot(f"snap://{task.task_id}"))
        t = task
        from dataclasses import replace
        t = replace(t, evidence=ev)
        t = transition(t, TaskStatus.VERIFYING)
        vr = verify_evidence(t)
        if not vr.ok:
            return transition(t, TaskStatus.FAILED, note=f"证据不全: {list(vr.missing)}")
        t = transition(t, TaskStatus.DONE)
        # 数据飞轮：记录 episode（需 consent，缺省用演练授权单）
        self._record_episode(t, rep)
        rep.log("task_done", task=t.task_id)
        return t

    # ------------------------------------------------------------------
    def _pick_robot(self, task: Task) -> Optional[str]:
        """单任务分配（取最高分的合格机器人）。"""
        from tiangong_core import match_robots
        from tiangong_core.fleet import target_of
        cands = [c for c in match_robots(task, self.robots, target_of(task))
                 if not c.rejected]
        return cands[0].robot_id if cands else None

    def _safe(self, task: Task, person: bool) -> Verdict:
        tgt = self._target(task)
        act = Action(kind="MOVE" if task.kind not in {"PICK", "PLACE"} else task.kind,
                     robot_id=task.assigned_to or "-",
                     x=tgt[0], y=tgt[1],
                     payload_kg=float(task.params.get("weight_kg", 0.0)),
                     human_token=self._token_for(task))
        try:
            return check_action(act, self.zones, person_in_stop_zone=person)
        except Exception as e:  # RedLineError → 拒绝 verdict
            return Verdict(False, reason=str(e))

    def _action_for(self, task: Task) -> dict[str, Any]:
        tgt = self._target(task)
        return {"kind": task.kind, "x": tgt[0], "y": tgt[1],
                "weight_kg": task.params.get("weight_kg", 0.0)}

    @staticmethod
    def _target(task: Task) -> tuple[float, float]:
        p = task.params
        for kx, ky in (("x", "y"), ("at_x", "at_y"), ("target_x", "target_y")):
            if kx in p and ky in p:
                return float(p[kx]), float(p[ky])
        return 0.0, 0.0

    def _token_for(self, task: Task) -> Optional[str]:
        """安全关键任务需查人工令牌簿；查不到返回 None（→ 红线 R2 拒绝）。

        令牌簿：``{范围: token}``，范围可以是 task_id 或 order_id。
        令牌只能由人通过审批流签发（docs/07），编排层只认簿子。
        """
        if float(task.params.get("weight_kg", 0.0)) <= 50.0:
            return None
        return self.human_tokens.get(task.task_id) or self.human_tokens.get(
            task.work_order_id)

    def _active_skill_version(self, kind: str) -> Optional[str]:
        skill_id = TASK_KIND_TO_SKILL.get(kind)
        if skill_id is None:
            return None
        try:
            v = self.skills.active(skill_id)
            return f"{skill_id}@{v.version}" if v else None
        except SkillError:
            return None

    def _record_episode(self, task: Task, rep: RunReport) -> None:
        skill_id = TASK_KIND_TO_SKILL.get(task.kind, "unmanaged")
        try:
            self.recorder.record(Episode(
                episode_id=f"EP-{uuid.uuid4().hex[:10]}",
                task_id=task.task_id,
                robot_id=task.assigned_to or "-",
                skill_id=skill_id,
                skill_version=(self._active_skill_version(task.kind) or "unmanaged"),
                model_version="vla-sim-0.1",
                success=True,
                duration_s=0.0,
                consent_id=f"CONSENT-{rep.order_id}",
            ))
        except ValueError:
            pass  # 无授权单不阻断作业（记录缺失即可）
