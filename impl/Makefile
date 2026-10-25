.PHONY: help test test-verbose lint fmt run demo smoke clean

PY      := ../.venv/Scripts/python.exe
PYTHONPATH_ENV := packages/tiangong_core;services/agent-runtime

help:
	@echo 天工 Tiangong-EWA · 常用命令
	@echo   make test        运行全部测试
	@echo   make coverage    覆盖率门禁（核心 ≥ 90%%）
	@echo   make lint        静态检查（ruff）
	@echo   make run         本地启动 agent-runtime（:8000）
	@echo   make demo        跑一遍演示闭环（无真机）
	@echo   make smoke       冒烟（需先 make run）

test:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m pytest -q

test-verbose:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m pytest -v

coverage:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m coverage run --source=tiangong_core,app -m pytest -q
	$(PY) -m coverage report

lint:
	$(PY) -m ruff check packages services

fmt:
	$(PY) -m ruff format packages services

run:
	cd services/agent-runtime && PYTHONPATH="../../packages/tiangong_core;." ../../$(PY) -m granian --interface asgi app.server:app --host 0.0.0.0 --port 8000 --reload

demo:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) tools/demo_run.py

smoke:
	curl -s localhost:8000/healthz
	curl -s localhost:8000/tg-api/v1/robots

clean:
	rm -rf .pytest_cache .coverage **/__pycache__
