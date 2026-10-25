"""机器人链路抽象（RobotLink）。

真机通过适配器接入（VDA 5050 / gRPC / 厂商 SDK），本仓库交付：
- ``RobotLink`` 协议（遥测下发 + 动作执行 + 急停）
- ``MockRobotDriver`` 仿真驱动（无真机也能全链路跑通，用于 CI 与教学）

纪律：链路层**不做安全判定**——安全在 tiangong_core.safety（运行时层）。
链路只负责"把已通过审查的动作发出去"，并在遥测里回传现场事实。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class Telemetry:
    robot_id: str
    x: float
    y: float
    battery_pct: float
    speed: float
    online: bool
    person_in_stop_zone: bool = False
    at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    detail: str = ""
    telemetry_ref: str = ""
    duration_s: float = 0.0


class RobotLink(Protocol):
    """机器人链路协议。"""

    def telemetry(self, robot_id: str) -> Optional[Telemetry]:  # pragma: no cover
        ...

    def execute(self, robot_id: str, action: Mapping[str, Any]) -> ExecutionResult:  # pragma: no cover
        ...

    def emergency_stop(self, robot_id: str) -> bool:  # pragma: no cover
        ...

    def list_robots(self) -> Sequence[str]:  # pragma: no cover
        ...
