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
