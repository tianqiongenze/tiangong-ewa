"""降级阶梯：VLA 失败不是"重试到天荒地老"，而是逐级降级。

阶梯（从上到下，每一级都要显式标记 degraded，宪法第四条）：
  L1 VLA 技能重试（同版本，最多 2 次）
  L2 规则技能兜底（确定性、可解释）
  L3 人工遥操作（人接管）
  L4 挂起等待（保持现场，交班处理）

纪律：任何一级触发都必须记录 degraded 事实与当前级数；
"静默降级"视为违规。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DegradeLevel(StrEnum):
    NONE = "NONE"
    L1_RETRY = "L1_RETRY"
    L2_RULE_FALLBACK = "L2_RULE_FALLBACK"
    L3_TELEOP = "L3_TELEOP"
    L4_HOLD = "L4_HOLD"


@dataclass(frozen=True)
class LadderStep:
    level: DegradeLevel
    action: str           # RETRY_VLA / RUN_RULE_SKILL / REQUEST_TELEOP / HOLD
    max_times: int
    requires_human: bool
    note: str = ""


STEPS: tuple[LadderStep, ...] = (
    LadderStep(DegradeLevel.L1_RETRY, "RETRY_VLA", 2, False, "同版本重试"),
    LadderStep(DegradeLevel.L2_RULE_FALLBACK, "RUN_RULE_SKILL", 1, False, "规则技能兜底"),
    LadderStep(DegradeLevel.L3_TELEOP, "REQUEST_TELEOP", 1, True, "人工遥操作"),
    LadderStep(DegradeLevel.L4_HOLD, "HOLD", 1, True, "挂起待人工处理"),
)

_NONE_STEP = LadderStep(DegradeLevel.NONE, "NONE", 0, False, "未降级")


class DegradationLadder:
    """按失败次数推进降级；到顶后保持 L4。

    初始为 NONE（未降级）；首次失败进入 L1。
    """

    def __init__(self) -> None:
        self._idx = -1           # 当前级（-1 = 未降级）
        self._used = 0           # 当前级已用次数
        self._degraded = False

    @property
    def level(self) -> DegradeLevel:
        return DegradeLevel.NONE if self._idx < 0 else STEPS[self._idx].level

    @property
    def degraded(self) -> bool:
        return self._degraded

    def step(self) -> LadderStep:
        return _NONE_STEP if self._idx < 0 else STEPS[self._idx]

    def on_failure(self) -> LadderStep:
        """记录一次失败，返回应当采取的动作。"""
        self._degraded = True
        if self._idx < 0:
            self._idx = 0
            self._used = 0
        cur = STEPS[self._idx]
        self._used += 1
        if self._used >= cur.max_times and self._idx < len(STEPS) - 1:
            self._idx += 1
            self._used = 0
        return STEPS[self._idx]

    def on_success(self) -> None:
        """成功后回到未降级态但不清除 degraded 标记（本次作业已降级过）。"""
        self._idx = -1
        self._used = 0

    def reset(self) -> None:
        self._idx = -1
        self._used = 0
        self._degraded = False
