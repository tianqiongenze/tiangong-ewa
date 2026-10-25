# impl · 天工可运行工程

> 具身智能工厂作业智能体平台的最小可运行实现。
> 无真机可跑（MockRobotDriver 仿真机群），有真机按 `contracts/proto/robot_link.proto` 接。

## 结构

```
impl/
├── packages/tiangong_core/     领域核（纯函数 + 不可变模型，可单测）
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
├── services/agent-runtime/      REST + MCP 服务（Litestar）
│   ├── app/orchestrator.py      编排闭环
│   ├── app/robots/              机器人链路（协议 + 仿真驱动）
│   ├── app/server.py             REST API（/tg-api/v1）
│   ├── app/mcp.py                MCP 服务端（/mcp，7 工具）
│   └── app/seed.py               演示厂区（脱敏合成数据）
├── tests/                       58 项测试（核心 94% 覆盖）
├── contracts/proto/             真机链路 gRPC 契约
├── deploy/k8s/                  部署清单
└── tools/demo_run.py            演示闭环
```

## 快速开始

```bash
# 1. 安装依赖（任选 Python 3.12+ 环境）
pip install -r services/agent-runtime/requirements.txt

# 2. 跑测试（无需任何硬件）
make test            # 或：PYTHONPATH=packages/tiangong_core:services/agent-runtime pytest -q

# 3. 跑演示闭环
make demo            # 正常搬运
make demo -- --chaos # 注入失败，看降级阶梯
make demo -- --person # 停止区有人，看 R4 停机

# 4. 起服务
make run             # :8000
curl localhost:8000/healthz
curl localhost:8000/tg-api/v1/robots
```

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

## 安全要点（与宪法一致）

| 规则 | 行为 |
|---|---|
| 禁入区目标 | 动作拒绝（R1），任务 ABORTED |
| >50kg 或危险动作 | 必须人工令牌（R2），模型不得自签 |
| 停止区有人 | 停机（R4），任务 FAILED 待人工确认后重试 |
| 证据不全 | 不得 DONE（打回重做） |
| 技能变更 | 审批 → 灰度 → 可回滚（四态） |

## 接真机

1. 按 `contracts/proto/robot_link.proto` 实现厂商 adapter（gRPC 服务端跑在机器人或网关上）；
2. 在 `app/robots/` 新增 `GrpcRobotLink`（实现 `base.RobotLink` 协议）；
3. `app/state.py` 换装配——**不动核心包与服务逻辑**（宪法第九条）。
