"""天工 Locust 压测场景（与 k6 同口径，便于交叉验证）。

配比（按任务权重）：
- 6 : 分析聚合读（命中多级缓存）——看板首屏的主力
- 4 : 机群/作业单/技能/指标读
- 2 : 下单 → 审批 → 执行（写链路，含安全审查与证据落库）

运行（无头，产出 CSV）：
    locust -f tests/perf/locustfile.py --headless -u 30 -r 5 -t 60s \
        --host http://127.0.0.1:8200 --csv tests/perf/results/locust --only-summary
"""

from __future__ import annotations

import random

from locust import HttpUser, between, task


class OperatorUser(HttpUser):
    """操作员视角：看板浏览为主，偶发下单。"""

    wait_time = between(0.2, 0.8)

    def on_start(self) -> None:
        r = self.client.get("/healthz", name="/healthz")
        if r.status_code != 200:
            raise RuntimeError("服务未就绪")

    # ------------------------------------------------------------ 读
    @task(6)
    def analytics_summary(self) -> None:
        days = random.choice([7, 7, 7, 30])
        with self.client.get(f"/tg-api/v1/analytics/summary?days={days}",
                             name="/analytics/summary?days=[7|30]",
                             catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status {r.status_code}")
            elif not r.json().get("available", False):
                r.failure("analytics unavailable")

    @task(4)
    def fleet_and_orders(self) -> None:
        self.client.get("/tg-api/v1/robots", name="/robots")
        self.client.get("/tg-api/v1/work-orders", name="/work-orders")
        self.client.get("/tg-api/v1/skills", name="/skills")
        self.client.get("/tg-api/v1/observability/metrics", name="/metrics")

    @task(1)
    def audit_and_adapters(self) -> None:
        self.client.get("/tg-api/v1/audit?limit=20", name="/audit")
        self.client.get("/tg-api/v1/adapters/health", name="/adapters/health")

    # ------------------------------------------------------------ 写
    @task(2)
    def order_chain(self) -> None:
        payload = {
            "kind": "TRANSPORT",
            "params": {"from": "原料库", "to": "装配车间", "what": "电池包",
                       "weight_kg": 35, "x": 25, "y": 14},
            "requested_by": "locust-operator",
        }
        with self.client.post("/tg-api/v1/work-orders", json=payload,
                              name="/work-orders [submit]",
                              catch_response=True) as r:
            if r.status_code != 201:
                r.failure(f"submit {r.status_code}")
                return
            oid = r.json().get("order_id")

        with self.client.post(f"/tg-api/v1/work-orders/{oid}/approve",
                              json={"approved_by": "locust-supervisor"},
                              name="/work-orders/{id}/approve",
                              catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"approve {r.status_code}")
                return

        with self.client.post(f"/tg-api/v1/work-orders/{oid}/run", json={},
                              name="/work-orders/{id}/run",
                              catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"run {r.status_code}")
            elif "trace_id" not in r.json():
                r.failure("missing trace_id")
