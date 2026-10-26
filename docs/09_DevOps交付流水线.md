# 09 · DevOps 交付流水线

| 项 | 内容 |
|---|---|
| 文档 ID | TG-DOC-09 |
| 版本 | v1.1（新增持久化/可观测/多品牌适配的部署口径） |
| 编制日期 | 2026-10-26（首版 2026-10-25） |
| 依据 | CI/CD 门禁统一 |
| 状态 | 已评审 |

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

### 3.1 运行时配置（必配项）

| 变量 / 挂载 | 生产值 | 说明 |
|---|---|---|
| `TIANGONG_DB` | `/data/tiangong.db` | **不设则为内存库，重启即丢** |
| `TIANGONG_BULK_DB` | `/data/tiangong_bulk.db` | 分析库（1 亿条事实 + 预聚合）；不设为"分析不可用"，接口降级 |
| `TIANGONG_REDIS_URL` | `redis://redis:6379/0` | L2 缓存；不设则只用 L1（功能不受影响） |
| `TIANGONG_CACHE_TTL` | `5` | L1 默认 TTL（秒） |
| `/data` 卷 | PVC（≥ 20 GiB，含分析库建议 ≥ 60 GiB） | SQLite + WAL + 分析库 |
| `/episodes` 卷 | 对象存储或 PVC | episode 分片，按天分片 |
| `TZ` | `Asia/Shanghai` | 审计时间口径统一 |

> **分析库与预聚合的部署纪律**：
> 1. 分析库随镜像/数据包分发或用 `tools/gen_bulk_data.py` + `tools/build_rollup.py` 离线构建；
> 2. **预聚合必须与明细同批更新**（否则口径漂移）：每日 ETL 末尾执行 `build_rollup.py --rebuild`；
> 3. 服务重启前执行 `tools/warmup_cache.py`，冷启动首请求即可命中 L2（docs/44 §5）。

> 单副本写库限制：SQLite 为单写者模型，**agent-runtime 写实例应为 1**；
> 需要横向扩展时，读实例可多副本（走读连接 + 缓存），写实例保持单点并由 K8s 保证重启。
> 这是 A7 决策的显式代价，生产规模化时按 A9 迁移 PostgreSQL（届时可多写）。

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

### 4.3 多品牌接入拓扑（v1.1）

```
              ┌──────────────────────────────┐
              │ agent-runtime                │
              │  AdapterRegistry             │
              └───┬───────────┬──────────┬───┘
                  │ gRPC      │ MQTT     │ gRPC
          ┌───────▼──┐  ┌─────▼─────┐  ┌─▼────────┐
          │ 厂商 A 网关│  │ VDA 5050  │  │ 厂商 B 网关│
          │（私有协议）│  │ broker    │  │（私有协议）│
          └───────┬──┘  └─────┬─────┘  └─┬────────┘
                  ▼            ▼          ▼
               AMR 群     跨品牌 AGV    机械臂/四足
```

- 适配器以**独立 Deployment 或 sidecar** 部署，崩溃不拖垮编排主进程
- broker 与网关需在安全区网络内，禁止跨区直连（等保边界）
- 新增品牌 = 新增 adapter + 注册表登记，**不改核心与服务逻辑**

---

## 5. 观测

### 5.1 指标口径

| 层 | 指标 | 采集端点 |
|---|---|---|
| 业务 | 作业单完成率 / 平均节拍 / 人工接管率 | `/tg-api/v1/episodes/stats` |
| 技能 | 每技能成功率 / 降级触发分布 | `episodes/stats.success_rate` + RunReport |
| 安全 | 红线违规次数 / 停机次数 / 急停次数 | `/tg-api/v1/audit` |
| 系统 | API P95 / span 错误率 / 缓存命中率 | `/tg-api/v1/observability/metrics` |
| 链路 | 单次作业全链 span | `/tg-api/v1/observability/spans?trace_id=` |
| 集成 | 事件投递与失败数 | `/tg-api/v1/events` |
| 适配 | 各品牌健康与延迟 | `/tg-api/v1/adapters/health` |

### 5.2 与外部 APM 对接

`app/otel.py` 输出的 span 结构对齐 OTel GenAI SemConv（`gen_ai.system`、
`gen_ai.request.model`、`gen_ai.usage.*`），可直接映射到现有 APM 的 LLM 观测面板；
未引入 OTel SDK 是刻意选择（避免版本绑定），接入时只需在 `Tracer` 内加一个 exporter。

### 5.3 告警建议阈值

见 docs/38 §5.1（含分级、首响时限与升级路径），DevOps 侧只负责规则落地与通知通道。

### 5.4 性能基线（实测，1 亿条数据）

| 场景 | 实测 | 阈值 | 数据来源 |
|---|---|---|---|
| 全链路 P95（读为主） | 1.53 ms | < 300 ms | k6 16,823 请求 |
| 下单→审批→执行 P95 | 7 ms | < 1200 ms | k6 write_chain |
| 聚合查询 P95（缓存命中） | 1 ms | < 120 ms | k6 an_summary |
| 聚合查询（预聚合，无缓存） | 0.23–1.06 ms | — | docs/44 §4.3 |
| 缓存命中率 | 99.96% | > 60% | 压测期 |
| 失败率 | 0.00% | < 1% | k6 + Locust |

> 容量结论：单进程 granian + 预聚合 + 两级缓存在本机可稳定承载 250+ rps（读为主）。
> 写路径受 SQLite 单写者限制，规模化前需按 A9 迁移 PostgreSQL。
> 回归方式：`make perf-k6` / `make perf-locust`（脚本内置阈值，不达标即退出非 0）。

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
