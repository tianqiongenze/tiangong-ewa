"""演示闭环：无真机跑通「下单 → 分解 → 审批 → 调度 → 执行 → 证据 → 数据飞轮」。

用法：
    python tools/demo_run.py            # 正常搬运
    python tools/demo_run.py --chaos    # 注入失败，看降级阶梯
    python tools/demo_run.py --person   # 停止区有人，看 R4 停机
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "tiangong_core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "agent-runtime"))

from app.orchestrator import Orchestrator  # noqa: E402
from app.robots import MockRobotDriver  # noqa: E402
from app.seed import DEMO_ROBOTS, DEMO_ZONES, seed_skills  # noqa: E402
from tiangong_core import WorkOrder, WorkOrderKind  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chaos", action="store_true", help="注入机器人失败")
    ap.add_argument("--person", action="store_true", help="停止区有人")
    args = ap.parse_args()

    link = MockRobotDriver.demo_fleet()
    if args.chaos:
        for r in link.robots.values():
            r.fail_next = 99
    orch = Orchestrator(link=link, robots=DEMO_ROBOTS, zones=DEMO_ZONES,
                        skills=seed_skills())

    order = WorkOrder(
        "WO-DEMO-01", WorkOrderKind.TRANSPORT,
        {"from": "原料库", "to": "装配车间工位A", "what": "电池包",
         "weight_kg": 35, "x": 25.0, "y": 14.0},
        priority=3, requested_by="设备科-老周")

    print(f"① 下单：{order.order_id} {order.kind.value} {order.params['what']}"
          f"（{order.params['weight_kg']}kg → ({order.params['x']}, {order.params['y']})）")
    g, rep = orch.submit(order)
    print(f"② 分解：{len(g.tasks)} 个任务，trace={rep.trace_id}")
    for tid in g.topological_order():
        t = g.tasks[tid]
        print(f"   - {tid} {t.kind} depends={list(t.depends_on)}")

    g = orch.approve(g, rep, "设备科-老周")
    print("③ 审批：PENDING → APPROVED（人：设备科-老周）")

    rep = orch.run(g, rep, person_in_stop_zone=args.person)
    print(f"④ 执行：aborted={rep.aborted} degraded={rep.degraded} "
          f"level={rep.degrade_level}")
    for tid, t in rep.tasks.items():
        print(f"   - {tid}: {t.status.value} → {t.assigned_to} "
              f"attempts={t.attempts} note={t.note or '-'}")
        for ev in t.evidence.items:
            print(f"       证据 {ev.kind}: {ev.ref}")

    print(f"⑤ 数据飞轮：{len(orch.recorder)} 条 episode，"
          f"分片={ {k: len(v) for k, v in orch.recorder.shards().items()} }")
    print("\n完整报告 JSON：")
    print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2)[:1500])


if __name__ == "__main__":
    main()
