"""任务分解：作业单 → 任务 DAG。

分解策略（Plan 接口 + 规则兜底，与 Diting-PdM"可解释优先"一致）：
- ``RuleBasedPlanner``：模板化分解，确定性、可解释、可单测（默认）
- ``LLMPlanner``：留给大模型规划器的接口（生产环境），输出同样收敛到 DAG

纪律：分解结果必须是**有向无环图**；出现环 = 规划缺陷，直接拒绝
（宪法第一条：规格是唯一真相源——坏计划不得进入执行）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

from .models import Task, TaskStatus, WorkOrder, WorkOrderKind


class PlanError(ValueError):
    """分解失败（含产生环）。"""


class Planner(Protocol):
    """规划器协议：作业单 → 任务列表（依赖关系隐含在 depends_on 中）。"""

    def plan(self, order: WorkOrder) -> Sequence[Task]:  # pragma: no cover - 协议
        ...


def _mk(order: WorkOrder, tid: str, kind: str, params: Mapping[str, Any],
        deps: tuple[str, ...] = ()) -> Task:
    return Task(task_id=tid, work_order_id=order.order_id, kind=kind,
                params=dict(params), depends_on=deps)


@dataclass(frozen=True)
class RuleBasedPlanner:
    """规则规划器：五类作业单的模板分解（可解释兜底）。"""

    def plan(self, order: WorkOrder) -> Sequence[Task]:
        k = order.kind
        p = order.params
        oid = order.order_id
        if k is WorkOrderKind.TRANSPORT:
            return self._transport(oid, p)
        if k is WorkOrderKind.INSPECT:
            return self._inspect(oid, p)
        if k is WorkOrderKind.LOAD_UNLOAD:
            return self._load_unload(oid, p)
        if k is WorkOrderKind.CLEAN:
            return self._clean(oid, p)
        if k is WorkOrderKind.CHARGE:
            return self._charge(oid, p)
        raise PlanError(f"未知作业单类型: {k}")

    # -- 各模板 -----------------------------------------------------------
    @staticmethod
    def _transport(oid: str, p: Mapping[str, Any]) -> Sequence[Task]:
        src, dst = p["from"], p["to"]
        # 目标点坐标随作业单下发到末位任务（安全审查的对象）
        dst_xy = {}
        if "x" in p and "y" in p:
            dst_xy = {"x": float(p["x"]), "y": float(p["y"])}
        # 物料属性（重量）传播到所有搬运动作（安全审查与能力匹配的依据）
        w = {"weight_kg": float(p["weight_kg"])} if "weight_kg" in p else {}
        what = p.get("what", "料箱")
        return [
            _mk_task(oid, f"{oid}-T1", "PICK", {"at": src, "what": what, **w}),
            _mk_task(oid, f"{oid}-T2", "TRANSPORT",
                     {"from": src, "to": dst, "what": what, **w},
                     deps=(f"{oid}-T1",)),
            _mk_task(oid, f"{oid}-T3", "PLACE",
                     {"at": dst, "what": what, **w, **dst_xy},
                     deps=(f"{oid}-T2",)),
        ]

    @staticmethod
    def _inspect(oid: str, p: Mapping[str, Any]) -> Sequence[Task]:
        points: Iterable[Mapping[str, Any]] = p.get("points", [])
        tasks: list[Task] = []
        prev: Optional[str] = None
        for i, pt in enumerate(points, 1):
            tid = f"{oid}-T{i}"
            tasks.append(_mk_task(oid, tid, "SCAN",
                                  {"point": pt.get("name", f"点{i}"),
                                   "x": pt.get("x", 0.0), "y": pt.get("y", 0.0),
                                   "readings": pt.get("readings", ("温度", "异响"))},
                                  deps=(prev,) if prev else ()))
            prev = tid
        if p.get("photo_required", True):
            tasks.append(_mk_task(oid, f"{oid}-T{len(points) + 1}", "PHOTO",
                                  {"target": p.get("asset", ""), "x": 0.0, "y": 0.0},
                                  deps=(prev,) if prev else ()))
        return tasks

    @staticmethod
    def _load_unload(oid: str, p: Mapping[str, Any]) -> Sequence[Task]:
        station, slot = p["station"], p.get("slot", "A")
        return [
            _mk_task(oid, f"{oid}-T1", "APPROACH",
                     {"station": station, "slot": slot}),
            _mk_task(oid, f"{oid}-T2", "PICK",
                     {"station": station, "slot": slot, "what": p.get("what", "工件")},
                     deps=(f"{oid}-T1",)),
            _mk_task(oid, f"{oid}-T3", "PLACE",
                     {"station": p["machine"], "what": p.get("what", "工件")},
                     deps=(f"{oid}-T2",)),
        ]

    @staticmethod
    def _clean(oid: str, p: Mapping[str, Any]) -> Sequence[Task]:
        area = p["area"]
        return [
            _mk_task(oid, f"{oid}-T1", "SCAN",
                     {"point": f"{area}入口", "x": p.get("x", 0.0), "y": p.get("y", 0.0),
                      "readings": ("垃圾分布",)}),
            _mk_task(oid, f"{oid}-T2", "CLEAN", {"area": area}, deps=(f"{oid}-T1",)),
            _mk_task(oid, f"{oid}-T3", "PHOTO", {"target": area, "x": 0.0, "y": 0.0},
                     deps=(f"{oid}-T2",)),
        ]

    @staticmethod
    def _charge(oid: str, p: Mapping[str, Any]) -> Sequence[Task]:
        return [
            _mk_task(oid, f"{oid}-T1", "RETURN", {"to": p.get("dock", "充电桩")}),
            _mk_task(oid, f"{oid}-T2", "CHARGE", {"dock": p.get("dock", "充电桩")},
                     deps=(f"{oid}-T1",)),
        ]


def _mk_task(oid: str, tid: str, kind: str, params: Mapping[str, Any],
             deps: tuple[str, ...] = ()) -> Task:
    return Task(task_id=tid, work_order_id=oid, kind=kind,
                params=dict(params), depends_on=deps)


@dataclass(frozen=True)
class TaskGraph:
    """任务 DAG：节点 + 依赖，提供拓扑序与就绪集查询。"""

    tasks: Mapping[str, Task]

    def __post_init__(self) -> None:
        # 依赖必须存在
        for t in self.tasks.values():
            for d in t.depends_on:
                if d not in self.tasks:
                    raise PlanError(f"任务 {t.task_id} 依赖不存在的节点 {d}")
        # 无环校验（Kahn）
        self.topological_order()

    def topological_order(self) -> tuple[str, ...]:
        indeg = {tid: len(t.depends_on) for tid, t in self.tasks.items()}
        succ: dict[str, list[str]] = {tid: [] for tid in self.tasks}
        for t in self.tasks.values():
            for d in t.depends_on:
                succ[d].append(t.task_id)
        ready = sorted([tid for tid, d in indeg.items() if d == 0])
        order: list[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for s in succ[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    ready.append(s)
            ready.sort()
        if len(order) != len(self.tasks):
            raise PlanError("任务图存在环，规划拒绝")
        return tuple(order)

    def ready(self, done: set[str]) -> list[str]:
        """依赖已全部完成、自身未完成的任务（可并行派发）。"""
        return [tid for tid, t in self.tasks.items()
                if tid not in done and t.status not in _TERMINAL
                and all(d in done for d in t.depends_on)]

    def roots(self) -> list[str]:
        return [tid for tid, t in self.tasks.items() if not t.depends_on]


_TERMINAL = frozenset({TaskStatus.DONE, TaskStatus.ABORTED, TaskStatus.SUPERSEDED})


def decompose(order: WorkOrder, planner: Optional[Planner] = None) -> TaskGraph:
    """默认入口：规则规划器分解 + DAG 校验。"""
    p = planner or RuleBasedPlanner()
    tasks = p.plan(order)
    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        raise PlanError(f"任务 ID 重复: {ids}")
    return TaskGraph(tasks={t.task_id: t for t in tasks})


def topological_order(graph: TaskGraph) -> tuple[str, ...]:
    return graph.topological_order()
