"""共享单例（装配点）：机群链路、编排器、作业单注册表。

独立成模块避免 server ↔ mcp 循环导入。
"""

from __future__ import annotations

from typing import Any

from tiangong_core import SkillRegistry

from .orchestrator import Orchestrator
from .robots import MockRobotDriver
from .seed import DEMO_ROBOTS, DEMO_ZONES, seed_skills

# 演示部署装配；生产换依赖注入 + 持久化（见 docs/09）
link = MockRobotDriver.demo_fleet()
skills: SkillRegistry = seed_skills()
orch = Orchestrator(link=link, robots=DEMO_ROBOTS, zones=DEMO_ZONES, skills=skills)

# order_id → {"graph", "report", "status"}
_orders: dict[str, dict[str, Any]] = {}
