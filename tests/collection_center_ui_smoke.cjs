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
    const file = url.pathname.startsWith('/static/') ? path.join(root, 'web', url.pathname) : path.join(folder, 'backfill.html');
    if (!fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport:{width,height:1050}}), errors = [], posts = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('https://**', route => route.abort());
      let fail = false;
      const plan = {plan_id:'a'.repeat(64),candidate_date:'20261023',frozen_at:'2026-10-26T09:00:12+08:00',stock_count:35,leader_count:30,decision_count:5,hot_window_seconds:180,listing_exclusions:[],rows:[{code:'002242',name:'九阳股份',sources:['leader_pool','decision_pool'],sectors:['家用电器','机器人概念']}],warnings:[]};
      await page.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        if (route.request().method()==='POST') posts.push(url.pathname);
        let data = {};
        if (url.pathname.endsWith('/defaults')) data = {today:'20261026',postclose:'20261023',preopen:'20261026',intraday:'20261026',calendar_verified:true};
        else if (url.pathname.endsWith('/market-collection/status')) {
          if (fail) { await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({ok:false,message:'测试读取失败'})}); return; }
          const phase=url.searchParams.get('phase');
          data = {phase,date:url.searchParams.get('date'),as_of:'2026-10-26T09:20:12+08:00',trading_day:true,calendar_verified:true,task:{state:'idle'},plan,boards:{codes:['880001']}};
          if (phase==='postclose') data.data={ready:true,complete:false,table_rows:{stock_daily_silver:5374,sector_daily_silver:180,index_daily_silver:3},missing:[],source_missing:['hm_detail'],sources:{all_daily:{ok:true,rows:5374},hm_detail:{ok:false,rows:0,error:'上游返回为空'}}};
          if (phase==='intraday') Object.assign(data,{live_date_matches:true,storage:'redis',missing:[],collector:{ok:true,fresh:true,lease_owned:true,count:35,requested_count:35,updated_at:'2026-10-26T09:20:11+08:00'},archive:{ok:true,fresh:true,pending:0,updated_at:'2026-10-26T09:20:11+08:00'},quotes:{sample_intervals:{p50_seconds:3.1}},minute_sync:{ok:true,fresh:true,updated_at:'2026-10-26T09:20:10+08:00'}});
        } else if (url.pathname==='/api/admin/history-workflows') data=[];
        await route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,data})});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/market-collection`, {waitUntil:'domcontentloaded', timeout:60000});
      await page.waitForFunction(() => document.querySelector('#mc-status-title').textContent.includes('增强数据'));
      assert.equal(await page.locator('[role=tab]').count(),4);
      assert.equal(posts.length,0);
      await page.screenshot({path:path.join(folder,`collection-postclose-${width}.png`),fullPage:true});
      await page.click('#mc-action');
      assert.deepEqual(posts,['/api/fetch']);
      await page.click('[data-tab=preopen]');
      await page.waitForFunction(() => document.querySelector('#mc-status-title').textContent.includes('名单已冻结'));
      assert.equal(await page.locator('#mc-action').isDisabled(),true);
      assert.match(await page.textContent('#mc-table'),/九阳股份/);
      await page.screenshot({path:path.join(folder,`collection-preopen-${width}.png`),fullPage:true});
      await page.click('[data-tab=intraday]');
      await page.waitForFunction(() => document.querySelector('#mc-status-title').textContent.includes('运行正常'));
      assert.match(await page.textContent('#mc-metrics'),/3.1s/);
      await page.click('#mc-action');
      assert.equal(posts.length,1);
      await page.screenshot({path:path.join(folder,`collection-intraday-${width}.png`),fullPage:true});
      await page.click('[data-tab=backfill]');
      assert.equal(await page.locator('#history-workflow').isVisible(),true);
      assert.equal(await page.locator('[data-hw-phase=run]').count(),0);
      assert.equal(await page.locator('#mc-panel-data').isVisible(),false);
      await page.screenshot({path:path.join(folder,`collection-backfill-${width}.png`),fullPage:true});
      await page.click('[data-tab=postclose]');
      await page.waitForFunction(() => document.querySelector('#mc-status-title').textContent.includes('增强数据'));
      fail=true; await page.click('#mc-refresh');
      await page.waitForFunction(() => document.querySelector('#mc-error').textContent.includes('测试读取失败'));
      assert.equal(await page.locator('#mc-action').isDisabled(),true);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow,false);
      const iconCount=await page.locator('#collection-center svg.lucide').count();
      assert.ok(iconCount>=6);
      assert.deepEqual(errors,[]);
      await page.close();
    }
    console.log('Collection center: 4 tabs, read-only loads, isolated actions, errors, desktop/mobile passed.');
  } finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => {console.error(error);process.exitCode=1;});
