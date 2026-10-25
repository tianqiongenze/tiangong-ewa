"""任务状态机（九态）。

合法迁移白名单——不在表里的迁移一律 ``IllegalTransition``。
与 Diting-PdM 技能四态同构，新增 VERIFYING（证据校验）与 ASSIGNED（机群分配）。
"""

from __future__ import annotations

from .models import Task, TaskStatus


class IllegalTransition(ValueError):
    """非法状态迁移。"""


_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.APPROVED, TaskStatus.ABORTED,
                                   TaskStatus.SUPERSEDED}),
    TaskStatus.APPROVED: frozenset({TaskStatus.ASSIGNED, TaskStatus.ABORTED,
                                    TaskStatus.SUPERSEDED}),
    TaskStatus.ASSIGNED: frozenset({TaskStatus.EXECUTING, TaskStatus.ABORTED,
                                    TaskStatus.SUPERSEDED, TaskStatus.FAILED}),
    TaskStatus.EXECUTING: frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED,
                                     TaskStatus.ABORTED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.DONE, TaskStatus.FAILED,
                                     TaskStatus.EXECUTING}),   # 证据不全打回重做
    TaskStatus.FAILED: frozenset({TaskStatus.APPROVED}),        # 重试走重新审批
    TaskStatus.ABORTED: frozenset(),
    TaskStatus.DONE: frozenset(),
    TaskStatus.SUPERSEDED: frozenset(),
}


def allowed_next(status: TaskStatus) -> frozenset[TaskStatus]:
    return _TRANSITIONS[status]


def transition(task: Task, to: TaskStatus, **kw: object) -> Task:
    """执行迁移；非法则抛 ``IllegalTransition``（带 trace 线索）。"""
    cur = task.status
    if to not in _TRANSITIONS[cur]:
        raise IllegalTransition(
            f"任务 {task.task_id}: {cur.value} → {to.value} 非法"
            f"（允许: {sorted(s.value for s in _TRANSITIONS[cur])}）")
    from dataclasses import replace
    return replace(task, status=to, **kw)
