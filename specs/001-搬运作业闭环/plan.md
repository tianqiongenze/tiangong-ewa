# SPEC-001 技术方案（Plan）

| 项 | 内容 |
|---|---|
| 关联 | `spec.md` |
| 状态 | Implemented |
| 工作量 | 5 人日（平台 3 / 测试 2） |

---

## 1. 架构落点

```
POST /tg-api/v1/work-orders (TRANSPORT)
   │
   ▼
RuleBasedPlanner._transport ──▶ TaskGraph{T1 PICK, T2 TRANSPORT, T3 PLACE}
   │  （weight_kg 传播 / 目标坐标挂末位 / 环检测）
   ▼
POST /approve ──▶ 全部 APPROVED（人：设备科-老周）
   │
   ▼
Orchestrator.run（拓扑序）
   ├─ _pick_robot：capability.match_robots（硬门槛 + 打分）
   ├─ _safe：safety.check_action（R1 禁入 / R2 令牌 / R4 停机 / 限速）
   ├─ link.execute：MockRobotDriver（仿真）或 gRPC adapter（真机）
   └─ _finish：EvidenceChain 挂载 → verify_evidence → DONE
   │
   ▼
episode 入库（consent_id）+ RunReport 可回放
```

## 2. 关键取舍

| 决策 | 选择 | 理由 |
|---|---|---|
| 规划器 | 规则模板 | 确定性、可单测；LLM 接口后置 |
| 分配策略 | 单任务最高分 | 教学可解释；批量调度在 fleet.py 独立提供 |
| 重试纪律 | FAILED → 重新审批 → 再执行 | 状态机诚实：失败必须显式重新放行 |
| 仿真 | 2D 平面一步到位 + 按距离扣电 | 足够验证逻辑，不假装物理 |

## 3. 数据契约

见 `docs/06` §2.1 下单体；任务参数键：`at/from/to/what/weight_kg/x/y`。

## 4. 风险

| 风险 | 对策 |
|---|---|
| 真机移动非一步到位 | adapter 层处理；平台只认完成信号 |
| 目标坐标精度 | 遥测闭环校验（PLACE 后回读位置） |
