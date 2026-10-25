"""机群调度：把就绪任务派给最合适的机器人。

约束（硬）：
- 能力匹配通过（capability.hard_check）
- 目标点不在禁入区（safety.check_geofence）
- 一台机器人同时只执行一个任务（首期，串行占用）

软目标：得分高者优先；同分按机器人 ID 稳定排序（可回放）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import capability as cap_mod
from . import safety
from .models import Robot, SafetyZone, Task, TaskStatus


@dataclass(frozen=True)
class Assignment:
    task_id: str
    robot_id: str
    score: float
    reasons: tuple[str, ...]
    speed_limit: float | None = None


@dataclass(frozen=True)
class ScheduleResult:
    assignments: tuple[Assignment, ...] = ()
    unassigned: tuple[str, ...] = ()          # 任务 ID + 原因
    rejections: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unassigned


def target_of(task: Task) -> tuple[float, float] | None:
    """从任务参数提取目标点（若有）。"""
    for key in ("x", "at_x", "target_x"):
        if key in task.params and "y" in task.params:
            try:
                return float(task.params[key]), float(task.params["y"])
            except (TypeError, ValueError):
                return None
    p = task.params
    if "x" in p and "y" in p:
        try:
            return float(p["x"]), float(p["y"])
        except (TypeError, ValueError):
            return None
    return None


class FleetScheduler:
    """调度器：无状态纯函数式（输入 tasks/robots/zones，输出 assignments）。"""

    def __init__(self, zones: Sequence[SafetyZone] = ()) -> None:
        self.zones = tuple(zones)

    def schedule(self, tasks: Sequence[Task], robots: Sequence[Robot]) -> ScheduleResult:
        busy: set[str] = set()
        out: list[Assignment] = []
        unassigned: list[str] = []
        rejections: dict[str, str] = {}

        for task in sorted(tasks, key=lambda t: t.task_id):
            if task.status is not TaskStatus.APPROVED:
                continue
            tgt = target_of(task)
            if tgt is not None:
                v = safety.check_geofence(tgt[0], tgt[1], self.zones)
                if not v.allowed:
                    unassigned.append(task.task_id)
                    rejections[task.task_id] = v.reason
                    continue
            cands = cap_mod.match_robots(task, robots, tgt)
            ok = [c for c in cands if not c.rejected and c.robot_id not in busy]
            if not ok:
                unassigned.append(task.task_id)
                reasons = "; ".join(f"{c.robot_id}:{c.reject_reason}"
                                    for c in cands if c.rejected) or "无可用机器人"
                rejections[task.task_id] = reasons
                continue
            best = ok[0]
            busy.add(best.robot_id)
            lim = None
            if tgt is not None:
                lim = safety.speed_limit_in(tgt[0], tgt[1], self.zones)
            out.append(Assignment(task_id=task.task_id, robot_id=best.robot_id,
                                  score=best.score, reasons=best.reasons,
                                  speed_limit=None if lim == float("inf") else lim))
        return ScheduleResult(assignments=tuple(out), unassigned=tuple(unassigned),
                              rejections=rejections)
