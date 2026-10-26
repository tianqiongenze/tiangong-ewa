"""天工看板全链路 E2E（真实浏览器 + 真实服务进程）。

覆盖「全端全功能」：
1. 首屏实时渲染（数据源徽标、KPI、机群、安全区地图、技能、适配器）
2. KPI 与 API 一致（防"看板好看但对不上账"）
3. 下单 → 审批 → 执行 全链路（含证据计数）
4. 红线 R1（禁入区目标 → ABORTED）
5. 红线 R2（超载无令牌 → ABORTED）
6. 移动端响应式（无横向滚动）
7. 接口不可达 → 演示兜底（可用性降级）
8. 截图留证（报告附件）

运行：make e2e   （或 pytest tests/e2e -m e2e）
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

READY = "body[data-ready='1']"


def _api(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _open(pg, base: str) -> None:
    pg.goto(base + "/", wait_until="domcontentloaded")
    pg.wait_for_selector(READY, timeout=20000)


class TestDashboardRender:
    def test_live_badge_and_no_console_error(self, page):
        base = page._tg_base
        _open(page, base)
        expect(page.get_by_test_id("source-badge")).to_have_text("数据源：实时 API")
        assert page._tg_errors == [], f"控制台错误: {page._tg_errors}"

    def test_robots_table_matches_api(self, page):
        base = page._tg_base
        _open(page, base)
        api_robots = _api(base, "/tg-api/v1/robots")["robots"]
        rows = page.locator("#robots-body tr")
        expect(rows).to_have_count(len(api_robots))
        expect(page.get_by_test_id("kpi-robots")).to_have_text(str(len(api_robots)))

    def test_zone_map_renders_all_zones_and_robots(self, page):
        base = page._tg_base
        _open(page, base)
        zones = _api(base, "/tg-api/v1/zones")["zones"]
        robots = _api(base, "/tg-api/v1/robots")["robots"]
        expect(page.locator("#zones-layer rect")).to_have_count(len(zones))
        expect(page.locator("#robots-layer circle")).to_have_count(len(robots))

    def test_skills_and_adapters(self, page):
        base = page._tg_base
        _open(page, base)
        skills = _api(base, "/tg-api/v1/skills")["skills"]
        adapters = _api(base, "/tg-api/v1/adapters/health")["adapters"]
        expect(page.locator("#skills-body tr")).to_have_count(len(skills))
        expect(page.locator("#adapters-body tr")).to_have_count(len(adapters))

    def test_kpi_consistent_with_api(self, page):
        base = page._tg_base
        _open(page, base)
        summary = _api(base, "/tg-api/v1/analytics/summary?days=7")
        scale = _api(base, "/tg-api/v1/analytics/scale")
        assert summary["available"] and scale["available"]
        expect(page.get_by_test_id("an-rows")).to_have_text(f"{scale['total_rows']:,}")
        assert page.locator("#an-orders").inner_text().replace(",", "") == \
            str(summary["orders"]["total"])
        # 数据规模断言：小样本 E2E 库 20 万行
        assert scale["total_rows"] == 200_000

    def test_metrics_card_populated(self, page):
        base = page._tg_base
        _open(page, base)
        assert "ms" in page.get_by_test_id("m-p95").inner_text()
        assert "%" in page.get_by_test_id("m-hit").inner_text()


class TestOrderFlow:
    def test_submit_approve_run_done(self, page):
        base = page._tg_base
        _open(page, base)
        page.get_by_test_id("kind-select").select_option("TRANSPORT")
        page.get_by_test_id("input-weight").fill("35")
        page.get_by_test_id("input-x").fill("25")
        page.get_by_test_id("input-y").fill("14")
        page.get_by_test_id("submit-order").click()

        expect(page.get_by_test_id("order-result")).to_be_visible()
        order_id = page.get_by_test_id("order-id").inner_text()
        assert order_id.startswith("WO-")
        expect(page.get_by_test_id("order-status")).to_have_text("PENDING")

        page.get_by_test_id("btn-approve").click()
        expect(page.get_by_test_id("order-status")).to_have_text("APPROVED")

        page.get_by_test_id("btn-run").click()
        expect(page.get_by_test_id("order-status")).to_have_text("DONE", timeout=20000)
        ev = int(page.get_by_test_id("evidence-count").inner_text())
        assert ev >= 3, f"证据条数不足: {ev}"

        # 服务端状态与 UI 一致
        rec = _api(base, f"/tg-api/v1/work-orders/{order_id}")
        assert rec["status"] == "DONE"
        assert rec["report"]["degraded"] in (True, False)

        page.screenshot(path=str(_art("order-done.png")), full_page=True)

    def test_red_line_r1_forbidden_zone(self, page):
        base = page._tg_base
        _open(page, base)
        page.get_by_test_id("input-x").fill("55")     # 高压配电房（禁入区）
        page.get_by_test_id("input-y").fill("55")
        page.get_by_test_id("submit-order").click()
        expect(page.get_by_test_id("order-result")).to_be_visible()
        page.get_by_test_id("btn-approve").click()
        expect(page.get_by_test_id("order-status")).to_have_text("APPROVED")
        page.get_by_test_id("btn-run").click()
        expect(page.get_by_test_id("order-status")).to_have_text("ABORTED", timeout=20000)
        expect(page.get_by_test_id("order-result")).to_contain_text("R1")

    def test_red_line_r2_overload_without_token(self, page):
        base = page._tg_base
        _open(page, base)
        page.get_by_test_id("input-weight").fill("80")   # 超过 50kg 阈值
        page.get_by_test_id("input-x").fill("25")
        page.get_by_test_id("input-y").fill("14")
        page.get_by_test_id("submit-order").click()
        expect(page.get_by_test_id("order-result")).to_be_visible()
        page.get_by_test_id("btn-approve").click()
        page.get_by_test_id("btn-run").click()
        expect(page.get_by_test_id("order-status")).to_have_text("ABORTED", timeout=20000)
        expect(page.get_by_test_id("order-result")).to_contain_text("R2")


class TestResilience:
    def test_mobile_viewport_no_horizontal_scroll(self, browser, live_server):
        ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                  locale="zh-CN")
        pg = ctx.new_page()
        pg.goto(live_server + "/", wait_until="domcontentloaded")
        pg.wait_for_selector(READY, timeout=20000)
        overflow = pg.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"移动端出现横向滚动 {overflow}px"
        pg.screenshot(path=str(_art("dashboard-mobile.png")), full_page=True)
        ctx.close()

    def test_offline_falls_back_to_demo(self, page):
        base = page._tg_base
        page.route("**/tg-api/**", lambda route: route.abort())
        _open(page, base)
        expect(page.get_by_test_id("source-badge")).to_have_text("数据源：演示兜底")
        expect(page.locator("#robots-body tr")).to_have_count(4)
        expect(page.get_by_test_id("banner")).to_contain_text("接口不可达")

    def test_refresh_keeps_live(self, page):
        base = page._tg_base
        _open(page, base)
        page.get_by_test_id("btn-refresh").click()
        expect(page.get_by_test_id("source-badge")).to_have_text("数据源：实时 API")

    def test_desktop_screenshot(self, page):
        base = page._tg_base
        _open(page, base)
        page.screenshot(path=str(_art("dashboard-desktop.png")), full_page=True)


def _art(name: str):
    from pathlib import Path

    d = Path(__file__).resolve().parent / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d / name
