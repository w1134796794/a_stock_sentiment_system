const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(require.resolve('playwright', { paths: [process.argv[2] || process.cwd()] }));

const root = path.resolve(__dirname, '..');
const groups = ['重点确认', '盘中观察', '暂不参与'];
const strategies = ['主线龙头候选', '弱转强修复', '首板启动', '涨停回踩', '跌停反包'];
const row = (code, group, hits) => ({ code, name: `候选${code}`, action_group: group,
  hit_strategies: hits, strategy_consensus: hits.length, strategy_total: 5,
  mainline: '芯片', mainline_confirmed: true, sector_strength: 70, position: '观察' });
const fixture = Object.fromEntries(groups.map((group, index) => [group, [
  row(`60000${index}`, group, [strategies[0], strategies[index + 1]]),
  row(`60001${index}`, group, [strategies[4]]),
]]));
fixture['盘中观察'].push(row('600099', '盘中观察', ['主线龙头候选扩展']));
fixture['重点确认'][0].hit_strategies.push(strategies[0]);

(async () => {
  const server = http.createServer((req, res) => {
    const assets = {
      '/static/workbench/workspace.js': ['web/static/workbench/workspace.js', 'text/javascript'],
      '/static/workbench/workspace.css': ['web/static/workbench/workspace.css', 'text/css'],
      '/static/css/app.css': ['web/static/css/app.css', 'text/css'],
    };
    const asset = assets[req.url];
    if (!asset) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': asset[1] });
    res.end(fs.readFileSync(path.join(root, asset[0])));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' });
    const output = path.join(root, 'outputs/strategy-filter-ui');
    fs.mkdirSync(output, { recursive: true });
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      let apiRequests = 0;
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/workspace', route => route.fulfill({ contentType: 'text/html', body:
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/css/app.css"><link rel="stylesheet" href="/static/workbench/workspace.css"><div id="workbench-root" data-initial-date="20261008"></div><script type="module" src="/static/workbench/workspace.js"></script>' }));
      await page.route('**/api/v1/workbench**', route => {
        apiRequests += 1;
        const url = new URL(route.request().url());
        let data;
        if (url.pathname.endsWith('/realtime')) data = { rows: [], refresh_policy: { auto_refresh: false } };
        else if (url.pathname.endsWith('/intelligence')) data = null;
        else if (url.pathname.includes('/stocks/')) data = { candles: [], industry: [], concepts: [] };
        else if (url.pathname.includes('/candidates/')) data = fixture['重点确认'][0];
        else data = { trade_date: url.searchParams.get('date') || '20261008',
          available_dates: ['20261008', '20261009'], market: { risk_flags: [] }, market_context: { promotion:
            url.searchParams.get('date') === '20261009' ? {} : {
              rate_1to2: 7.5, rate_1to2_success: 3, rate_1to2_sample: 40,
              rate_2to3: 66.67, rate_2to3_success: 2, rate_2to3_sample: 3,
              rate_3to4: 0, rate_3to4_success: 0, rate_3to4_sample: 2,
              rate_high: null, rate_high_success: 0, rate_high_sample: 0,
            } },
          decision_summary: { actionable: 5 }, quick_links: [],
          groups: url.searchParams.get('date') === '20261009' ? {} : fixture };
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, data }) });
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/workspace`);
      const select = page.getByRole('combobox', { name: '按策略筛选' });
      await select.waitFor();
      const promotion = page.getByRole('region', { name: '连板晋级梯队' });
      for (const [label, rate, counts] of [['一进二', '7.5%', '3 / 40'], ['二进三', '66.7%', '2 / 3'], ['三进四', '0.0%', '0 / 2'], ['高位晋级（4板+）', '--', '0 / 0']]) {
        const tier = promotion.locator(':scope > div').filter({ has: page.getByText(label, { exact: true }) });
        assert.equal(await tier.locator('strong').innerText(), `${rate}\n${counts}`);
      }
      await promotion.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `promotion-${viewport.width}.png`) });
      assert.equal(await select.locator('option').count(), 7);
      assert.equal(await select.locator('option').filter({ hasText: '主线龙头候选（3）' }).count(), 1);
      await page.waitForTimeout(800);
      const requestCount = apiRequests;
      await select.selectOption(strategies[0]);
      assert.equal(await page.locator('.candidate-card').count(), 3);
      for (const group of groups) {
        const section = page.locator('.decision-section').filter({ has: page.getByRole('heading', { name: new RegExp(`^${group}\\s*1$`) }) });
        assert.equal(await section.locator('.candidate-card').count(), 1);
      }
      for (const strategy of strategies.slice(1)) {
        await select.selectOption(strategy);
        assert.equal(await page.locator('.candidate-card').count(), strategy === strategies[4] ? 3 : 1);
      }
      await select.selectOption(strategies[0]);
      await page.getByRole('textbox', { name: '搜索候选股' }).fill('600002');
      assert.equal(await page.locator('.candidate-card').count(), 1);
      await page.getByRole('textbox', { name: '搜索候选股' }).fill('不存在');
      assert.equal(await page.locator('.candidate-card').count(), 0);
      assert.equal(await page.getByText('没有符合当前筛选条件的股票。', { exact: true }).count(), 3);
      await page.getByRole('textbox', { name: '搜索候选股' }).fill('');
      for (const group of groups) {
        await page.locator('.group-filter').getByRole('button', { name: group, exact: true }).click();
        assert.equal(await page.locator('.decision-section').count(), 1);
        assert.equal(await page.locator('.candidate-card').count(), 1);
      }
      await page.locator('.group-filter').getByRole('button', { name: '全部', exact: true }).click();
      assert.equal(apiRequests, requestCount, 'Filtering must not request API or market data');
      await page.locator('.decision-tools').scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
      await page.screenshot({ path: path.join(output, `strategy-${viewport.width}.png`) });
      await page.getByRole('combobox', { name: '选择交易日' }).selectOption('20261009');
      await page.locator('.decision-section--focus .empty-state').waitFor();
      assert.equal(await promotion.locator('small').first().innerText(), '-- / --');
      assert.equal(await select.inputValue(), strategies[0]);
      assert.equal(await select.locator('option').filter({ hasText: '主线龙头候选（0）' }).count(), 1);
      await select.selectOption('');
      assert.equal(await page.locator('.candidate-card').count(), 0);
      await page.getByRole('combobox', { name: '选择交易日' }).selectOption('20261008');
      await page.locator('.candidate-card').first().waitFor();
      assert.equal(await page.locator('.candidate-card').count(), 5);
      await page.locator('.market-controls button').click();
      await page.waitForTimeout(100);
      assert.equal(await page.locator('.candidate-card').count(), 5);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Strategy filters passed: all groups, exact/multi-hit matching, counts, search, empty dates, refresh, no extra requests, desktop/mobile layout');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
