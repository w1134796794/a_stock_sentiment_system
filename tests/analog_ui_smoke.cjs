const fs = require('fs');
const path = require('path');
const { chromium } = require(require.resolve('playwright', { paths: [process.argv[2] || process.cwd()] }));

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' });
  const candidate = { code: '600001', name: '测试候选', action_group: '重点确认', strategy_consensus: 2, strategy_total: 5,
    hit_strategies: ['弱转强'], mainline: '汽车零部件', mainline_confirmed: true, sector_strength: 78,
    conclusion: '分时确认后的冻结评估', evidence: {}, position: '确认后参考10%' };
  const analysis = { status: 'estimated', forecast_id: 'ui-frozen-forecast', as_of: '2026-09-01T09:40:00',
    probability: .65, probability_interval: { low: .45, high: .79 }, expected_net_return: .032, payoff_ratio: 1.8,
    reliability: { raw_sample_count: 36, trading_days: 18, effective_sample_count: 24.7 },
    fallback_level: 'similar_cases', sample_window: { start: '20260601', end: '20260828' },
    return_distributions: { net_return: { p05: -.068 } } };
  const cases = [{ case_id: 'win', code: '600002', market_date: '20260801', net_return: .05, success: true,
    weight: .05, distance: .2, exit_reason: '结构止盈', fees: 2.4,
    normalized_path: [{ day: 0, value: 100 }, { day: 1, value: 102 }, { day: 3, value: 99 }, { day: 5, value: 105 }] },
    { case_id: 'loss', code: '600003', market_date: '20260802', net_return: -.04, success: false, weight: .04, distance: .3 }];
  const directory = path.resolve('outputs/analog-ui'); fs.mkdirSync(directory, { recursive: true });
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.route('**/workspace', route => route.fulfill({ contentType: 'text/html', body:
      '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/css/app.css"><link rel="stylesheet" href="/static/workbench/workspace.css"><div id="workbench-root" data-initial-date="20260901"></div><script type="module" src="/static/workbench/workspace.js"></script>' }));
    await page.route('**/api/v1/workbench**', route => {
      const url = route.request().url();
      let data;
      if (url.includes('/analog-analysis')) data = { analysis, cases: { rows: cases, total: 2 } };
      else if (url.includes('/candidates/')) data = candidate;
      else if (url.includes('/stocks/')) data = { candles: [], industry: [], concepts: [] };
      else if (url.includes('/realtime')) data = { rows: [] };
      else if (url.includes('/intelligence')) data = null;
      else data = { trade_date: '20260901', available_dates: ['20260901'], market: { risk_flags: [] }, market_context: {},
        decision_summary: { actionable: 1 }, quick_links: [], groups: { '重点确认': [candidate] } };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, data }) });
    });
    await page.goto('http://127.0.0.1:8011/workspace');
    await page.locator('.candidate-card').click();
    await page.getByText('净盈利频率', { exact: true }).waitFor();
    await page.locator('.analog-case-list details').first().locator('summary').click();
    await page.locator('.analog-case-chart canvas').first().waitFor();
    const chart = page.locator('.analog-case-chart'); await chart.scrollIntoViewIfNeeded();
    const nonblank = await chart.evaluate(el => Array.from(el.querySelectorAll('canvas')).some(c => {
      const pixels = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      return pixels.some((v, i) => i % 4 !== 3 && v > 150);
    }));
    if (!nonblank) throw new Error('Case chart is blank');
    const overflow = await page.locator('.drawer').evaluate(el => el.scrollWidth > el.clientWidth + 2);
    if (overflow) throw new Error('Drawer overflows viewport');
    const bounds = await page.locator('.drawer').boundingBox();
    if (bounds.x < 0 || bounds.x + bounds.width > viewport.width + 1) throw new Error('Drawer is clipped');
    await page.screenshot({ path: path.join(directory, `drawer-${viewport.width}.png`) });
    await page.getByRole('button', { name: '亏损', exact: true }).click();
    if (await page.locator('.analog-case-list details').count() !== 1) throw new Error('Profit/loss filter failed');
    if (errors.length) throw new Error(errors.join('\n'));
    await page.close();
    const reportPage = await browser.newPage({ viewport });
    reportPage.on('pageerror', e => errors.push(e.message));
    await reportPage.route('**/strategy-evaluation', route => route.fulfill({ contentType: 'text/html',
      body: fs.readFileSync(path.join(directory, 'evaluation.html'), 'utf8') }));
    await reportPage.route('**/api/operations/experiments**', route => {
      const url=route.request().url();
      if(url.endsWith('/report')) {
        const accounts={}; ['original_rules','analog_filter','analog_rotation'].forEach((name,i)=>{
          accounts[name]={summary:{valued_days:5,fill_count:6,total_return_pct:i-1,max_drawdown_pct:2,fees:200,turnover:1.5,mean_cash_utilization:.9},
            comparisons:[{code:'600001',as_of:'2026-09-01T09:40:00',recommended_research_action:'no_action',decisions:[
              {action:'hold',eligible:true},{action:'cash_buy',eligible:false,reason:'insufficient_cash'},
              {action:'rotate',eligible:false,reason:'t_plus_one'},{action:'no_action',eligible:true}]}],
            nav:[0,1,2,3,4].map(day=>({equity:1000000+day*1000*(i-1)}))};
        });
        return route.fulfill({contentType:'application/json',body:JSON.stringify({report:{kind:'analog_account_comparison',status:'complete',classification:'research_event_time',experiment_id:'report',accounts}})});
      }
      return route.fulfill({contentType:'application/json',body:JSON.stringify({rows:url.includes('kind=analog')?[{experiment_id:'report',frozen_at:'2026-09-01'}]:[]})});
    });
    await reportPage.goto('http://127.0.0.1:8011/strategy-evaluation');
    await reportPage.locator('#ev-kind').selectOption('analog');
    await reportPage.locator('#ev-analog-rows').getByText('相似过滤 + 换股', {exact:true}).waitFor();
    if(await reportPage.locator('.ev-kpis').isVisible())throw new Error('Strategy KPIs leak into analog report');
    await reportPage.locator('#ev-actions summary').click();
    if(await reportPage.locator('#ev-action-rows tr').count()!==3)throw new Error('Account action comparisons missing');
    const reportNonblank=await reportPage.locator('#ev-analog canvas').evaluateAll(canvases=>canvases.some(c=>{
      const pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      return pixels.some((v,i)=>i%4!==3&&v>150);
    }));
    if(!reportNonblank)throw new Error('Account NAV chart is blank');
    if(await reportPage.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2))throw new Error('Evaluation controls overflow viewport');
    await reportPage.screenshot({path:path.join(directory,`evaluation-${viewport.width}.png`)});
    if(errors.length)throw new Error(errors.join('\n'));
    await reportPage.close();
  }
  await browser.close(); console.log('Desktop/mobile drawer, chart pixels and case filters passed');
})().catch(error => { console.error(error); process.exit(1); });
