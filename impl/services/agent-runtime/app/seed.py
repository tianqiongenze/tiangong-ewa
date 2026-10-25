"""演示种子数据：雅迪-style 虚拟厂区（脱敏合成，无真实生产数据）。

厂区布局（2D 平面，米）：
- 装配车间（0–40 × 0–20）：人行通道 SLOW 区
- 原料库（40–60 × 0–20）：重载搬运目标
- 高压配电房（50–60 × 50–60）：禁入区
- 人机交叉作业区（0–10 × 0–10）：停止区
"""

from __future__ import annotations

from tiangong_core import (
    Robot,
    RobotCapability,
    SafetyZone,
    Skill,
    SkillRegistry,
    ZoneKind,
)

DEMO_ZONES = (
    SafetyZone("Z-HV-ROOM", ZoneKind.FORBIDDEN, 50, 50, 60, 60),
    SafetyZone("Z-HUMAN-CROSS", ZoneKind.STOP, 0, 0, 10, 10),
    SafetyZone("Z-WALKWAY", ZoneKind.SLOW, 0, 12, 40, 16, speed_limit=0.3),
)

DEMO_ROBOTS = (
    Robot("AMR-01", "AMR", "P200", RobotCapability(
        payload_kg=200, max_speed=1.5, sensors=("camera", "lidar"),
        skills=("transport", "docking")), x=2.0, y=2.0),
    Robot("AMR-02", "AMR", "P200", RobotCapability(
        payload_kg=200, max_speed=1.5, sensors=("camera", "lidar"),
        skills=("transport", "docking")), x=30.0, y=8.0),
    Robot("ARM-01", "ARM", "S6", RobotCapability(
        payload_kg=6, max_speed=2.0, reach_m=1.2,
        sensors=("camera", "force"), skills=("pick_place",)),
        x=12.0, y=4.0),
    Robot("QD-01", "QUADRUPED", "B2", RobotCapability(
        payload_kg=10, max_speed=1.8, sensors=("camera", "thermal"),
        skills=("inspect", "docking"), intrinsically_safe=True),
        x=18.0, y=6.0),
)


def seed_skills() -> SkillRegistry:
    """种子技能（版本化、受审批）。"""
    reg = SkillRegistry()
    reg.register(Skill("pick_place", "取放（VLA）", description="上下料工位取放"))
    reg.register(Skill("transport", "搬运（VLA）", description="点位间物料搬运"))
    reg.register(Skill("scan", "巡检读数（规则+VLA）", description="读表/测温/拍照"))
    reg.publish("transport", "1.0.0", "vla://transport/1.0.0", canary_pct=10)
    reg.approve("transport", "1.0.0", approved_by="设备科-老周", canary_pct=10)
    reg.publish("pick_place", "1.0.0", "vla://pp/1.0.0", canary_pct=10)
    reg.approve("pick_place", "1.0.0", approved_by="设备科-老周", canary_pct=10)
    reg.publish("scan", "1.0.0", "rule://scan/1.0.0", canary_pct=20)
    reg.approve("scan", "1.0.0", approved_by="设备科-老周", canary_pct=20)
    return reg
