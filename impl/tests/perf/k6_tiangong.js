// 天工 k6 压测：读多写少混合 + 下单写链路
//
// 场景：
//   read_heavy  阶梯加压（5→20 VU）：机群/作业单/技能/指标/分析聚合（缓存命中）
//   write_chain 定压 4 VU：下单 → 审批 → 执行（完整业务闭环，含证据与事件）
//
// 运行：
//   k6 run --summary-export=tests/perf/results/k6-summary.json tests/perf/k6_tiangong.js
//   BASE=http://127.0.0.1:8200 k6 run ...
import http from 'k6/http';
import { check, group, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://127.0.0.1:8200';

const anSummary = new Trend('an_summary_ms', true);
const anCold = new Trend('an_summary_cold_ms', true);
const orderChain = new Trend('order_chain_ms', true);
const redLine = new Counter('red_line_blocked');
const chainFail = new Rate('chain_fail_rate');

export const options = {
  scenarios: {
    read_heavy: {
      executor: 'ramping-vus',
      exec: 'readFlow',
      startVUs: 5,
      stages: [
        { duration: '15s', target: 20 },
        { duration: '40s', target: 20 },
        { duration: '10s', target: 0 },
      ],
      gracefulRampDown: '5s',
      tags: { scenario: 'read_heavy' },
    },
    write_chain: {
      executor: 'constant-vus',
      exec: 'writeFlow',
      vus: 4,
      duration: '60s',
      startTime: '5s',
      tags: { scenario: 'write_chain' },
    },
  },
  thresholds: {
    'http_req_failed': ['rate<0.01'],
    'http_req_duration{scenario:read_heavy}': ['p(95)<300', 'p(99)<800'],
    'http_req_duration{scenario:write_chain}': ['p(95)<1200'],
    'an_summary_ms': ['p(95)<120'],       // 命中缓存的聚合查询
    'chain_fail_rate': ['rate<0.02'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

const JSON_HEADERS = { 'Content-Type': 'application/json' };

export function readFlow() {
  group('read_heavy', () => {
    const robots = http.get(`${BASE}/tg-api/v1/robots`, { tags: { name: 'robots' } });
    check(robots, { 'robots 200': (r) => r.status === 200 });

    const orders = http.get(`${BASE}/tg-api/v1/work-orders`, { tags: { name: 'orders' } });
    check(orders, { 'orders 200': (r) => r.status === 200 });

    const skills = http.get(`${BASE}/tg-api/v1/skills`, { tags: { name: 'skills' } });
    check(skills, { 'skills 200': (r) => r.status === 200 });

    const metrics = http.get(`${BASE}/tg-api/v1/observability/metrics`,
      { tags: { name: 'metrics' } });
    check(metrics, { 'metrics 200': (r) => r.status === 200 });

    // 分析聚合：命中多级缓存（预热过的 key）
    const t0 = Date.now();
    const an = http.get(`${BASE}/tg-api/v1/analytics/summary?days=7`,
      { tags: { name: 'analytics_7d' } });
    anSummary.add(Date.now() - t0);
    check(an, {
      'analytics 200': (r) => r.status === 200,
      'analytics available': (r) => r.json('available') === true,
    });

    // 每 5 轮打一次未预热的窗口，观察冷查询与热查询的差距
    if (__ITER % 5 === 0) {
      const t1 = Date.now();
      const cold = http.get(`${BASE}/tg-api/v1/analytics/summary?days=60`,
        { tags: { name: 'analytics_60d_cold' } });
      anCold.add(Date.now() - t1);
      check(cold, { 'cold analytics 200': (r) => r.status === 200 });
    }
  });
  sleep(0.2 + Math.random() * 0.3);
}

export function writeFlow() {
  group('write_chain', () => {
    const t0 = Date.now();
    const payload = JSON.stringify({
      kind: 'TRANSPORT',
      params: {
        from: '原料库', to: '装配车间', what: '电池包',
        weight_kg: 35, x: 25, y: 14,
      },
      requested_by: 'k6-operator',
    });
    const sub = http.post(`${BASE}/tg-api/v1/work-orders`, payload,
      { headers: JSON_HEADERS, tags: { name: 'submit' } });
    const okSub = check(sub, { 'submit 201': (r) => r.status === 201 });
    if (!okSub) { chainFail.add(1); return; }
    const oid = sub.json('order_id');

    const appr = http.post(`${BASE}/tg-api/v1/work-orders/${oid}/approve`,
      JSON.stringify({ approved_by: 'k6-supervisor' }),
      { headers: JSON_HEADERS, tags: { name: 'approve' } });
    check(appr, { 'approve 200': (r) => r.status === 200 });

    const run = http.post(`${BASE}/tg-api/v1/work-orders/${oid}/run`, '{}',
      { headers: JSON_HEADERS, tags: { name: 'run' } });
    const okRun = check(run, {
      'run 200': (r) => r.status === 200,
      'run 有报告': (r) => r.json('trace_id') !== undefined,
    });
    if (!okRun) { chainFail.add(1); return; }
    if (run.json('aborted') === true) { redLine.add(1); }
    chainFail.add(0);
    orderChain.add(Date.now() - t0);
  });
  sleep(0.5 + Math.random() * 0.5);
}
