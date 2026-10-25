"""共享单例（装配点）：机群链路、编排器、作业单注册表、缓存、事件、观测、仓储、适配器。

独立成模块避免 server ↔ mcp 循环导入。
"""

from __future__ import annotations

import os
from typing import Any

from tiangong_core import SkillRegistry

from .cache import MultiLevelCache
from .events import EventBus, MemoryTransport
from .memory import HermesMemory
from .orchestrator import Orchestrator
from .otel import Tracer
from .repository import Repository
from .robots import AdapterRegistry, MockRobotDriver, VDA5050Adapter
from .seed import DEMO_ROBOTS, DEMO_ZONES, seed_skills

# 演示部署装配；生产换依赖注入 + 持久化（见 docs/09）
link = MockRobotDriver.demo_fleet()
skills: SkillRegistry = seed_skills()
orch = Orchestrator(link=link, robots=DEMO_ROBOTS, zones=DEMO_ZONES, skills=skills)

# order_id → {"graph", "report", "status"}
_orders: dict[str, dict[str, Any]] = {}

# v1.1 重构装配 -----------------------------------------------------------
cache = MultiLevelCache(default_ttl=5.0)              # L1；L2（Redis）后置
events = EventBus(MemoryTransport())                  # Pulsar/Redis transport 后置
tracer = Tracer("agent-runtime")
# 仓储：TIANGONG_DB 环境变量指定文件路径（生产）；缺省内存（演示/测试）
repo = Repository(os.environ.get("TIANGONG_DB") or ":memory:")
adapters = AdapterRegistry()
adapters.register(VDA5050Adapter("seer", serials=["SN-SEER-01"]))    # 仙工（示范）
adapters.register(VDA5050Adapter("mushadow", serials=["SN-MS-01"]))  # 墨影（示范）
memory = HermesMemory(skills)

# 编排器插桩装配（观测/事件/持久化注入）
orch.tracer = tracer
orch.events = events
orch.repo = repo
