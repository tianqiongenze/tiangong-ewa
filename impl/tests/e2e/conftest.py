"""E2E 夹具：真实服务进程 + 真实浏览器（Chromium）。

设计：
- **独立进程**起 agent-runtime（granian），带临时业务库与分析库，
  避免污染开发态内存库；端口取空闲端口。
- 分析库用 `tools/gen_bulk_data.py --rows 200000` 生成小样本（结构与 1 亿条一致）。
- 浏览器优先用 Playwright 自带版本；若本机只装了旧版（CI 常见），
  可用 `TIANGONG_CHROME` 指定 chrome.exe 路径回退。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]          # impl/
PROJECT = ROOT.parent                               # Tiangong-EWA/
SERVICE = ROOT / "services" / "agent-runtime"
PY = Path(sys.executable)
GRANIAN = PY.parent / "granian.exe"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(0.3)
    raise RuntimeError(f"服务未就绪: {base} ({last})")


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """起真实服务进程，产出 base_url。"""
    work = tmp_path_factory.mktemp("e2e")
    bulk = work / "bulk.db"
    subprocess.run(
        [str(PY), str(PROJECT / "tools" / "gen_bulk_data.py"),
         "--rows", "200000", "--db", str(bulk), "--force"],
        check=True, capture_output=True, timeout=300)

    port = _free_port()
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join(
            [str(ROOT / "packages" / "tiangong_core"), str(SERVICE)]),
        "TIANGONG_DB": str(work / "tiangong.db"),
        "TIANGONG_BULK_DB": str(bulk),
        "TIANGONG_CACHE_TTL": "30",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.Popen(
        [str(GRANIAN), "--interface", "asgi", "--host", "127.0.0.1",
         "--port", str(port), "app.server:app"],
        cwd=str(SERVICE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def browser():
    """Chromium 实例（session 级，避免反复启动）。"""
    with sync_playwright() as p:
        exe = os.environ.get("TIANGONG_CHROME", "")
        try:
            b = p.chromium.launch()
        except Exception:
            if not exe:
                candidates = list(Path.home().glob(
                    "AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe"))
                exe = str(candidates[-1]) if candidates else ""
            if not exe:
                pytest.skip("无可用 Chromium：请执行 `playwright install chromium`")
            b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


@pytest.fixture
def page(browser, live_server):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                              locale="zh-CN")
    pg = ctx.new_page()
    pg._tg_base = live_server          # 供用例取 base_url
    errors: list[str] = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg._tg_errors = errors
    yield pg
    ctx.close()


@pytest.fixture(scope="session", autouse=True)
def _artifacts_dir():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
