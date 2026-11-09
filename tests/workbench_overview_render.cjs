// Render-only fixtures: these values never enter the application data stores.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const frontend = path.resolve(__dirname, '../frontend');
const resolve = name => require.resolve(name, { paths: [frontend] });
const React = require(resolve('react'));
const { renderToStaticMarkup } = require(resolve('react-dom/server'));
const filename = path.join(frontend, 'src/TerminalOverview.tsx');
(async () => {
const { createServer } = await import(pathToFileURL(resolve('vite')).href);
const server = await createServer({ root: frontend, configFile: false, server: { middlewareMode: true } });
try {
const component = await server.ssrLoadModule('/src/TerminalOverview.tsx');
const render = (market, market_context, intelligence = null) => renderToStaticMarkup(
  React.createElement(component.TerminalOverview, { data: {
    trade_date: '20261106', market, market_context, generated_at: 'fixture',
  }, intelligence, loadingIntel: false, chart: React.createElement('div', null, 'chart fixture') }));

const missing = render({}, {});
assert.match(missing, /市场评分 未评估/);
assert.match(missing, /市场广度数据不完整/);
assert.doesNotMatch(missing, /今日决策|候选600|NaN|undefined/);
const zero = render({ market_score: 0, position_scale: 0, limit_up_count: 0, limit_down_count: 0 }, {
  promotion: { rate_1to2: 0, rate_1to2_success: 0, rate_1to2_sample: 5 },
  up_count: 0, down_count: 10, flat_count: 0,
});
assert.match(zero, /市场评分 0/);
assert.match(zero, /0\.0%/);
assert.match(zero, /共 10 只/);
const populated = render({ market_score: 44, risk_flags: ['风险测试'] }, {}, { limitup: { echelon: [{
  board_height: 3, count: 1, stocks: [{ code: '000001', name: '测试股票', first_time: '92500:00', open_times: 0 }],
}] } });
assert.match(populated, /09:25:00/);
assert.match(populated, /\/stock\/000001\?date=20261106/);
assert.match(populated, /\/market-review\?date=20261106/);
console.log('Terminal overview render checks passed: missing values, zero values, breadth, time and dated links.');
} finally { await server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
