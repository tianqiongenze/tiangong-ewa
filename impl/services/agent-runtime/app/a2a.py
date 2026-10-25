"""A2A（Agent-to-Agent）跨域协作。

对标 Diting-PdM Phase E（A2A 智能体互联）：
- **agent card**：能力声明（本平台能接什么作业单、暴露什么技能）
- **任务委派**：外部智能体（如谛听 Diting-PdM 的诊断智能体）把意图委派过来，
  本平台分解为机器人作业单——"HI 劣化告警 → 自动巡检作业单"的联动通道

协议形态：REST JSON（/a2a/v1），与 Google A2A 概念对齐（card + task）。
"""

from __future__ import annotations

import uuid
from typing import Any

from tiangong_core import WorkOrder, WorkOrderKind

from .state import _orders, orch

AGENT_CARD = {
    "name": "tiangong-ewa",
    "description": "具身智能工厂作业智能体平台：搬运/巡检/上下料/清洁/回充",
    "version": "0.1.0",
    "capabilities": {
        "work_order_kinds": [k.value for k in WorkOrderKind],
        "skills": ["pick_place", "transport", "scan"],
        "safety": ["geofence", "red_lines", "human_token", "estop"],
        "protocols": ["rest", "mcp", "grpc(vda5050)"],
    },
    "endpoints": {"tasks": "/a2a/v1/tasks", "card": "/a2a/v1/card"},
}


def delegate(intent: str, params: dict[str, Any],
             source_agent: str = "unknown") -> dict[str, Any]:
    """接收外部智能体意图，转作业单（PENDING，需审批）。

    intent ∈ WorkOrderKind；未知意图返回可承接清单（拒答要可读）。
    """
    try:
        kind = WorkOrderKind(intent)
    except ValueError:
        return {"ok": False,
                "error": f"不支持的意图: {intent}",
                "supported": AGENT_CARD["capabilities"]["work_order_kinds"]}
    order_id = f"WO-A2A-{uuid.uuid4().hex[:8].upper()}"
    order = WorkOrder(order_id, kind, dict(params), 5,
                      requested_by=f"agent:{source_agent}")
    graph, rep = orch.submit(order)
    _orders[order_id] = {"graph": graph, "report": rep, "status": "PENDING"}
    return {"ok": True, "order_id": order_id, "trace_id": rep.trace_id,
            "tasks": list(graph.tasks), "source_agent": source_agent}
