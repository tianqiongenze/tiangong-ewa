# impl · 天工可运行工程

> 具身智能工厂作业智能体平台的最小可运行实现。
> 无真机可跑（MockRobotDriver 仿真机群），有真机按 `contracts/proto/robot_link.proto` 接，
> 跨品牌 AGV/AMR 走 VDA 5050（`app/robots/vda5050.py`）。

**状态**：v1.1 · 96 项测试全绿 · 覆盖率 93% · ruff 零告警 · CI 全绿

## 结构

```
impl/
├── packages/tiangong_core/     领域核（纯函数 + 不可变模型，零基础设施依赖）
│   ├── models.py                数据模型（九态任务/安全区/能力画像/技能版本）
│   ├── taskgraph.py             作业单 → 任务 DAG（规则规划器 + 环检测）
│   ├── capability.py            能力匹配（硬门槛 + 可解释打分）
│   ├── safety.py                电子围栏 + 五条红线（禁入/人工令牌/停机）
│   ├── evidence.py              证据链（无证据不判完成）
│   ├── lifecycle.py             九态状态机（迁移白名单）
│   ├── degrade.py               降级阶梯 L1重试→L2规则→L3遥操→L4挂起
│   ├── skills.py                技能治理（版本/审批/灰度/回滚）
│   ├── fleet.py                 机群调度（约束 + 打分）
│   ├── worldmodel.py            反事实可行性（"X 缺席还能干吗"）
│   └── episode.py               数据飞轮（实景 episode 采集）
├── services/agent-runtime/      REST + MCP + A2A 服务（Litestar）
│   ├── app/orchestrator.py      编排闭环（内嵌 span / 事件 / 持久化插桩）
│   ├── app/repository.py        CQRS 读写分离（SQLite WAL，写主读从，6 表）
│   ├── app/cache.py             多级缓存（L1 TTL + L2 可插拔，命中率统计）
│   ├── app/events.py            事件总线（Transport 抽象 + MemoryTransport）
│   ├── app/otel.py              OTel GenAI SemConv（Session>Trace>Span）
│   ├── app/memory/              Hermes 三层记忆（工作/情景/技能）
│   ├── app/a2a.py               Agent Card + 意图委派
│   ├── app/robots/              链路抽象 + Mock 驱动 + VDA 5050 适配器 + 注册表
│   ├── app/server.py            REST API（/tg-api/v1 + /a2a/v1）
│   ├── app/mcp.py               MCP 服务端（/mcp，7 工具）
│   └── app/seed.py              演示厂区（脱敏合成数据）
├── tests/                       96 项测试（44 核心 + 14 服务 + 38 重构专项）
├── contracts/proto/             真机链路 gRPC 契约
├── deploy/k8s/                  部署清单
└── tools/demo_run.py            演示闭环
```

## 快速开始

```bash
# 0. 建虚拟环境（仓库根目录）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r impl/services/agent-runtime/requirements.txt ruff

# 1. 跑测试（无需任何硬件）
cd impl && make test          # 96 项
make coverage                 # 覆盖率（当前 93%）
make lint                     # ruff

# 2. 跑演示闭环
make demo                     # 正常搬运
make demo -- --chaos          # 注入失败，看降级阶梯
make demo -- --person         # 停止区有人，看 R4 停机

# 3. 起服务（持久化到文件，重启不丢）
TIANGONG_DB=/data/tiangong.db make run      # :8000
curl localhost:8000/healthz
curl localhost:8000/tg-api/v1/robots
```

> `TIANGONG_DB` 不设置时使用内存库（`mode=memory`，多连接共享），
> 适合演示与测试；**生产必须显式指定文件路径**。

## 一次完整调用（REST）

```bash
# 下单（PENDING，不执行）
curl -X POST localhost:8000/tg-api/v1/work-orders -H 'Content-Type: application/json' -d '{
  "kind": "TRANSPORT",
  "params": {"from": "原料库", "to": "装配车间", "what": "电池包",
             "weight_kg": 35, "x": 25, "y": 14},
  "requested_by": "设备科-老周"}'
# → {"order_id": "WO-xxxx", "tasks": [T1 PICK, T2 TRANSPORT, T3 PLACE]}

# 人工审批（人确认后才可执行）
curl -X POST localhost:8000/tg-api/v1/work-orders/WO-xxxx/approve \
     -H 'Content-Type: application/json' -d '{"approved_by": "老周"}'

# 执行（返回证据链 + 降级状态）
curl -X POST localhost:8000/tg-api/v1/work-orders/WO-xxxx/run \
     -H 'Content-Type: application/json' -d '{}'
```

## 接口一览

| 类别 | 端点 |
|---|---|
| 基础 | `GET /healthz`、`GET /version` |
| 机群 | `GET /tg-api/v1/robots`、`GET /tg-api/v1/zones`、`POST /robots/{id}/estop` |
| 作业单 | `POST /work-orders`、`POST /work-orders/{id}/approve`、`POST /work-orders/{id}/run`、`GET /work-orders[/{id}]` |
| 技能 | `POST /skills/{id}/versions`、`POST .../versions/{v}/approve`、`POST /skills/{id}/rollback`、`GET /skills` |
| 令牌 | `POST /tg-api/v1/tokens`（仅人调用） |
| 飞轮 | `GET /tg-api/v1/episodes/stats` |
| 观测 | `GET /observability/spans?trace_id=`、`GET /observability/metrics`、`GET /events` |
| 适配 | `GET /adapters/health` |
| 审计 | `GET /audit?limit=` |
| 互联 | `GET /a2a/v1/card`、`POST /a2a/v1/tasks` |
| MCP | `POST /mcp`（initialize / tools/list / tools/call，7 工具） |

## 安全要点（与宪法一致）

| 规则 | 行为 |
|---|---|
| 禁入区目标 | 动作拒绝（R1），任务 ABORTED |
| >50kg 或危险动作 | 必须人工令牌（R2），模型不得自签 |
| 停止区有人 | 停机（R4），任务 FAILED 待人工确认后重试 |
| 证据不全 | 不得 DONE（打回重做） |
| 技能变更 | 审批 → 灰度 → 可回滚（四态） |

> 红线与令牌**不进缓存**：安全判定永远实时（docs/03 A10）。

## 工程能力（v1.1）

| 能力 | 模块 | 运维观测点 |
|---|---|---|
| 读写分离 | `repository.py`（写主/读从，WAL） | `PRAGMA integrity_check`、审计条数 |
| 多级缓存 | `cache.py`（L1 TTL + L2 可插拔） | `metrics.cache.hit_rate` |
| 事件总线 | `events.py`（可换 Pulsar/Redis） | `events.stats.failed` |
| 链路追踪 | `otel.py`（GenAI 语义属性） | `metrics.spans_error`、`p95_ms` |
| 多品牌适配 | `robots/vda5050.py` + 注册表 | `adapters/health` |
| 三层记忆 | `memory/`（工作/情景/技能） | 情景索引按技能聚合 |
| 智能体互联 | `a2a.py` | Agent Card 能力声明 |

## 接真机

1. 按 `contracts/proto/robot_link.proto` 实现厂商 adapter（gRPC 服务端跑在机器人或网关上）；
2. 跨品牌 AGV/AMR 用 `VDA5050Adapter`（MQTT 主题 `uagv/v2/{manufacturer}/{serial}/order|instantAction|state`）；
3. 在 `app/robots/` 新增实现 `base.RobotLink` 协议的类，注册进 `AdapterRegistry`；
4. `app/state.py` 换装配——**不动核心包与服务逻辑**（宪法第九条）。

## 相关文档

`docs/03` 架构 · `docs/06` 接口 · `docs/08` 测试 · `docs/09` DevOps · `docs/38` 运维手册 · `docs/43` 审查与重构
