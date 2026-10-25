"""能力匹配：任务 → 机器人候选排序。

匹配不是"找个能用的"，而是"找个**敢用**的"：负载、展幅、传感器、
技能、续航、本质安全五项硬门槛 + 打分（近者优先、电多者优先）。
打分理由必须随结果返回——调度决策要可解释（宪法第三条）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import Robot, Task

# 任务类型 → 必需能力
REQUIRED: Mapping[str, dict[str, Any]] = {
    "PICK": {"payload": True},
    "TRANSPORT": {"payload": True},
    "PLACE": {"payload": True},
    "APPROACH": {},
    "SCAN": {"sensors": ("camera",)},
    "PHOTO": {"sensors": ("camera",)},
    "CLEAN": {"skills": ("cleaning",)},
    "RETURN": {},
    "CHARGE": {"skills": ("docking",)},
}


@dataclass(frozen=True)
class Candidate:
    robot_id: str
    score: float
    reasons: tuple[str, ...]
    rejected: bool = False
    reject_reason: str = ""


def _payload_of(task: Task) -> float:
    try:
        return float(task.params.get("weight_kg", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _needs_reach(task: Task) -> bool:
    return task.kind in {"PICK", "PLACE", "APPROACH"} and "station" in task.params


def hard_check(task: Task, robot: Robot) -> tuple[bool, str]:
    """硬门槛：任何一条不过即淘汰（返回 False + 原因）。"""
    req = REQUIRED.get(task.kind, {})
    cap = robot.capability
    if not robot.online:
        return False, "离线"
    if req.get("payload") and _payload_of(task) > cap.payload_kg:
        return False, f"超载（{_payload_of(task):.1f} > {cap.payload_kg:.1f} kg）"
    if req.get("sensors"):
        missing = [s for s in req["sensors"] if s not in cap.sensors]
        if missing:
            return False, f"缺传感器 {missing}"
    if req.get("skills"):
        missing = [s for s in req["skills"] if s not in cap.skills]
        if missing:
            return False, f"缺技能 {missing}"
    if _needs_reach(task) and cap.reach_m is None:
        return False, "无机械臂展幅参数（上下料必须）"
    if robot.battery_pct < 15.0:
        return False, f"电量不足（{robot.battery_pct:.0f}%）"
    return True, ""


def score_robot(task: Task, robot: Robot, target: tuple[float, float] | None = None) -> float:
    """软打分：距离近 + 电量足 + 速度够者优先（0–1）。"""
    cap = robot.capability
    s = 0.0
    if target is not None:
        import math
        d = math.hypot(robot.x - target[0], robot.y - target[1])
        s += 0.5 * max(0.0, 1.0 - d / 200.0)
    else:
        s += 0.25
    s += 0.3 * min(robot.battery_pct, 100.0) / 100.0
    s += 0.2 * min(cap.max_speed, 2.0) / 2.0
    return round(s, 4)


def match_robots(task: Task, robots: Sequence[Robot],
                 target: tuple[float, float] | None = None) -> list[Candidate]:
    """返回全部候选（含被淘汰者及原因）——拒绝也要留痕。"""
    out: list[Candidate] = []
    for r in robots:
        ok, why = hard_check(task, r)
        if ok:
            out.append(Candidate(
                robot_id=r.robot_id,
                score=score_robot(task, r, target),
                reasons=("硬门槛通过",
                         f"电量 {r.battery_pct:.0f}%",
                         f"负载余量 {r.capability.payload_kg - _payload_of(task):.1f} kg"),
            ))
        else:
            out.append(Candidate(robot_id=r.robot_id, score=0.0,
                                 reasons=(), rejected=True, reject_reason=why))
    out.sort(key=lambda c: c.score, reverse=True)
    return out
