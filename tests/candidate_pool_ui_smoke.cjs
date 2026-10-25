const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..'), output=path.join(root,'outputs/candidate-ui');
(async()=>{
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    const file=url.pathname.startsWith('/static/')?path.join(root,'web',url.pathname):path.join(output,url.pathname==='/fragment'?'fragment.html':url.pathname.startsWith('/stock/')?'stock.html':'page.html');
    if(!fs.existsSync(file)){res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:1000}}), errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('https://**',r=>r.abort());
      await page.goto(`http://127.0.0.1:${server.address().port}/data/strategy/20261023`,{waitUntil:'domcontentloaded'});
      await page.waitForFunction(()=>document.querySelector('#side-nav svg.lucide'));
      for(const replaced of [false,true]){
        if(replaced){
          await page.evaluate(()=>htmx.ajax('GET','/fragment',{target:'#section-panel',swap:'innerHTML'}));
          await page.waitForFunction(()=>document.querySelectorAll('[data-dp-filter][aria-pressed=true]').length===1);
        }
        await page.click('[data-dp-filter="主线龙头候选"]');
        assert.equal(await page.locator('.dp-card:not([hidden])').count(),1);
        assert.match(await page.textContent('[data-dp-status]'),/匹配 1 \/ 4/);
        await page.click('[data-dp-filter="弱转强修复"]');
        assert.equal(await page.locator('.dp-card:not([hidden])').count(),2);
        await page.click('[data-dp-filter="暂无匹配策略"]');
        assert.equal(await page.locator('.dp-card:not([hidden])').count(),0);
        assert.equal(await page.locator('[data-dp-empty]:not([hidden])').count(),3);
        await page.click('[data-dp-filter=""]');
        assert.equal(await page.locator('.dp-card:not([hidden])').count(),4);
        const href=await page.locator('.dp-stock-link').first().getAttribute('href');
        assert.equal(href,'/stock/300369?date=20261023&context_date=20261023#daily-view');
        assert.match(await page.locator('.dp-chart-links a').nth(1).getAttribute('href'),/#intraday-view$/);
        await page.click('[data-dp-filter="主线龙头候选"]');
        await page.click('[data-dp-filter="主线龙头候选"]');
        assert.equal(await page.locator('.dp-card:not([hidden])').count(),4);
      }
      await page.screenshot({path:path.join(output,`candidates-${width}.png`),fullPage:true});
      await page.route('**/api/stock/**',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({date:'20261023',market_date:'20261023',intraday:[{time:1792719060,value:10},{time:1792719120,value:10.2}],daily:[{time:'2026-10-22',open:10,high:11,low:9,close:10.5},{time:'2026-10-23',open:10.5,high:11.5,low:10,close:11}],signals:[],plans:[],risk:[],auction:{}})}));
      await page.locator('.dp-stock-link').first().click();
      await page.waitForURL('**/stock/300369?date=20261023&context_date=20261023#daily-view');
      await page.waitForFunction(()=>document.querySelector('#daily-subtitle').textContent.includes('2 根日K'));
      assert.ok(await page.locator('#daily-chart canvas').count()>0);
      assert.ok(await page.locator('#intraday-chart canvas').count()>0);
      await page.screenshot({path:path.join(output,`charts-${width}.png`),fullPage:true});
      assert.deepEqual(errors,[]);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.close();
    }
    console.log('Candidate pool: strategy filters, empty/reset/toggle states, SSR + HTMX replacement, chart links, desktop/mobile passed.');
  }finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
