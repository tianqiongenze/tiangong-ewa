"""agent-runtime REST 入口（Litestar）。

路径前缀 /tg-api/v1（与 Diting-PdM 的 /pdm-api 对齐，双平台并存时不冲突）。
"""

from __future__ import annotations

import uuid
from typing import Any

from litestar import Litestar, get, post
from litestar.status_codes import HTTP_200_OK
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.status_codes import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND

from tiangong_core import (
    Robot,
    TaskGraph,
    TaskStatus,
    WorkOrder,
    WorkOrderKind,
)

from . import __version__
from .schemas import (
    ApproveIn,
    RunIn,
    SkillApproveIn,
    SkillPublishIn,
    TokenIn,
    WorkOrderIn,
)
from .seed import DEMO_ZONES
from .state import _orders, link, orch, skills
from .mcp import mcp_endpoint

SERVICE_NAME = "agent-runtime"


# ---------------------------------------------------------------- 基础
@get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@get("/version")
async def version() -> dict[str, str]:
    return {"service": SERVICE_NAME, "version": __version__,
            "core": "tiangong_core-0.1.0"}


# ---------------------------------------------------------------- 机群
@get("/tg-api/v1/robots")
async def list_robots() -> dict[str, Any]:
    """机群状态：在册能力 + 实时遥测。"""
    out = []
    for r in orch.robots:
        t = link.telemetry(r.robot_id)
        out.append({
            "robot_id": r.robot_id, "kind": r.kind, "model": r.model,
            "payload_kg": r.capability.payload_kg,
            "skills": list(r.capability.skills),
            "telemetry": None if t is None else {
                "x": t.x, "y": t.y, "battery_pct": t.battery_pct,
                "online": t.online},
        })
    return {"robots": out}


@get("/tg-api/v1/zones")
async def list_zones() -> dict[str, Any]:
    return {"zones": [{
        "zone_id": z.zone_id, "kind": z.kind.value,
        "rect": [z.x_min, z.y_min, z.x_max, z.y_max],
        "speed_limit": z.speed_limit,
    } for z in DEMO_ZONES]}


@post("/tg-api/v1/robots/{robot_id:str}/estop", status_code=HTTP_200_OK)
async def estop(robot_id: str) -> dict[str, Any]:
    ok = link.emergency_stop(robot_id)
    if not ok:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                            detail=f"机器人不存在: {robot_id}")
    return {"robot_id": robot_id, "stopped": True}


# ---------------------------------------------------------------- 作业单
@post("/tg-api/v1/work-orders", status_code=201)
async def submit_work_order(data: WorkOrderIn) -> dict[str, Any]:
    try:
        kind = WorkOrderKind(data.kind)
    except ValueError:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                            detail=f"未知作业单类型: {data.kind}")
    order_id = f"WO-{uuid.uuid4().hex[:8].upper()}"
    order = WorkOrder(order_id, kind, dict(data.params), data.priority,
                      data.requested_by, data.due)
    graph, rep = orch.submit(order)
    _orders[order_id] = {"graph": graph, "report": rep, "status": "PENDING"}
    return {"order_id": order_id, "trace_id": rep.trace_id,
            "tasks": [{"task_id": t.task_id, "kind": t.kind,
                       "depends_on": list(t.depends_on)} for t in graph.tasks.values()]}


@post("/tg-api/v1/work-orders/{order_id:str}/approve", status_code=HTTP_200_OK)
async def approve(order_id: str, data: ApproveIn) -> dict[str, Any]:
    rec = _get(order_id)
    if rec["status"] != "PENDING":
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                            detail=f"当前状态 {rec['status']} 不可审批")
    rec["graph"] = orch.approve(rec["graph"], rec["report"], data.approved_by)
    rec["status"] = "APPROVED"
    return {"order_id": order_id, "status": "APPROVED", "by": data.approved_by}


@post("/tg-api/v1/work-orders/{order_id:str}/run", status_code=HTTP_200_OK)
async def run(order_id: str, data: RunIn) -> dict[str, Any]:
    rec = _get(order_id)
    if rec["status"] not in {"APPROVED", "RUNNING"}:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                            detail=f"当前状态 {rec['status']} 不可运行（需先审批）")
    rec["status"] = "RUNNING"
    rep = orch.run(rec["graph"], rec["report"], person_in_stop_zone=data.person_in_stop_zone)
    rec["status"] = "DONE" if not rep.aborted else "ABORTED"
    return rep.to_dict()


@get("/tg-api/v1/work-orders/{order_id:str}")
async def get_order(order_id: str) -> dict[str, Any]:
    rec = _get(order_id)
    return {"order_id": order_id, "status": rec["status"],
            "report": rec["report"].to_dict()}


@get("/tg-api/v1/work-orders")
async def list_orders() -> dict[str, Any]:
    return {"orders": [{"order_id": k, "status": v["status"]}
                       for k, v in _orders.items()]}


# ---------------------------------------------------------------- 技能治理
@post("/tg-api/v1/skills/{skill_id:str}/versions", status_code=201)
async def publish_skill(skill_id: str, data: SkillPublishIn) -> dict[str, Any]:
    try:
        v = skills.publish(skill_id, data.version, data.entry, data.canary_pct)
    except Exception as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e))
    return {"skill_id": skill_id, "version": v.version, "state": v.state}


@post("/tg-api/v1/skills/{skill_id:str}/versions/{version:str}/approve", status_code=HTTP_200_OK)
async def approve_skill(skill_id: str, version: str,
                        data: SkillApproveIn) -> dict[str, Any]:
    try:
        v = skills.approve(skill_id, version, data.approved_by, data.canary_pct)
    except Exception as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e))
    return {"skill_id": skill_id, "version": v.version, "state": v.state,
            "approved_by": v.approved_by}


@post("/tg-api/v1/skills/{skill_id:str}/rollback", status_code=HTTP_200_OK)
async def rollback_skill(skill_id: str, to_version: str = Parameter(query="to")) -> dict[str, Any]:
    try:
        v = skills.rollback(skill_id, to_version)
    except Exception as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e))
    return {"skill_id": skill_id, "active": v.version, "state": v.state}


@get("/tg-api/v1/skills")
async def list_skills() -> dict[str, Any]:
    return {"skills": [{
        "skill_id": s.skill_id,
        "active": (s.active_version().version if s.active_version() else None),
        "versions": [{"version": v.version, "state": v.state,
                      "canary_pct": v.canary_pct} for v in s.versions],
    } for s in skills.all()]}


# ---------------------------------------------------------------- 人工令牌
@post("/tg-api/v1/tokens", status_code=201)
async def issue_token(data: TokenIn) -> dict[str, Any]:
    """人工签发确认令牌（真人审批流的机器入口；模型不得调用本接口）。"""
    orch.human_tokens[data.scope] = data.token
    return {"scope": data.scope, "issued": True, "signed_by": data.signed_by}


# ---------------------------------------------------------------- 数据飞轮
@get("/tg-api/v1/episodes/stats")
async def episode_stats() -> dict[str, Any]:
    rec = orch.recorder
    skill_ids = sorted({e.skill_id for eps in rec.shards().values()
                        for e in eps})
    return {
        "total": len(rec),
        "success_rate": {sid: rec.success_rate(sid) for sid in skill_ids},
        "shards": {k: len(v) for k, v in rec.shards().items()},
    }


def _get(order_id: str) -> dict[str, Any]:
    rec = _orders.get(order_id)
    if rec is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND,
                            detail=f"作业单不存在: {order_id}")
    return rec


app = Litestar(route_handlers=[
    healthz, version,
    list_robots, list_zones, estop,
    submit_work_order, approve, run, get_order, list_orders,
    publish_skill, approve_skill, rollback_skill, list_skills,
    issue_token, episode_stats,
    mcp_endpoint,
])
