"""事件总线（Event Bus）。

对标 Diting-PdM events.py：事件模型 + Transport 抽象 + 内存实现；
Pulsar/Redis transport 后置（接口已预留，接口不变即可热插拔）。

用途：
- 作业单状态变更广播（PENDING→APPROVED→…）
- 安全事件（停机/急停/红线违规）→ 审计与告警
- episode 落库后的数据飞轮触发

纪律：发布失败不得阻断主流程（fire-and-forget + 失败计数）；
事件不可变（append-only），带 trace_id 可回放。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Event:
    topic: str                     # work_order / task / safety / episode / skill
    type: str                      # created / approved / halted / estop / ...
    payload: dict[str, Any]
    trace_id: str = ""
    at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: f"EV-{uuid.uuid4().hex[:10]}")

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "topic": self.topic,
                "type": self.type, "payload": self.payload,
                "trace_id": self.trace_id, "at": self.at}


class Transport(Protocol):
    """传输抽象：内存 / Redis Streams / Pulsar 三态可换。"""

    def publish(self, event: Event) -> bool:  # pragma: no cover - 协议
        ...

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None:  # pragma: no cover
        ...


class MemoryTransport:
    """内存传输（演示/测试/单基地）。线程安全，按 topic 分发。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Event], None]]] = {}
        self._log: list[Event] = []
        self._lock = threading.Lock()
        self.published = 0
        self.failed = 0

    def publish(self, event: Event) -> bool:
        try:
            with self._lock:
                self._log.append(event)
                self.published += 1
                handlers = list(self._handlers.get(event.topic, []))
            for h in handlers:
                try:
                    h(event)
                except Exception:
                    self.failed += 1   # 单订阅者失败不影响其他订阅者
            return True
        except Exception:
            self.failed += 1
            return False

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None:
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)

    def history(self, topic: str | None = None) -> list[Event]:
        with self._lock:
            if topic is None:
                return list(self._log)
            return [e for e in self._log if e.topic == topic]


class EventBus:
    """门面：发布 + 订阅 + 统计。"""

    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or MemoryTransport()

    def publish(self, topic: str, type_: str, payload: dict[str, Any],
                trace_id: str = "") -> Event:
        ev = Event(topic=topic, type=type_, payload=payload, trace_id=trace_id)
        self.transport.publish(ev)
        return ev

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None:
        self.transport.subscribe(topic, handler)

    @property
    def stats(self) -> dict[str, int]:
        t = self.transport
        if isinstance(t, MemoryTransport):
            return {"published": t.published, "failed": t.failed,
                    "topics": len(t._handlers)}
        return {}
