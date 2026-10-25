# SPEC-002 技术方案（Plan）

| 项 | 内容 |
|---|---|
| 关联 | `spec.md` |
| 状态 | Implemented |
| 工作量 | 4 人日（平台 2 / 测试 1 / 安全评审 1） |

---

## 1. 架构落点

```
Action(kind, robot_id, x, y, payload_kg, human_token)
   │
   ▼
check_action(action, zones, person_in_stop_zone)
   ├─ R1 禁入区遍历        → RedLineError（拒绝）
   ├─ R2 令牌判定          → HUMAN_TOKEN_ACTIONS ∪ payload>50kg
   ├─ R4 人员停机          → Verdict(speed_limit=0)
   └─ SLOW 区              → Verdict(speed_limit=z.speed_limit)
```

## 2. 关键取舍

| 决策 | 选择 | 理由 |
|---|---|---|
| 令牌簿位置 | 编排层（Orchestrator.human_tokens） | 平台只认簿子；签发在外部审批流 |
| 停机语义 | Verdict 而非异常 | 停机是正常安全状态，不是错误 |
| 恢复路径 | 人工确认后重新提交任务 | 杜绝"自动恢复" |
| 急停 | 链路层 stopped 集合 | 抢占式，不依赖编排状态 |

## 3. 与本体安全的分界

平台负责"不发起危险动作 + 危险现场必停"；
本体的急停回路、安全 PLC 由机器人厂商负责（docs/07 §4）。
