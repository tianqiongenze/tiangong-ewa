# 09 · DevOps 交付流水线

| 项 | 内容 |
|---|---|
| 文档 ID | TG-DOC-09 |
| 版本 | v1.0 |
| 编制日期 | 2026-10-25 |
| 依据 | CI/CD 门禁统一 |
| 状态 | 待评审 |

---

## 1. 流水线总览

```
push/PR ──▶ CI（.github/workflows/ci.yml）
              ├─ lint（ruff）
              ├─ test（pytest --maxfail=1）
              ├─ coverage（--fail-under=80）
              ├─ demo smoke（无硬件全链路）
              └─ buf lint（契约）
                    │
                    ▼ 全绿
              build 镜像 ──▶ 部署（K3s / K8s）
```

统一门禁原则：语言可不同，门槛必须一致（与 Diting-PdM 同）。

---

## 2. 本地开发流

```bash
make test       # 全量测试
make coverage   # 覆盖率
make lint       # ruff
make demo       # 演示闭环
make run        # 起服务 :8000
make smoke      # 冒烟（healthz + robots）
```

---

## 3. 制品与镜像

- 镜像：`registry/tiangong/agent-runtime:<version>`
- 版本：语义化（0.1.0 起）；每次发版对应 git tag
- 安全基线：`readOnlyRootFilesystem` + `runAsNonRoot` + 资源限额（见 deploy/k8s）

---

## 4. 部署拓扑

### 4.1 单基地（演示/首期）

```
K3s 集群
├── agent-runtime (×2 副本)
├── PostgreSQL（作业/技能/episode 元数据）
└── 对象存储（episode 分片）
```

### 4.2 多基地（Ph3）

```
总部中心
├── 技能注册中心（审批/灰度/回滚的唯一真相源）
├── 数据湖（脱敏 episode 汇聚）
└── 看板（成功率/降级率/人工接管率）

基地边缘（每基地）
├── agent-runtime（本地闭环，弱网可自治）
├── 本地 episode 缓存（断点续传）
└── RobotLink adapters
```

> 边缘自治原则：网络中断不影响已审批作业单的本地执行；
> 恢复后 episode 与审批记录自动同步。

---

## 5. 观测

| 层 | 指标 |
|---|---|
| 业务 | 作业单完成率 / 平均节拍 / 人工接管率 |
| 技能 | 每技能成功率 / 降级触发分布 |
| 安全 | 红线违规次数 / 停机次数 / 急停次数 |
| 系统 | API P95 / 调度耗时 / 安全审查耗时（NFR-102） |

---

## 6. 回滚策略

| 对象 | 回滚方式 | RTO |
|---|---|---|
| 服务 | 镜像版本回滚 | < 5 min |
| 技能 | `POST /skills/{id}/rollback?to={v}` | 即时 |
| 计划 | 任务 SUPERSEDED + 重新分解 | 即时 |
| 数据 | episode checksum 校验 + 分片重导 | < 1 h |

---

## 7. 环境管理

| 环境 | 用途 | 数据 |
|---|---|---|
| dev | 开发联调 | 合成 |
| staging | 验收（42 项检查单） | 脱敏实景 |
| prod | 生产 | 受控 |

晋升规则：dev → staging 自动；staging → prod 需安全签字（docs/11）。
