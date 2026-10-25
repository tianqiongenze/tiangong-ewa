"""多品牌机器人适配器框架。

对标工业实践：
- **VDA 5050** 是跨品牌 AGV/AMR 的通用语言（MQTT + JSON 主题规范）；
  仙工智能 SRC 等已验证"自定义协议 + VDA 5050 双协议混场调度"
- ROS2 + VDA 5050 包为开源参照；Open-RMF / NVIDIA Isaac 为多机调度参照

本框架提供：
1. ``RobotAdapter`` 协议：capabilities / telemetry / execute / estop
2. ``VDA5050Adapter``：VDA 5050 MQTT 契约骨架（连接、order 下发、
   instantAction、state 解析、事实表 Factsheet 解析）——MQTT 客户端注入，
   不锁 paho 版本
3. ``AdapterRegistry``：注册 / 发现 / 健康探测 / 按能力选型

纪律：adapter 只做协议转换，**不做安全判定**（安全在 tiangong_core.safety）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from tiangong_core import Robot, RobotCapability


@dataclass(frozen=True)
class AdapterHealth:
    ok: bool
    detail: str = ""
    latency_ms: float = 0.0
    at: float = field(default_factory=time.time)


class RobotAdapter(Protocol):
    """机器人适配器协议（多品牌统一入口）。"""

    brand: str
    protocol: str                    # vda5050 / grpc / http / mock

    def discover(self) -> Sequence[Robot]:  # pragma: no cover - 协议
        ...

    def telemetry(self, robot_id: str) -> dict[str, Any] | None:  # pragma: no cover
        ...

    def execute(self, robot_id: str,
                action: Mapping[str, Any]) -> dict[str, Any]:  # pragma: no cover
        ...

    def estop(self, robot_id: str) -> bool:  # pragma: no cover
        ...

    def health(self) -> AdapterHealth:  # pragma: no cover
        ...


class VDA5050Adapter:
    """VDA 5050 MQTT 适配器骨架。

    主题规范（VDA 5050 v2.0）：
      发布：{interface}/{majorVersion}/{manufacturer}/{serialNumber}/order
            {interface}/{majorVersion}/{manufacturer}/{serialNumber}/instantActions
      订阅：{interface}/{majorVersion}/{manufacturer}/{serialNumber}/state
            {interface}/{majorVersion}/{manufacturer}/{serialNumber}/visualization
            {interface}/{majorVersion}/{manufacturer}/{serialNumber}/connection
    本类实现**契约与消息构造**，MQTT 客户端由外部注入（paho / gmqtt / 自研皆可），
    未注入时进入 dry-run 模式（记录待发消息，供联调）。
    """

    protocol = "vda5050"

    def __init__(self, manufacturer: str, interface: str = "uagv",
                 version: str = "v2", mqtt_client: Any | None = None,
                 serials: Sequence[str] = ()) -> None:
        self.manufacturer = manufacturer
        self.brand = manufacturer
        self.interface = interface
        self.version = version
        self.mqtt = mqtt_client
        self.serials = tuple(serials)
        self._outbox: list[tuple[str, str]] = []     # dry-run 待发（topic, payload）

    # ------------------------------------------------------------ 主题
    def topic(self, serial: str, channel: str) -> str:
        return f"{self.interface}/{self.version}/{self.manufacturer}/{serial}/{channel}"

    # ------------------------------------------------------------ 发现
    def discover(self) -> Sequence[Robot]:
        """从 Factsheet/connection 主题已缓存的序列号构造在册机器人。"""
        return tuple(
            Robot(robot_id=s, kind="AMR", model=f"{self.manufacturer}/{self.interface}",
                  capability=RobotCapability(payload_kg=0, max_speed=0))
            for s in self.serials)

    # ------------------------------------------------------------ 遥测
    def telemetry(self, robot_id: str) -> dict[str, Any] | None:
        # 生产：从 state 主题缓存读；骨架返回 None（联调期由 MockRobotDriver 顶替）
        return None

    # ------------------------------------------------------------ 执行
    def build_order(self, robot_id: str, order_id: str, release: bool = True,
                    nodes: Sequence[Mapping[str, Any]] = (),
                    edges: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """构造 VDA 5050 Order 消息。"""
        return {
            "orderId": order_id,
            "orderUpdateId": 0,
            "released": release,
            "nodes": [dict(n) for n in nodes],
            "edges": [dict(e) for e in edges],
        }

    def build_instant_action(self, action_type: str, **kwargs: Any) -> dict[str, Any]:
        """构造 InstantAction（cancelOrder / factsheetRequest / 急停等）。"""
        return {"actionType": action_type, **kwargs}

    def execute(self, robot_id: str, action: Mapping[str, Any]) -> dict[str, Any]:
        """把平台动作翻译为 VDA 5050 消息下发。"""
        kind = str(action.get("kind", "MOVE"))
        act_type = "pickup" if kind in {"PICK", "PLACE"} else "move"
        if kind in {"MOVE", "PICK", "PLACE", "TRANSPORT", "APPROACH", "RETURN"}:
            msg = self.build_order(
                robot_id, order_id=str(action.get("order_id", f"O-{robot_id}")),
                nodes=[{"nodeId": "target", "x": float(action.get("x", 0.0)),
                        "y": float(action.get("y", 0.0)),
                        "actions": [{"actionType": act_type,
                                     "blockingType": "HARD"}]}])
            topic = self.topic(robot_id, "order")
        else:
            msg = self.build_instant_action("cancelOrder")
            topic = self.topic(robot_id, "instantActions")
        return self._publish(topic, msg)

    def estop(self, robot_id: str) -> bool:
        """VDA 5050 急停：下发 cancelOrder + 平台侧记录。"""
        self._publish(self.topic(robot_id, "instantActions"),
                      self.build_instant_action("cancelOrder"))
        return True

    def parse_state(self, payload: str) -> dict[str, Any]:
        """解析 state 主题消息（电池/位置/错误/动作状态）。"""
        try:
            d = json.loads(payload)
        except (ValueError, TypeError):
            return {}
        return {
            "robot_id": d.get("serialNumber", d.get("agvId", "")),
            "x": float(d.get("x", 0.0) or 0.0),
            "y": float(d.get("y", 0.0) or 0.0),
            "battery_pct": float((d.get("batteryState") or {}).get("batteryCharge", 0)),
            "driving": bool(d.get("driving", False)),
            "errors": list(d.get("errors") or []),
        }

    # ------------------------------------------------------------ 健康
    def health(self) -> AdapterHealth:
        if self.mqtt is None:
            return AdapterHealth(False, "dry-run（未注入 MQTT 客户端）")
        try:
            self.mqtt.ping()
            return AdapterHealth(True, "mqtt ok")
        except Exception as e:
            return AdapterHealth(False, f"mqtt: {e}")

    # ------------------------------------------------------------ 内部
    def _publish(self, topic: str, msg: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(msg, ensure_ascii=False)
        if self.mqtt is None:
            self._outbox.append((topic, payload))
            return {"ok": True, "dry_run": True, "topic": topic}
        try:
            self.mqtt.publish(topic, payload)
            return {"ok": True, "dry_run": False, "topic": topic}
        except Exception as e:
            return {"ok": False, "error": str(e), "topic": topic}

    def drain_outbox(self) -> list[tuple[str, str]]:
        """取出并清空 dry-run 待发队列（联调/测试用）。"""
        out, self._outbox = self._outbox, []
        return out


class AdapterRegistry:
    """适配器注册表：多品牌接入的唯一入口。"""

    def __init__(self) -> None:
        self._adapters: dict[str, RobotAdapter] = {}

    def register(self, adapter: RobotAdapter) -> None:
        self._adapters[adapter.brand] = adapter

    def get(self, brand: str) -> RobotAdapter | None:
        return self._adapters.get(brand)

    def all(self) -> Mapping[str, RobotAdapter]:
        return dict(self._adapters)

    def discover_all(self) -> list[Robot]:
        robots: list[Robot] = []
        for a in self._adapters.values():
            try:
                robots.extend(a.discover())
            except Exception:
                continue    # 单品牌发现失败不影响其他品牌（降级纪律）
        return robots

    def health_all(self) -> dict[str, AdapterHealth]:
        out = {}
        for brand, a in self._adapters.items():
            try:
                out[brand] = a.health()
            except Exception as e:
                out[brand] = AdapterHealth(False, str(e))
        return out

    def find_capable(self, predicate: Callable[[Robot], bool]) -> list[Robot]:
        return [r for r in self.discover_all() if predicate(r)]
