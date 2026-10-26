# 项目更新同步记录

> 本仓库 = 天工（Tiangong-EWA）具身智能工厂作业智能体平台。
> 规则：只追加，不修改历史行。

---

## 2026-10-25 ｜ v1.0 建立（基线）

- 市场依据：2026 中国具身智能市场破万亿；工信部+国资委"实景实训"专项行动；行业矛盾"Demo 多、订单少"
- 定位：异构机群调度 + 具身技能治理 + 证据链 + 数据飞轮的作业智能体平台（不做本体、不训 VLA）
- 文档：docs/00–11（12 份）+ specs/（宪法 + SPEC-001/002，含 Gherkin）+ DECISIONS.md + AGENTS.md
- 工程：tiangong_core 十一模块 + agent-runtime（REST+MCP）+ MockRobotDriver 仿真 + gRPC 契约
- 测试：58 项全绿，核心+服务覆盖率 92%（门禁 80%）
- 导出：export/ 22 份 docx + 22 份 pdf
- 安全：五条红线（禁入/人工令牌/数据源/停机/证据）代码级实现

---

## 2026-10-26 ｜ v1.1 重构升级（五视角审查后）

- 审查：招标师 / FDE / 架构师 / 教授级高工 / 高级美工五视角独立打分 **78/100**，低于 80 可交付线 → 触发重构（docs/43）
- 缺口：无缓存、无读写分离、无事件总线、无可观测、多品牌仅 mock、无可视化、缺标准矩阵与运维手册
- 工程新增（7 模块）：
  - `app/repository.py`：CQRS 读写分离（SQLite WAL 写主读从，6 表，审计只追加）
  - `app/cache.py`：多级缓存（L1 TTL + L2 可插拔，前缀失效，命中率统计，L2 故障降级）
  - `app/events.py`：事件总线（Event + Transport 抽象 + MemoryTransport，可换 Pulsar/Redis）
  - `app/otel.py`：OTel GenAI SemConv 三层可观测（Session>Trace>Span，无 SDK 依赖）
  - `app/robots/vda5050.py`：VDA 5050 适配器 + AdapterRegistry（多品牌健康探测）
  - `app/memory/`：Hermes 三层记忆（工作/情景/技能）
  - `app/a2a.py`：Agent Card + 意图委派（与 Diting-PdM 互联）
- 服务新增端点：`/observability/spans`、`/observability/metrics`、`/events`、`/adapters/health`、`/audit`、`/a2a/v1/card`、`/a2a/v1/tasks`
- 测试：96 项全绿（44 核心 + 14 服务 + 38 重构专项），覆盖率 **93%**；ruff 零告警；Litestar 弃用告警清零
- 文档：新增 `docs/12 标准符合性矩阵`、`docs/38 运维手册`、`docs/43 多视角审查与重构总路线`；`docs/03/08/09` 同步 v1.1
- 可视化：`web/index.html` 单文件运营看板（机群遥测 / 任务看板 / 安全区 SVG 地图 / KPI）
- 导出：export/ 26 份 docx + 26 份 pdf（清理旧编号残留）
- 自评：招标 19 / FDE 18 / 架构 19 / 教授 18 / 美工 17 → **91/100**

---

## 2026-10-26 ｜ v1.2 数据规模与性能专项

- 数据层：新增分析库（与业务库物理分离），生成并写入 **100,000,000 条**作业事实
  （wo 15M / task 40M / episode 35M / audit 10M），落盘 **5.03 GiB**，装载 1,027 s + 建索引 357 s
- 读模型：新增 `tools/build_rollup.py` 预聚合（agg_daily / agg_daily_task / agg_daily_skill /
  agg_daily_action / agg_hourly，共 5,164 行），聚合查询 **173,537 ms → 0.23 ms**
- 缓存：`cache.py` 增加 `RedisL2`（JSON + SCAN 前缀失效 + 故障降级）；`state.py` 按 `TIANGONG_REDIS_URL` 装配；
  新增 `tools/redis_compat.py`（RESP 兼容服务）与 `tools/warmup_cache.py`（211 key / 138.6 KiB / 1.01 s）
- 缺陷修复：L2 命中回填 L1 时误改写 L2 TTL，导致预热键 5 s 后过期 → 拆分 `_set_l1` + 回归测试
- 分析 API：新增 `/tg-api/v1/analytics/{scale,summary,hourly,audit}`、`/analytics/orders/{id}/timeline`
- 看板：`web/index.html` 改为**实时数据源**（API 驱动 + 演示兜底 + 数据源徽标），
  新增下单/审批/执行闭环表单、分析库概览卡；服务端同源托管 `/` + CORS
- 测试：新增 `tests/test_analytics_cache.py`（24 项）与 `tests/e2e/`（Playwright 13 项）；
  合计 **120 项单元/接口 + 13 项 E2E 全绿**，覆盖率 93%
- 压测：新增 `tests/perf/k6_tiangong.js` 与 `locustfile.py`；
  k6 16,823 请求 / 256.7 rps / **0 失败** / P95 **1.53 ms**；
  Locust 7,757 请求 / 131.3 rps / **0 失败** / P95 **4 ms**；压测期缓存命中率 **99.96%**
- 文档：新增 `docs/44_全链路与性能测试报告.md`；docs/03（读模型 + A14–A16）、docs/08（E2E/性能层）、
  docs/09（`TIANGONG_BULK_DB`/`TIANGONG_REDIS_URL` + 性能基线）、docs/38（巡检项 + 容量）同步

---

## 2026-10-26 ｜ v1.2 增补：局域网人工测试指南 + 配套课程

- 新增 `docs/45_局域网人工测试操作指南.md`：访问地址/防火墙/代理/端口排查、
  必测主链路 10 项（含 R1/R2 红线）、多端验证、接口自检 9 项、测试记录表、缺陷反馈模板
- 新增配套课程仓库：[tiangong-course](https://github.com/tianqiongenze/tiangong-course)
  （64 课时 · 德国双元制本土化 · 费曼 + 布鲁姆；16 套 PPT + 44 份文档的 Word/PDF）
- 课程与上游同步机制：`tools/sync_upstream.py` 生成 `upstream/` 快照并扫描过期数字

---

## 2026-10-26 ｜ v1.2 增补：局域网人工测试指南 + 配套课程

- 新增 `docs/45_局域网人工测试操作指南.md`：访问地址/防火墙/代理/端口排查、
  必测主链路 10 项（含 R1/R2 红线）、多端验证、接口自检 9 项、测试记录表、缺陷反馈模板
- 新增配套课程仓库：[tiangong-course](https://github.com/tianqiongenze/tiangong-course)
  （64 课时 · 德国双元制本土化 · 费曼 + 布鲁姆；16 套 PPT + 44 份文档的 Word/PDF）
- 课程与上游同步机制：`tools/sync_upstream.py` 生成 `upstream/` 快照并扫描过期数字
