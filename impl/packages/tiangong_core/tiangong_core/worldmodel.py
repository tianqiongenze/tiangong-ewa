"""世界模型 L1：反事实可行性。

不预测未来曲线（那是 PdM 的世界模型），这里回答调度问题：
**"如果 X 失效/缺席，作业单还能完成吗？"**——
给计划做"压力测试"，把"赌单点"的计划在上线前拦下来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from . import capability as cap_mod
from .models import Robot, Task


@dataclass(frozen=True)
class Counterfactual:
    scenario: str                 # 场景描述
    feasible: bool
    reason: str
    affected: tuple[str, ...] = ()   # 受影响任务


def feasibility(tasks: Sequence[Task], robots: Sequence[Robot]) -> bool:
    """当前机群能否覆盖全部任务（每个任务至少一台合格机器人）。"""
    for t in tasks:
        if not any(not c.rejected for c in cap_mod.match_robots(t, robots)):
            return False
    return True


def counterfactual(tasks: Sequence[Task], robots: Sequence[Robot],
                   drop_robot_id: Optional[str] = None) -> Counterfactual:
    """反事实：某台机器人缺席时，计划是否仍可行。"""
    if drop_robot_id is None:
        ok = feasibility(tasks, robots)
        return Counterfactual("基线（无缺席）", ok,
                              "机群可覆盖全部任务" if ok else "存在无合格机器人的任务")

    remain = [r for r in robots if r.robot_id != drop_robot_id]
    affected: list[str] = []
    for t in tasks:
        with_r = [c for c in cap_mod.match_robots(t, robots) if not c.rejected]
        wo_r = [c for c in cap_mod.match_robots(t, remain) if not c.rejected]
        if with_r and not wo_r:
            affected.append(t.task_id)
    ok = not affected and feasibility(tasks, remain)
    reason = ("剩余机群仍可覆盖" if ok
              else f"机器人 {drop_robot_id} 缺席导致 {len(affected)} 个任务无合格执行者")
    return Counterfactual(f"假设 {drop_robot_id} 缺席", ok, reason, tuple(affected))
