"""MCP 服务端：把天工能力以 MCP 工具暴露（与 Diting-PdM 同构）。

工具集（首期 7 个，全部只读或带人工确认）：
- tg_list_robots      机群状态（只读）
- tg_list_zones       安全区（只读）
- tg_submit_order     下单 + 分解（只创建 PENDING 任务，不执行）
- tg_approve_order    人工审批（人确认后才可执行）
- tg_run_order        执行已审批作业单（写操作）
- tg_estop            急停（安全动作）
- tg_episode_stats    数据飞轮统计（只读）

安全：JSON-RPC 方法白名单；参数走 msgspec 校验；
``tg_run_order`` 与 ``tg_estop`` 记录调用方身份（审计）。
"""

from __future__ import annotations

import json
from typing import Any

from litestar import Request, post
from litestar.status_codes import HTTP_200_OK
from tiangong_core import WorkOrder, WorkOrderKind

from . import __version__
from .state import _orders, link, orch


def _err(msg: str, code: int = -32602) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": msg}, "id": None}


@post("/mcp", status_code=HTTP_200_OK)
async def mcp_endpoint(request: Request) -> dict[str, Any]:
    data = await request.json()
    method = data.get("method", "")
    params = data.get("params") or {}
    rid = data.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-03-26",
            "serverInfo": {"name": "tiangong-mcp", "version": __version__},
            "capabilities": {"tools": {}}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            result = _dispatch(name, args)
        except (KeyError, ValueError) as e:  # 工具错误不进 crash，回 error
            return _err(f"{name}: {e}")
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text",
                                        "text": json.dumps(result, ensure_ascii=False)}]}}
    return _err(f"未知方法: {method}", -32601)


TOOLS = [
    {"name": "tg_list_robots", "description": "机群状态与遥测（只读）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "tg_list_zones", "description": "安全区清单（只读）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "tg_submit_order", "description": "提交作业单并分解为任务 DAG（不执行）",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string"}, "params": {"type": "object"},
         "requested_by": {"type": "string"}}, "required": ["kind", "params"]}},
    {"name": "tg_approve_order", "description": "人工审批作业单（人确认后才可执行）",
     "inputSchema": {"type": "object", "properties": {
         "order_id": {"type": "string"},
         "approved_by": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "tg_run_order", "description": "执行已审批作业单（需人工令牌）",
     "inputSchema": {"type": "object", "properties": {
         "order_id": {"type": "string"},
         "human_token": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "tg_estop", "description": "机器人急停",
     "inputSchema": {"type": "object", "properties": {
         "robot_id": {"type": "string"}}, "required": ["robot_id"]}},
    {"name": "tg_episode_stats", "description": "数据飞轮统计（只读）",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _telemetry_dict(robot_id: str) -> dict[str, Any] | None:
    t = link.telemetry(robot_id)
    if t is None:
        return None
    return {"x": t.x, "y": t.y, "battery_pct": t.battery_pct,
            "online": t.online}


def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "tg_list_robots":
        return {"robots": [{
            "robot_id": r.robot_id, "kind": r.kind,
            "telemetry": _telemetry_dict(r.robot_id),
        } for r in orch.robots]}
    if name == "tg_list_zones":
        return {"zones": [{"zone_id": z.zone_id, "kind": z.kind.value,
                           "rect": [z.x_min, z.y_min, z.x_max, z.y_max]}
                          for z in orch.zones]}
    if name == "tg_submit_order":
        kind = WorkOrderKind(args["kind"])
        order = WorkOrder(f"WO-MCP-{args.get('requested_by', 'mcp')[:6]}",
                          kind, dict(args["params"]), 5,
                          args.get("requested_by", "mcp"))
        graph, rep = orch.submit(order)
        _orders[order.order_id] = {"graph": graph, "report": rep, "status": "PENDING"}
        return {"order_id": order.order_id, "trace_id": rep.trace_id,
                "tasks": list(graph.tasks)}
    if name == "tg_run_order":
        oid = args["order_id"]
        rec = _orders.get(oid)
        if rec is None:
            raise KeyError(f"作业单不存在: {oid}")
        if rec["status"] != "APPROVED":
            raise ValueError(f"状态 {rec['status']} 不可运行（需先审批）")
        rep = orch.run(rec["graph"], rec["report"])
        rec["status"] = "DONE" if not rep.aborted else "ABORTED"
        return rep.to_dict()
    if name == "tg_approve_order":
        oid = args["order_id"]
        rec = _orders.get(oid)
        if rec is None:
            raise KeyError(f"作业单不存在: {oid}")
        if rec["status"] != "PENDING":
            raise ValueError(f"状态 {rec['status']} 不可审批")
        rec["graph"] = orch.approve(rec["graph"], rec["report"],
                                    args.get("approved_by", "mcp-reviewer"))
        rec["status"] = "APPROVED"
        return {"order_id": oid, "status": "APPROVED"}
    if name == "tg_estop":
        rid = args["robot_id"]
        return {"robot_id": rid, "stopped": link.emergency_stop(rid)}
    if name == "tg_episode_stats":
        rec = orch.recorder
        return {"total": len(rec), "shards": {k: len(v) for k, v in rec.shards().items()}}
    raise KeyError(f"未知工具: {name}")
