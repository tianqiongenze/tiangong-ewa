# AGENTS.md · 本项目智能体工作指南

> 任何 AI 智能体（或新人）在本仓库工作前必读。
> 本文件规定**怎么改这个仓库**，与 `specs/constitution.md`（十条宪法）配套。

---

## 1. 仓库地图

| 路径 | 内容 | 改前须知 |
|---|---|---|
| `docs/00–11` | 方案文档（总览/可研/SRS/架构/数据/Harness/接口/安全/测试/DevOps/路线/验收） | 先改文档再改代码（宪法第一条） |
| `specs/` | 宪法 + 特性规格（spec/plan/tasks/features） | 无规格不写码 |
| `impl/packages/tiangong_core/` | 领域核（纯函数 + 不可变模型） | 核心算法必须 TDD，覆盖率 ≥ 90% |
| `impl/services/agent-runtime/` | REST + MCP 服务（Litestar） | 契约变更先改 docs/06 |
| `impl/tests/` | 120 项（44 核心 + 14 服务 + 38 重构 + 24 分析/缓存）+ 13 项浏览器 E2E | 改核心必须同步改测试 |
| `impl/contracts/proto/` | 机器人链路 gRPC 契约 | buf lint 门禁 |
| `impl/deploy/` | K8s 清单 | 与 docs/09 对齐 |
| `export/` | 全部文档的 Word/PDF 导出 | 由 `tools/md_export.py` 生成，勿手改 |

## 2. 硬规则（违反 = PR 关闭）

1. **没有 spec 不写码**；spec 变更先改 `specs/` 再动 `impl/`
2. **安全代码（safety.py）任何改动**必须附带：新增/修改测试 + 安全评审记录
3. **核心算法改动**必须保持 `make test` 全绿 + 覆盖率 ≥ 90%（核心）/ ≥ 80%（总量）
4. **不得削弱五条红线**（禁入/令牌/数据源/停机/证据）；只能加强
5. **提交信息**必须带 spec ID 或 issue ID，例：`feat(spec-001): ...`
6. **不要直接改 `export/`**——改 md 后跑 `python tools/md_export.py` 重新生成

## 3. 标准工作流

```bash
# 1. 改规格（如需要）
vim specs/00X-*/spec.md

# 2. 改代码 + 测试（TDD：先写失败测试）
vim impl/packages/tiangong_core/tiangong_core/xxx.py
vim impl/tests/test_xxx.py

# 3. 本地门禁
cd impl && make test && make coverage && make lint

# 4. 演示冒烟（无硬件）
make demo
make demo -- --chaos
make demo -- --person

# 5. 提交（信息带 spec ID）
git commit -m "feat(spec-00X): ..."

# 6. 文档导出（如改了 docs/ 或 specs/）
python tools/md_export.py
```

## 4. 测试速查（改哪部分跑哪些）

| 改动 | 必跑测试 |
|---|---|
| taskgraph/capability/safety/evidence/lifecycle/degrade/skills/fleet/worldmodel/episode | `tests/test_tiangong_core.py` 对应组 |
| orchestrator | `TestOrchestratorE2E` 全组 |
| server/mcp | `TestAPI` / `TestMCP` |
| robots/mock | `TestOrchestratorE2E` + demo |

## 5. 常见任务

| 任务 | 做法 |
|---|---|
| 新增作业单类型 | taskgraph 加模板 + 测试 + docs/02 FR 表 + specs 规格 |
| 新增机器人能力维度 | models.RobotCapability + capability.hard_check + 测试 |
| 新增安全区类型 | models.ZoneKind + safety + 测试 + docs/07 |
| 接真机 | 实现 contracts/proto + `app/robots/` 新 adapter + state.py 换装配 |
| 新增 REST 端点 | server.py + schemas.py + TestAPI + docs/06 |

## 6. 环境

- Python ≥ 3.12；依赖见 `impl/services/agent-runtime/requirements.txt`
- 运行测试：`cd impl && PYTHONPATH=packages/tiangong_core:services/agent-runtime pytest -q`
- 演示数据全部为合成脱敏数据，无真实生产数据

## 7. 上游参照

本项目方法论与 Diting-PdM（谛听）同源：
宪法十条、Harness 十组件、证据链、技能治理、SDD 流程均由其迁移并按具身场景加强。
