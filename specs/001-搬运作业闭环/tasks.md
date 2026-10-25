# SPEC-001 任务清单

| # | 任务 | 状态 | 对应测试 |
|---|---|---|---|
| T1 | 领域模型（Task/WorkOrder/Evidence/Skill） | done | test_tiangong_core 全组 |
| T2 | RuleBasedPlanner 五模板 + DAG 校验 | done | TestTaskGraph |
| T3 | capability 硬门槛 + 可解释打分 | done | TestCapability |
| T4 | safety 围栏 + 红线 + 令牌 | done | TestSafety |
| T5 | lifecycle 九态白名单 | done | TestLifecycle |
| T6 | degrade 四级阶梯 | done | TestDegrade |
| T7 | skills 四态 + 回滚 | done | TestSkills |
| T8 | fleet 约束调度 | done | TestFleet |
| T9 | worldmodel 反事实 | done | TestWorldModel |
| T10 | episode 采集 + 分片 | done | TestEpisode |
| T11 | Orchestrator 闭环 | done | TestOrchestratorE2E |
| T12 | REST API | done | TestAPI |
| T13 | MCP 服务端 | done | TestMCP |
| T14 | MockRobotDriver + demo | done | make demo |
| T15 | gRPC 契约 | done | buf lint |
| T16 | CI 门禁 | done | .github/workflows/ci.yml |
