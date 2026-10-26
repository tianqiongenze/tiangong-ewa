"""共享单例（装配点）：机群链路、编排器、作业单注册表、缓存、事件、观测、仓储、适配器。

独立成模块避免 server ↔ mcp 循环导入。
"""

from __future__ import annotations

import os
from contextlib import suppress
from typing import Any

from tiangong_core import SkillRegistry

from .analytics import AnalyticsStore
from .cache import MultiLevelCache, RedisL2
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
# 缓存：L1 必有；配置 TIANGONG_REDIS_URL 时挂 L2（Redis），支持外部预热
_redis_url = os.environ.get("TIANGONG_REDIS_URL", "").strip()
_l2 = None
if _redis_url:
    with suppress(Exception):        # L2 不可用不影响启动（降级纪律）
        _l2 = RedisL2(_redis_url)
cache = MultiLevelCache(l2=_l2,
                        default_ttl=float(os.environ.get("TIANGONG_CACHE_TTL", "5")))
events = EventBus(MemoryTransport())                  # Pulsar/Redis transport 后置
tracer = Tracer("agent-runtime")
# 仓储：TIANGONG_DB 环境变量指定文件路径（生产）；缺省内存（演示/测试）
repo = Repository(os.environ.get("TIANGONG_DB") or ":memory:")
# 分析库：TIANGONG_BULK_DB 指向 1 亿条事实库（只读）；缺省不可用
analytics = AnalyticsStore(os.environ.get("TIANGONG_BULK_DB", ""))
adapters = AdapterRegistry()
adapters.register(VDA5050Adapter("seer", serials=["SN-SEER-01"]))    # 仙工（示范）
adapters.register(VDA5050Adapter("mushadow", serials=["SN-MS-01"]))  # 墨影（示范）
memory = HermesMemory(skills)

# 编排器插桩装配（观测/事件/持久化注入）
orch.tracer = tracer
orch.events = events
orch.repo = repo
