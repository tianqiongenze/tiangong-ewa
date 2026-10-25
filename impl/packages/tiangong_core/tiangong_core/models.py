"""领域数据模型（不可变）。

所有模型用 ``dataclass(frozen=True)``：状态只能通过显式迁移函数改变，
杜绝"悄悄改一个字段"式的腐蚀（宪法第一条：规格是唯一真相源）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class WorkOrderKind(StrEnum):
    """作业单类型（首期五类，对应实景实训九大场景中的产线高频作业）。"""

    TRANSPORT = "TRANSPORT"          # 搬运：把物料从 A 送到 B
    INSPECT = "INSPECT"              # 巡检：按路线读表、测温、拍照
    LOAD_UNLOAD = "LOAD_UNLOAD"      # 上下料：机床/产线工位取放
    CLEAN = "CLEAN"                  # 清洁：地面/设备表面
    CHARGE = "CHARGE"                # 回充：低电量自主归位


class ZoneKind(StrEnum):
    """安全区类型（GB/T 16855 / ISO 13849 概念的本土化映射）。"""

    FORBIDDEN = "FORBIDDEN"   # 禁入区：任何机器人不得进入（高压间、化学品库）
    STOP = "STOP"             # 停止区：有人即停（人机交叉作业区）
    SLOW = "SLOW"             # 限速区：降速通过（人行通道、窄门）


class TaskStatus(StrEnum):
    """任务生命周期（九态，与 Diting-PdM 技能状态同构，便于教学迁移）。"""

    PENDING = "PENDING"        # 已分解，待审批
    APPROVED = "APPROVED"      # 已批准，待分配
    ASSIGNED = "ASSIGNED"      # 已分配到机器人
    EXECUTING = "EXECUTING"    # 执行中
    VERIFYING = "VERIFYING"    # 证据校验中
    DONE = "DONE"              # 完成（证据已核验）
    FAILED = "FAILED"          # 失败（可重试）
    ABORTED = "ABORTED"        # 中止（安全原因，人工触发）
    SUPERSEDED = "SUPERSEDED"  # 被新任务取代（重规划产生）


@dataclass(frozen=True)
class SafetyZone:
    """矩形安全区（工厂平面坐标，米）。"""

    zone_id: str
    kind: ZoneKind
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    speed_limit: float = 0.5   # SLOW 区限速 m/s

    def contains(self, x: float, y: float) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max


@dataclass(frozen=True)
class RobotCapability:
    """机器人能力画像（调度匹配的依据）。"""

    payload_kg: float                 # 额定负载
    max_speed: float                  # 最高速度 m/s
    reach_m: float | None = None   # 机械臂展（上下料类需要）
    sensors: tuple[str, ...] = ()     # camera/lidar/thermal/force
    skills: tuple[str, ...] = ()      # 具备的技能 ID
    endurance_min: float = 240.0      # 续航（分钟）
    intrinsically_safe: bool = False  # 本质安全（碰撞不伤人）→ 可进 STOP 区


@dataclass(frozen=True)
class Robot:
    """一台在册机器人（ fleet 成员 ）。"""

    robot_id: str
    kind: str                        # AMR / QUADRUPED / HUMANOID / ARM / COBOT
    model: str
    capability: RobotCapability
    battery_pct: float = 100.0
    online: bool = True
    x: float = 0.0
    y: float = 0.0
    firmware: str = "1.0.0"


@dataclass(frozen=True)
class EvidenceItem:
    """一条证据（结论必须挂证据，宪法第三条）。"""

    kind: str            # telemetry / snapshot / skill_version / model_version / human_token
    ref: str             # 引用（trace_id / 文件路径 / 版本号）
    detail: str = ""
    captured_at: str = ""


@dataclass(frozen=True)
class Evidence:
    """证据链：一组有序证据。"""

    items: tuple[EvidenceItem, ...] = ()

    def add(self, item: EvidenceItem) -> Evidence:
        return Evidence(items=self.items + (item,))

    def kinds(self) -> tuple[str, ...]:
        return tuple(i.kind for i in self.items)


@dataclass(frozen=True)
class Task:
    """分解后的原子任务（DAG 节点）。"""

    task_id: str
    work_order_id: str
    kind: str                  # PICK / TRANSPORT / PLACE / SCAN / PHOTO / RETURN / CHARGE
    params: Mapping[str, Any]  # 起点、终点、目标、读表项等
    depends_on: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: str | None = None
    evidence: Evidence = field(default_factory=Evidence)
    attempts: int = 0
    note: str = ""

    def with_status(self, status: TaskStatus, **kw: Any) -> Task:
        return replace(self, status=status, **kw)


@dataclass(frozen=True)
class WorkOrder:
    """工厂作业单（人下的意图，不是机器人指令）。"""

    order_id: str
    kind: WorkOrderKind
    params: Mapping[str, Any]
    priority: int = 5            # 1 最高
    requested_by: str = ""       # 申请人（人）
    due: str = ""                # 期望完成时间


@dataclass(frozen=True)
class SkillVersion:
    """技能的一个受控版本（审批 + 灰度 + 回滚，宪法第十条）。"""

    skill_id: str
    version: str
    state: str = "PENDING"       # PENDING / APPROVED / REJECTED / SUPERSEDED
    entry: str = ""              # 入口（技能实现标识）
    canary_pct: int = 0          # 灰度比例
    approved_by: str = ""


@dataclass(frozen=True)
class Skill:
    """技能（机器人能力的最小封装，VLA 或规则实现的上层抽象）。"""

    skill_id: str
    name: str
    versions: tuple[SkillVersion, ...] = ()
    description: str = ""

    def active_version(self) -> SkillVersion | None:
        """当前生效版本：APPROVED 中 version 最大者。"""
        approved = [v for v in self.versions if v.state == "APPROVED"]
        return max(approved, key=lambda v: v.version) if approved else None
