const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const folder = path.join(root, 'outputs/history-workflow-ui');
(async () => {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const file = url.pathname.startsWith('/static/') ? path.join(root, 'web', url.pathname) : path.join(folder, `${url.searchParams.get('role') || 'admin'}.html`);
    if (!fs.existsSync(file)) {res.writeHead(404); res.end(); return;}
    res.setHeader('content-type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    for (const mode of ['admin', 'backfill']) for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport: {width, height: 1000}}), errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('dialog', dialog => dialog.accept());
      const job = {job_id: 'a'.repeat(32), start:'20261022', end:'20261023', dates:['20261022','20261023'], potential_stock_count:35, updated_at:'2026-10-25T10:00:00+08:00', stages:{}, state:'idle', error:'', phase:''};
      const posted = [];
      let firstFreeze = true;
      await page.route('https://**', route => route.abort());
      await page.route('**/api/**', async route => {
        const url = new URL(route.request().url()), method = route.request().method();
        let body = {ok:true, data:{}};
        if (url.pathname.startsWith('/api/admin/history-workflows')) {
          const parts = url.pathname.split('/').filter(Boolean), phase = parts[4];
          if (method === 'POST' && phase) {
            posted.push(phase);
            job.phase = phase; job.state='complete'; job.stages[phase]='complete'; job.failures=[];
            if (phase === 'prepare') job.pool_scope=[{date:'20261022',candidate_date:'20261021',leader_pool:30,decision_pool:5,stock_count:35,basis:'preopen_frozen'}];
            if (phase === 'prefetch') job.progress={stage:'补取池内分钟',trade_date:'20261022',code:'000001',completed:1,total:40,success:1,failed:0,skipped:0,pool_success:1,pool_total:35,last_result:{valid:true,rows:240,source:'axdata',reason:''}};
            if (phase === 'freeze' && firstFreeze) {
              firstFreeze=false; job.state='blocked'; job.stages.freeze='blocked'; job.error='缺少分钟与板块证据';
              job.failures=[{trade_date:'20261022',code:'000001',evidence:'sector_minutes',reason:'<img src=x onerror=alert(1)> 缺少真实板块成分分钟，不能向前填充'}];
            } else if (phase === 'freeze') {job.error=''; job.frozen_version='b'.repeat(32); job.archive_status=[{date:'20261022',profile:'leader_pool',status:'missing'}];}
            else if (phase === 'run') job.summary={total_return:.023,max_drawdown:-.01,total_trades:4};
            else if (phase === 'reconcile') job.reconciliation=[{date:'20261022',profile:'leader_pool',status:'missing',reason:'缺少当时归档，不能用收盘快照替代'}];
            body.data=job;
          } else if (parts.length===3) body.data=[job];
          else if (url.pathname.endsWith('/report/log')) body.data='Fixture log: no market calls or real account writes';
          else if (url.pathname.endsWith('/report/result')) body.data={trades:[{date:'20261022',stock_code:'000001',stock_name:'示例',action:'BUY',entry_price:10.25,exit_price:null,pnl:0}]};
          else body.data=job;
        } else if (url.pathname.endsWith('/accounts')) body.data=[];
        else if (url.pathname.endsWith('/status')) body={state:'idle',lines:[],next:0};
        else if (url.pathname==='/api/portfolio') body.data={positions:[],trades:[],exit_signals:[],summary:{}};
        await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/${mode === 'backfill' ? 'market-backfill' : 'portfolio/backtest'}?role=${mode}`);
      await page.waitForFunction(() => document.querySelector('#hw-status').textContent.includes('20261022'));
      if (mode === 'backfill') assert.equal(await page.locator('[data-hw-phase=run]').count(), 0);
      else assert.equal(await page.locator('[data-hw-phase=run]').isDisabled(), true);
      assert.match(await page.textContent('[data-hw-phase=prepare]'), /读取历史池名单/);
      await page.click('[data-hw-phase=prepare]');
      await page.waitForFunction(() => document.querySelector('#hw-detail').textContent.includes('20261021'));
      assert.match(await page.textContent('#hw-detail'), /35/);
      await page.screenshot({path:path.join(folder,`${mode}-pools-${width}.png`),fullPage:true});
      await page.click('[data-hw-phase=prefetch]');
      await page.waitForFunction(() => document.querySelector('#hw-status').textContent.includes('240条 axdata'));
      assert.match(await page.textContent('#hw-status'), /成功 1.*失败 0.*池内 1\/35/);
      await page.screenshot({path:path.join(folder,`${mode}-progress-${width}.png`),fullPage:true});
      await page.click('[data-hw-phase=freeze]');
      await page.waitForFunction(() => document.querySelector('#hw-error').textContent.includes('缺少分钟'));
      if (mode === 'admin') assert.equal(await page.locator('[data-hw-phase=run]').isDisabled(), true);
      else assert.equal(await page.locator('#hw-use-backtest').isVisible(), false);
      assert.equal(await page.locator('#hw-detail img').count(),0);
      await page.screenshot({path:path.join(folder,`${mode}-blocked-${width}.png`),fullPage:true});
      await page.click('[data-hw-phase=freeze]');
      if (mode === 'admin') {
        await page.click('[data-hw-phase=run]');
        await page.click('[data-hw-phase=reconcile]');
        await page.waitForFunction(() => document.querySelector('#hw-detail').textContent.includes('不能用收盘快照替代'));
        assert.match(await page.textContent('#hw-summary'), /2.30%/);
        await page.waitForFunction(() => document.querySelector('#hw-trades-detail').textContent.includes('10.25'));
      } else {
        await page.waitForFunction(() => !document.querySelector('#hw-use-backtest').hidden);
        assert.match(await page.getAttribute('#hw-use-backtest','href'), new RegExp('history_job=' + job.job_id));
        assert.match(await page.textContent('#hw-data-summary'), /校验通过/);
        assert.deepEqual(posted, ['prepare','prefetch','freeze','freeze']);
        assert.equal(await page.textContent('#hw-summary'), '');
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2));
      assert.deepEqual(errors, []);
      await page.screenshot({path:path.join(folder,`${mode}-complete-${width}.png`),fullPage:true});
      if (mode === 'backfill') {
        await page.click('#hw-use-backtest');
        await page.waitForFunction(() => document.querySelector('#hw-status')?.textContent.includes('20261022'));
        assert.equal(await page.inputValue('#hw-jobs'), job.job_id);
        assert.equal(await page.locator('[data-hw-phase=run]').isDisabled(), false);
        assert.deepEqual(posted, ['prepare','prefetch','freeze','freeze']);
      }
      await page.close();
    }
    console.log('History/backfill desktop/mobile: phase gates, retries, selected-version handoff, no automatic backtest and safe rendering passed');
  } finally {if(browser) await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(error => {console.error(error);process.exitCode=1;});
