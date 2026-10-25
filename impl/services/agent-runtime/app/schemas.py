"""API 数据契约（msgspec）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgspec


class WorkOrderIn(msgspec.Struct):
    kind: str                       # TRANSPORT / INSPECT / LOAD_UNLOAD / CLEAN / CHARGE
    params: Mapping[str, Any]
    priority: int = 5
    requested_by: str = "anonymous"
    due: str = ""


class ApproveIn(msgspec.Struct):
    approved_by: str = "reviewer"


class RunIn(msgspec.Struct):
    person_in_stop_zone: bool = False


class SkillPublishIn(msgspec.Struct):
    version: str
    entry: str
    canary_pct: int = 0


class SkillApproveIn(msgspec.Struct):
    approved_by: str
    canary_pct: int | None = None


class TokenIn(msgspec.Struct):
    scope: str                      # task_id 或 order_id
    token: str
    signed_by: str = ""


class ErrorOut(msgspec.Struct):
    error: str
    detail: str = ""


class A2ADelegateIn(msgspec.Struct):
    intent: str
    params: Mapping[str, Any]
    source_agent: str = "unknown"
