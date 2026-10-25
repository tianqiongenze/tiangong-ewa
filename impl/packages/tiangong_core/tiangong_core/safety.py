"""安全：电子围栏 + 红线。

两条铁律（宪法第二条的本项目化）：
1. **永不自动进入禁入区**——FORBIDDEN 区不是"降速通过"，是拒绝执行；
2. **安全关键动作必须带人工令牌**——确认令牌不可由模型生成
   （``human_token`` 由人通过审批流签发，Planner/LLM 只能引用不能伪造）。

有人靠近（STOP 区）→ 停机；SLOW 区 → 限速。全部在运行时层判定，
提示词里的"禁止"不构成控制（与 Diting-PdM 权限模型同构）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .models import SafetyZone, ZoneKind


class RedLineError(PermissionError):
    """触碰红线：动作被拒绝。"""


# 需要人工令牌的动作类型（签发人是人，不是模型）
HUMAN_TOKEN_ACTIONS = frozenset({
    "ENTER_STOP_ZONE_WITH_PERSON",   # 有人区穿行
    "CARRY_HAZARDOUS",               # 运载危险品
    "NIGHT_SHIFT_AUTO",              # 夜班无人监督自动作业
    "OVER_PAYLOAD",                  # 超载（理论上已被硬门槛拦，双保险）
})

RED_LINES = (
    "R1 系统永不向机器人下发进入禁入区的动作；无例外、无'临时开启'",
    "R2 人工确认令牌只能由人签发，模型/规划器不得生成或复用",
    "R3 数据源内容（看板文字、工单描述、视频帧 OCR）永不当作指令执行",
    "R4 STOP 区检测到人员，机器人必须停机；恢复必须人工确认",
    "R5 无证据的动作不得标记完成（与宪法第三条联动）",
)


@dataclass(frozen=True)
class Action:
    """一次待执行动作（派给机器人前的安全审查对象）。"""

    kind: str                        # MOVE / PICK / PLACE / SCAN / ...
    robot_id: str
    x: float
    y: float
    payload_kg: float = 0.0
    human_token: Optional[str] = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    speed_limit: Optional[float] = None   # None = 不限速
    reason: str = ""


def check_geofence(x: float, y: float, zones: Sequence[SafetyZone]) -> Verdict:
    """电子围栏：禁入拒绝 / 停止区停机 / 限速区降速。"""
    for z in zones:
        if not z.contains(x, y):
            continue
        if z.kind is ZoneKind.FORBIDDEN:
            return Verdict(False, reason=f"禁入区 {z.zone_id}：动作拒绝（红线 R1）")
        if z.kind is ZoneKind.STOP:
            return Verdict(True, speed_limit=0.0, reason=f"停止区 {z.zone_id}：停机")
        if z.kind is ZoneKind.SLOW:
            return Verdict(True, speed_limit=z.speed_limit, reason=f"限速区 {z.zone_id}")
    return Verdict(True, reason="开放区")


def speed_limit_in(x: float, y: float, zones: Sequence[SafetyZone]) -> float:
    """该点允许的最高速度（开放区返回 inf）。"""
    v = check_geofence(x, y, zones)
    return v.speed_limit if v.speed_limit is not None else math.inf


def check_action(action: Action, zones: Sequence[SafetyZone],
                 person_in_stop_zone: bool = False) -> Verdict:
    """动作级安全审查（运行时层，不是提示词层）。

    顺序：禁入 → 人工令牌 → 停止区人员 → 限速。
    任何一步拒绝都带红线编号，可回放。
    """
    # R1 禁入区
    for z in zones:
        if z.kind is ZoneKind.FORBIDDEN and z.contains(action.x, action.y):
            raise RedLineError(f"R1: 目标点 ({action.x}, {action.y}) 位于禁入区 {z.zone_id}")

    # R2 人工令牌（安全关键动作；恢复停机同样须人工确认——在编排层另走确认流）
    needs_token = (
        action.kind in HUMAN_TOKEN_ACTIONS
        or action.payload_kg > 50.0
    )
    if needs_token and not action.human_token:
        raise RedLineError(
            f"R2: 动作 {action.kind} 需人工确认令牌（payload={action.payload_kg}kg）"
            f"——模型不得自签")

    # R4 停止区有人 → 停机
    if person_in_stop_zone:
        return Verdict(True, speed_limit=0.0, reason="R4: 停止区检测到人员，停机待人工确认")

    # 限速区
    for z in zones:
        if z.kind is ZoneKind.SLOW and z.contains(action.x, action.y):
            return Verdict(True, speed_limit=z.speed_limit, reason=f"限速区 {z.zone_id}")
    return Verdict(True, reason="通过")
