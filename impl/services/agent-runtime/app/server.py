"""agent-runtime REST 入口（Litestar）。

路径前缀 /tg-api/v1（与 Diting-PdM 的 /pdm-api 对齐，双平台并存时不冲突）。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

from litestar import Litestar, get, post
from litestar.config.cors import CORSConfig
from litestar.exceptions import HTTPException
from litestar.params import Body, PathParameter, QueryParameter
from litestar.static_files import create_static_files_router
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND
from tiangong_core import (
    WorkOrder,
    WorkOrderKind,
)

from . import __version__
from .a2a import AGENT_CARD, delegate
from .mcp import mcp_endpoint
from .schemas import (
    A2ADelegateIn,
    ApproveIn,
    RunIn,
    SkillApproveIn,
    SkillPublishIn,
    TokenIn,
    WorkOrderIn,
)
from .seed import DEMO_ZONES
from .state import (
    _orders,
    adapters,
    analytics,
    cache,
    events,
    link,
    orch,
    repo,
    skills,
    tracer,
)

SERVICE_NAME = "agent-runtime"


# ---------------------------------------------------------------- 基础
@get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@get("/version")
async def version() -> dict[str, str]:
    return {"service": SERVICE_NAME, "version": __version__, "core": "tiangong_core-0.1.0"}


# ---------------------------------------------------------------- 机群
@get("/tg-api/v1/robots")
async def list_robots() -> dict[str, Any]:
    """机群状态：在册能力 + 实时遥测。"""
    out = []
    for r in orch.robots:
        t = link.telemetry(r.robot_id)
        out.append(
            {
                "robot_id": r.robot_id,
                "kind": r.kind,
                "model": r.model,
                "payload_kg": r.capability.payload_kg,
                "skills": list(r.capability.skills),
                "telemetry": None
                if t is None
                else {"x": t.x, "y": t.y, "battery_pct": t.battery_pct, "online": t.online},
            }
        )
    return {"robots": out}


@get("/tg-api/v1/zones")
async def list_zones() -> dict[str, Any]:
    return {
        "zones": [
            {
                "zone_id": z.zone_id,
                "kind": z.kind.value,
                "rect": [z.x_min, z.y_min, z.x_max, z.y_max],
                "speed_limit": z.speed_limit,
            }
            for z in DEMO_ZONES
        ]
    }


@post("/tg-api/v1/robots/{robot_id:str}/estop", status_code=HTTP_200_OK)
async def estop(robot_id: Annotated[str, PathParameter(name="robot_id")]) -> dict[str, Any]:
    ok = link.emergency_stop(robot_id)
    if not ok:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND, detail=f"机器人不存在: {robot_id}"
        ) from None
    return {"robot_id": robot_id, "stopped": True}


# ---------------------------------------------------------------- 作业单
@post("/tg-api/v1/work-orders", status_code=201)
async def submit_work_order(data: Annotated[WorkOrderIn, Body()]) -> dict[str, Any]:
    try:
        kind = WorkOrderKind(data.kind)
    except ValueError:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST, detail=f"未知作业单类型: {data.kind}"
        ) from None
    order_id = f"WO-{uuid.uuid4().hex[:8].upper()}"
    order = WorkOrder(order_id, kind, dict(data.params), data.priority, data.requested_by, data.due)
    graph, rep = orch.submit(order)
    _orders[order_id] = {"graph": graph, "report": rep, "status": "PENDING"}
    return {
        "order_id": order_id,
        "trace_id": rep.trace_id,
        "tasks": [
            {"task_id": t.task_id, "kind": t.kind, "depends_on": list(t.depends_on)}
            for t in graph.tasks.values()
        ],
    }


@post("/tg-api/v1/work-orders/{order_id:str}/approve", status_code=HTTP_200_OK)
async def approve(
    order_id: Annotated[str, PathParameter(name="order_id")],
    data: Annotated[ApproveIn, Body()],
) -> dict[str, Any]:
    rec = _get(order_id)
    if rec["status"] != "PENDING":
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST, detail=f"当前状态 {rec['status']} 不可审批"
        )
    rec["graph"] = orch.approve(rec["graph"], rec["report"], data.approved_by)
    rec["status"] = "APPROVED"
    return {"order_id": order_id, "status": "APPROVED", "by": data.approved_by}


@post("/tg-api/v1/work-orders/{order_id:str}/run", status_code=HTTP_200_OK)
async def run(
    order_id: Annotated[str, PathParameter(name="order_id")],
    data: Annotated[RunIn, Body()],
) -> dict[str, Any]:
    rec = _get(order_id)
    if rec["status"] not in {"APPROVED", "RUNNING"}:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"当前状态 {rec['status']} 不可运行（需先审批）",
        )
    rec["status"] = "RUNNING"
    rep = orch.run(rec["graph"], rec["report"], person_in_stop_zone=data.person_in_stop_zone)
    rec["status"] = "DONE" if not rep.aborted else "ABORTED"
    return rep.to_dict()


@get("/tg-api/v1/work-orders/{order_id:str}")
async def get_order(order_id: Annotated[str, PathParameter(name="order_id")]) -> dict[str, Any]:
    rec = _get(order_id)
    return {"order_id": order_id, "status": rec["status"], "report": rec["report"].to_dict()}


@get("/tg-api/v1/work-orders")
async def list_orders() -> dict[str, Any]:
    return {"orders": [{"order_id": k, "status": v["status"]} for k, v in _orders.items()]}


# ---------------------------------------------------------------- 技能治理
@post("/tg-api/v1/skills/{skill_id:str}/versions", status_code=201)
async def publish_skill(
    skill_id: Annotated[str, PathParameter(name="skill_id")],
    data: Annotated[SkillPublishIn, Body()],
) -> dict[str, Any]:
    try:
        v = skills.publish(skill_id, data.version, data.entry, data.canary_pct)
    except Exception as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return {"skill_id": skill_id, "version": v.version, "state": v.state}


@post("/tg-api/v1/skills/{skill_id:str}/versions/{version:str}/approve", status_code=HTTP_200_OK)
async def approve_skill(
    skill_id: Annotated[str, PathParameter(name="skill_id")],
    version: Annotated[str, PathParameter(name="version")],
    data: Annotated[SkillApproveIn, Body()],
) -> dict[str, Any]:
    try:
        v = skills.approve(skill_id, version, data.approved_by, data.canary_pct)
    except Exception as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return {
        "skill_id": skill_id,
        "version": v.version,
        "state": v.state,
        "approved_by": v.approved_by,
    }


@post("/tg-api/v1/skills/{skill_id:str}/rollback", status_code=HTTP_200_OK)
async def rollback_skill(
    skill_id: Annotated[str, PathParameter(name="skill_id")],
    to_version: Annotated[str, QueryParameter(name="to")] = "",
) -> dict[str, Any]:
    try:
        v = skills.rollback(skill_id, to_version)
    except Exception as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return {"skill_id": skill_id, "active": v.version, "state": v.state}


@get("/tg-api/v1/skills")
async def list_skills() -> dict[str, Any]:
    return {
        "skills": [
            {
                "skill_id": s.skill_id,
                "active": (s.active_version().version if s.active_version() else None),
                "versions": [
                    {"version": v.version, "state": v.state, "canary_pct": v.canary_pct}
                    for v in s.versions
                ],
            }
            for s in skills.all()
        ]
    }


# ---------------------------------------------------------------- 人工令牌
@post("/tg-api/v1/tokens", status_code=201)
async def issue_token(data: Annotated[TokenIn, Body()]) -> dict[str, Any]:
    """人工签发确认令牌（真人审批流的机器入口；模型不得调用本接口）。"""
    orch.human_tokens[data.scope] = data.token
    return {"scope": data.scope, "issued": True, "signed_by": data.signed_by}


# ---------------------------------------------------------------- 数据飞轮
@get("/tg-api/v1/episodes/stats")
async def episode_stats() -> dict[str, Any]:
    rec = orch.recorder
    skill_ids = sorted({e.skill_id for eps in rec.shards().values() for e in eps})
    return {
        "total": len(rec),
        "success_rate": {sid: rec.success_rate(sid) for sid in skill_ids},
        "shards": {k: len(v) for k, v in rec.shards().items()},
    }


# ---------------------------------------------------------------- v1.1 观测
@get("/tg-api/v1/observability/spans")
async def observability_spans(
    trace_id: Annotated[str, QueryParameter(name="trace_id")] = "",
) -> dict[str, Any]:
    """OTel 对齐 span 导出（Session>Trace>Span 三层）。"""
    return {"spans": tracer.export(trace_id or None)}


@get("/tg-api/v1/observability/metrics")
async def observability_metrics() -> dict[str, Any]:
    m = tracer.metrics()
    m["cache"] = {
        "hits": cache.stats.hits,
        "misses": cache.stats.misses,
        "l1_hits": cache.stats.l1_hits,
        "l2_hits": cache.stats.l2_hits,
        "hit_rate": round(cache.stats.hit_rate, 4),
        "l2_available": cache.l2_available,
    }
    m["events"] = events.stats
    m["analytics_available"] = analytics.available
    return m


@get("/tg-api/v1/events")
async def event_stream() -> dict[str, Any]:
    return {
        "stats": events.stats,
        "recent": [e.to_dict() for e in events.transport.history()[-20:]],
    }


@get("/tg-api/v1/adapters/health")
async def adapters_health() -> dict[str, Any]:
    """多品牌适配器健康（VDA 5050 等）。"""
    return {
        "adapters": {
            brand: {"ok": h.ok, "detail": h.detail, "latency_ms": h.latency_ms}
            for brand, h in adapters.health_all().items()
        }
    }


# ---------------------------------------------------------------- 分析（1 亿条事实库）
@get("/tg-api/v1/analytics/scale")
async def analytics_scale() -> dict[str, Any]:
    """数据规模（行数/库大小），30s 缓存。"""
    return cache.get_or_load("an:scale", analytics.scale, ttl=30)


@get("/tg-api/v1/analytics/summary")
async def analytics_summary(
    days: Annotated[int, QueryParameter(name="days")] = 7,
) -> dict[str, Any]:
    """近 N 天作业概览；结果进多级缓存（可被 warmup 预热）。"""
    return cache.get_or_load(f"an:summary:{days}", lambda: analytics.summary(days), ttl=60)


@get("/tg-api/v1/analytics/hourly")
async def analytics_hourly(
    days: Annotated[int, QueryParameter(name="days")] = 1,
) -> dict[str, Any]:
    return cache.get_or_load(f"an:hourly:{days}", lambda: analytics.hourly(days), ttl=60)


@get("/tg-api/v1/analytics/audit")
async def analytics_audit(
    days: Annotated[int, QueryParameter(name="days")] = 7,
) -> dict[str, Any]:
    return cache.get_or_load(f"an:audit:{days}", lambda: analytics.audit_stats(days), ttl=60)


@get("/tg-api/v1/analytics/orders/{order_id:int}/timeline")
async def analytics_timeline(
    order_id: Annotated[int, PathParameter(name="order_id")],
) -> dict[str, Any]:
    """单作业单时间线（点查，短缓存）。"""
    return cache.get_or_load(
        f"an:timeline:{order_id}", lambda: analytics.timeline(order_id), ttl=30
    )


@get("/tg-api/v1/audit")
async def audit_trail(
    limit: Annotated[int, QueryParameter(name="limit")] = 50,
) -> dict[str, Any]:
    return {"trail": repo.audit_trail(limit)}


# ---------------------------------------------------------------- A2A
@get("/a2a/v1/card")
async def a2a_card() -> dict[str, Any]:
    return AGENT_CARD


@post("/a2a/v1/tasks", status_code=HTTP_200_OK)
async def a2a_delegate(data: Annotated[A2ADelegateIn, Body()]) -> dict[str, Any]:
    """外部智能体（如 Diting-PdM）意图委派入口。"""
    return delegate(data.intent, dict(data.params), data.source_agent)


def _get(order_id: Annotated[str, PathParameter(name="order_id")]) -> dict[str, Any]:
    rec = _orders.get(order_id)
    if rec is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=f"作业单不存在: {order_id}")
    return rec


app = Litestar(
    cors_config=CORSConfig(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    route_handlers=[
        healthz,
        version,
        list_robots,
        list_zones,
        estop,
        submit_work_order,
        approve,
        run,
        get_order,
        list_orders,
        publish_skill,
        approve_skill,
        rollback_skill,
        list_skills,
        issue_token,
        episode_stats,
        observability_spans,
        observability_metrics,
        event_stream,
        adapters_health,
        audit_trail,
        analytics_scale,
        analytics_summary,
        analytics_hourly,
        analytics_audit,
        analytics_timeline,
        a2a_card,
        a2a_delegate,
        mcp_endpoint,
    ],
)

# 运营看板同源托管（web/index.html）：避免 file:// 跨域，E2E 可直接访问 /
_WEB_DIR = Path(__file__).resolve().parents[4] / "web"
if _WEB_DIR.is_dir():
    app.register(create_static_files_router(path="/", directories=[_WEB_DIR], html_mode=True))
