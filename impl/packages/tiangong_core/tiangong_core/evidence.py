"""证据链：结论必须挂证据，无证据不得完成（宪法第三条）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .models import Evidence, EvidenceItem, Task, TaskStatus

# 任务类型 → 必须的证据类型（缺一即不得判 DONE）
REQUIRED_EVIDENCE: Mapping[str, tuple[str, ...]] = {
    "PICK": ("telemetry", "skill_version"),
    "TRANSPORT": ("telemetry", "skill_version"),
    "PLACE": ("telemetry", "skill_version"),
    "SCAN": ("telemetry", "snapshot"),
    "PHOTO": ("snapshot",),
    "CLEAN": ("telemetry", "snapshot"),
    "APPROACH": ("telemetry",),
    "RETURN": ("telemetry",),
    "CHARGE": ("telemetry",),
}


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    missing: tuple[str, ...]
    trace_id: str


def verify_evidence(task: Task) -> VerifyResult:
    """校验任务证据完整性；不完整 → 不得 DONE。"""
    need = REQUIRED_EVIDENCE.get(task.kind, ("telemetry",))
    have = set(task.evidence.kinds())
    missing = tuple(k for k in need if k not in have)
    trace = next((i.ref for i in task.evidence.items if i.kind == "trace"), "")
    return VerifyResult(ok=not missing, missing=missing, trace_id=trace)


class EvidenceChain:
    """证据链构造器（追加式，不可篡改历史）。"""

    def __init__(self, trace_id: str) -> None:
        self._trace = EvidenceItem(kind="trace", ref=trace_id)

    def start(self) -> Evidence:
        return Evidence(items=(self._trace,))

    @staticmethod
    def telemetry(ref: str, detail: str = "") -> EvidenceItem:
        return EvidenceItem(kind="telemetry", ref=ref, detail=detail)

    @staticmethod
    def snapshot(ref: str, detail: str = "") -> EvidenceItem:
        return EvidenceItem(kind="snapshot", ref=ref, detail=detail)

    @staticmethod
    def skill_version(version: str) -> EvidenceItem:
        return EvidenceItem(kind="skill_version", ref=version)

    @staticmethod
    def model_version(version: str) -> EvidenceItem:
        return EvidenceItem(kind="model_version", ref=version)

    @staticmethod
    def human_token(token: str, signed_by: str = "") -> EvidenceItem:
        return EvidenceItem(kind="human_token", ref=token, detail=f"signed_by={signed_by}")


def attach(task: Task, *items: EvidenceItem) -> Task:
    """把证据挂到任务上（返回新任务，不改旧对象）。"""
    from dataclasses import replace
    return replace(task, evidence=Evidence(items=task.evidence.items + tuple(items)))
