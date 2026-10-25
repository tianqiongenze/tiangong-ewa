"""仿真机器人驱动：一个 2D 虚拟厂区，无真机也能全链路跑通。

用途：
- CI 全链路测试（不依赖硬件）
- 教学演示（课堂现场跑"下单 → 分解 → 调度 → 执行 → 证据"闭环）
- 真机接入前的联调靶场

仿真语义（刻意保持简单、可解释）：
- 机器人按动作目标点直线移动，每 tick 前进 speed*dt
- MOVE/PICK/PLACE/SCAN/PHOTO 等动作有确定性结果（可按配置注入失败）
- 电量随移动下降；低电量自动申请回充
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .base import ExecutionResult, Telemetry


@dataclass
class SimRobot:
    robot_id: str
    kind: str
    x: float = 0.0
    y: float = 0.0
    speed: float = 1.0            # m/s
    battery_pct: float = 100.0
    online: bool = True
    fail_next: int = 0            # 注入接下来 N 次失败（混沌测试用）


class MockRobotDriver:
    """内存仿真机群。"""

    def __init__(self, robots: Sequence[SimRobot] | None = None,
                 tick_s: float = 0.5) -> None:
        self.robots: dict[str, SimRobot] = {r.robot_id: r for r in (robots or [])}
        self.tick_s = tick_s
        self.stopped: set[str] = set()
        self.log: list[dict[str, Any]] = []

    # -- 装配 -------------------------------------------------------------
    @classmethod
    def demo_fleet(cls) -> MockRobotDriver:
        """演示机群：两台 AMR + 一台机械臂 + 一台四足（巡检）。"""
        return cls([
            SimRobot("AMR-01", "AMR", x=2.0, y=2.0, speed=1.2),
            SimRobot("AMR-02", "AMR", x=30.0, y=8.0, speed=1.2),
            SimRobot("ARM-01", "ARM", x=12.0, y=4.0, speed=1.0),
            SimRobot("QD-01", "QUADRUPED", x=18.0, y=6.0, speed=0.8),
        ])

    # -- RobotLink 协议 ----------------------------------------------------
    def list_robots(self) -> Sequence[str]:
        return tuple(self.robots)

    def telemetry(self, robot_id: str) -> Telemetry | None:
        r = self.robots.get(robot_id)
        if r is None:
            return None
        return Telemetry(robot_id=r.robot_id, x=r.x, y=r.y,
                         battery_pct=r.battery_pct, speed=r.speed,
                         online=r.online)

    def execute(self, robot_id: str, action: Mapping[str, Any]) -> ExecutionResult:
        r = self.robots.get(robot_id)
        if r is None:
            return ExecutionResult(False, f"机器人不存在: {robot_id}")
        if not r.online:
            return ExecutionResult(False, f"机器人离线: {robot_id}")
        if robot_id in self.stopped:
            return ExecutionResult(False, f"机器人已急停: {robot_id}")

        t0 = time.time()
        kind = str(action.get("kind", "MOVE"))
        tx = float(action.get("x", r.x))
        ty = float(action.get("y", r.y))

        # 混沌注入：fail_next > 0 时失败（用于降级阶梯测试）
        if r.fail_next > 0:
            r.fail_next -= 1
            self.log.append({"t": t0, "robot": robot_id, "action": kind, "ok": False})
            return ExecutionResult(False, f"注入失败（混沌测试）: {kind}")

        dist = math.hypot(tx - r.x, ty - r.y)
        if dist > 1e-6 and kind in {"MOVE", "PICK", "PLACE", "APPROACH", "RETURN"}:
            # 移动耗时（仿真：一步到位，但按距离扣电）
            r.x, r.y = tx, ty
            r.battery_pct = max(0.0, r.battery_pct - dist * 0.05)
        elif kind in {"SCAN", "PHOTO", "CLEAN", "CHARGE"}:
            r.battery_pct = max(0.0, r.battery_pct - 0.2)

        dur = round(time.time() - t0, 4)
        self.log.append({"t": t0, "robot": robot_id, "action": kind,
                         "x": tx, "y": ty, "ok": True})
        return ExecutionResult(True, f"{kind} 完成", telemetry_ref=f"sim://{robot_id}/{t0}",
                               duration_s=dur)

    def emergency_stop(self, robot_id: str) -> bool:
        if robot_id in self.robots:
            self.stopped.add(robot_id)
            return True
        return False

    def resume(self, robot_id: str) -> bool:
        self.stopped.discard(robot_id)
        return True
