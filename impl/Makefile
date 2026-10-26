.PHONY: help test test-verbose e2e lint fmt run demo smoke coverage \
        bulk redis warmup run-perf perf-k6 perf-locust clean

PY      := ../.venv/Scripts/python.exe
PYTHONPATH_ENV := packages/tiangong_core;services/agent-runtime
BULK    := data/tiangong_bulk.db
ROWS    ?= 100000000
PORT    ?= 8200

help:
	@echo 天工 Tiangong-EWA · 常用命令
	@echo   make test        单元+接口测试（115 项，不含浏览器）
	@echo   make e2e         Playwright 全链路 E2E（13 项，需 Chromium）
	@echo   make coverage    覆盖率门禁（核心 ≥ 90%%）
	@echo   make lint        静态检查（ruff）
	@echo   make run         本地启动 agent-runtime（:8000）
	@echo   make demo        跑一遍演示闭环（无真机）
	@echo   make smoke       冒烟（需先 make run）
	@echo   make bulk        生成 1 亿条分析库数据（ROWS= 可改）
	@echo   make redis       启动 Redis 兼容缓存服务（:6379）
	@echo   make warmup      预热分析聚合到 Redis（需先 make redis）
	@echo   make run-perf    压测用服务（文件库+分析库+Redis L2，:8200）
	@echo   make perf-k6     k6 压测
	@echo   make perf-locust Locust 压测

test:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m pytest -q

test-verbose:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m pytest -v

e2e:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m pytest tests/e2e -m e2e -q

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

# ---------------------------------------------------------------- 数据与缓存
bulk:
	$(PY) ../tools/gen_bulk_data.py --rows $(ROWS) --db ../$(BULK) --force

redis:
	$(PY) ../tools/redis_compat.py

warmup:
	$(PY) ../tools/warmup_cache.py --bulk-db ../$(BULK)

# 压测服务：文件库 + 分析库 + Redis L2（单进程，便于观测）
run-perf:
	cd services/agent-runtime && \
	TIANGONG_DB="$(CURDIR)/../data/tiangong.db" \
	TIANGONG_BULK_DB="$(CURDIR)/../$(BULK)" \
	TIANGONG_REDIS_URL="redis://127.0.0.1:6379/0" \
	PYTHONPATH="../../packages/tiangong_core;." \
	../../$(PY) -m granian --interface asgi app.server:app --host 127.0.0.1 --port $(PORT)

perf-k6:
	mkdir -p tests/perf/results
	k6 run --summary-export=tests/perf/results/k6-summary.json \
		-e BASE=http://127.0.0.1:$(PORT) tests/perf/k6_tiangong.js

perf-locust:
	mkdir -p tests/perf/results
	../.venv/Scripts/locust.exe -f tests/perf/locustfile.py --headless \
		-u 30 -r 5 -t 60s --host http://127.0.0.1:$(PORT) \
		--csv tests/perf/results/locust --only-summary

clean:
	rm -rf .pytest_cache .coverage **/__pycache__
