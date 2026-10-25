"""可观测性：Session > Trace > Span 三层 + OTel GenAI 语义属性。

对标工业实践（OTel GenAI Semantic Conventions + AWS AgentCore 三级层级）：
- Session：一次业务交互的完整生命周期（一个作业单）
- Trace：单条推理/执行链（一个 trace_id）
- Span：工具调用/模型调用/安全审查（最小可观测单元）

属性命名对齐 gen_ai.* 语义约定（锁版本，experimental）：
  gen_ai.system / gen_ai.request.model / gen_ai.usage.input_tokens /
  gen_ai.usage.output_tokens / gen_ai.response.finish_reasons

本实现为零依赖结构化记录器：产出与 OTel 对齐的 span 字典，
可无缝对接真实 SDK（接 SDK 时只改 emit 出口，不动调用点）。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_id: str | None = None
    kind: str = "internal"           # internal / client / server
    start_at: float = field(default_factory=time.time)
    end_at: float | None = None
    status: str = "OK"               # OK / ERROR
    attributes: dict[str, Any] = field(default_factory=dict)

    def duration_ms(self) -> float:
        if self.end_at is None:
            return 0.0
        return round((self.end_at - self.start_at) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "trace_id": self.trace_id,
            "span_id": self.span_id, "parent_id": self.parent_id,
            "kind": self.kind, "duration_ms": self.duration_ms(),
            "status": self.status, "attributes": self.attributes,
        }


class Tracer:
    """三层观测器：Session > Trace > Span。"""

    def __init__(self, service_name: str = "agent-runtime") -> None:
        self.service = service_name
        self._spans: list[Span] = []
        self._current_trace: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ Session
    @contextmanager
    def session(self, name: str) -> Iterator[str]:
        """一个 Session（业务交互），产出 trace_id。"""
        trace_id = f"TR-{uuid.uuid4().hex[:12]}"
        prev = self._current_trace
        self._current_trace = trace_id
        try:
            yield trace_id
        finally:
            self._current_trace = prev

    # -------------------------------------------------------------- Trace
    @property
    def trace_id(self) -> str:
        return self._current_trace or f"TR-{uuid.uuid4().hex[:12]}"

    # -------------------------------------------------------------- Span
    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        """开一个 span；异常自动记 ERROR。"""
        sp = Span(name=name, trace_id=self.trace_id, attributes=dict(attrs))
        try:
            yield sp
        except Exception:
            sp.status = "ERROR"
            raise
        finally:
            sp.end_at = time.time()
            with self._lock:
                self._spans.append(sp)

    # ------------------------------------------------------------ 查询
    def spans(self, trace_id: str | None = None) -> list[Span]:
        with self._lock:
            if trace_id is None:
                return list(self._spans)
            return [s for s in self._spans if s.trace_id == trace_id]

    def export(self, trace_id: str | None = None) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.spans(trace_id)]

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            total = len(self._spans)
            err = sum(1 for s in self._spans if s.status == "ERROR")
            durs = [s.duration_ms() for s in self._spans if s.end_at]
            p95 = sorted(durs)[int(len(durs) * 0.95)] if durs else 0.0
            return {"spans_total": total, "spans_error": err,
                    "p95_ms": p95, "service": self.service}


# 全局单例（服务装配用）
tracer = Tracer()


def gen_ai_attrs(model: str, system: str = "vla",
                 input_tokens: int = 0, output_tokens: int = 0,
                 finish_reasons: str = "stop") -> dict[str, Any]:
    """GenAI 语义属性构造器（对齐 OTel SemConv）。"""
    return {
        "gen_ai.system": system,
        "gen_ai.request.model": model,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.response.finish_reasons": finish_reasons,
    }
