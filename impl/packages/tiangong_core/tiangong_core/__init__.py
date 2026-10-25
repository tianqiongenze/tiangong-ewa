"""天工核心包（tiangong_core）。

具身智能工厂作业智能体的领域核：任务分解、能力匹配、安全区、证据链、
状态机、降级、技能治理、机群调度、世界模型、数据飞轮。

设计纪律（与项目宪法一致）：
- 纯函数 + 不可变数据类，核心算法可单测（TDD，覆盖率 ≥ 90%）
- 不依赖任何框架，可被 agent-runtime 服务与仿真器共同引用
- 所有对外结论必须带证据（evidence），无证据不得输出
"""

from .capability import match_robots, score_robot  # noqa: F401
from .degrade import DegradationLadder, DegradeLevel, LadderStep  # noqa: F401
from .episode import Episode, EpisodeRecorder  # noqa: F401
from .evidence import EvidenceChain, verify_evidence  # noqa: F401
from .fleet import Assignment, FleetScheduler, ScheduleResult  # noqa: F401
from .lifecycle import IllegalTransition, allowed_next, transition  # noqa: F401
from .models import (  # noqa: F401
    Evidence,
    EvidenceItem,
    Robot,
    RobotCapability,
    SafetyZone,
    Skill,
    SkillVersion,
    Task,
    TaskStatus,
    WorkOrder,
    WorkOrderKind,
    ZoneKind,
)
from .safety import (  # noqa: F401
    RED_LINES,
    Action,
    RedLineError,
    Verdict,
    check_action,
    check_geofence,
    speed_limit_in,
)
from .skills import SkillRegistry, SkillState  # noqa: F401
from .taskgraph import PlanError, TaskGraph, decompose, topological_order  # noqa: F401
from .worldmodel import Counterfactual, counterfactual, feasibility  # noqa: F401

__version__ = "0.1.0"
