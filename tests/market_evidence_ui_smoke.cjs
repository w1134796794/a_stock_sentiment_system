'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const path=require('node:path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const folder=path.join(root,'outputs','market-evidence-ui');
(async()=>{
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    let file;
    if(url.pathname.startsWith('/static/'))file=path.join(root,'web',url.pathname);
    else file=path.join(folder,url.searchParams.get('role')==='admin'?'admin.html':'viewer.html');
    if(!fs.existsSync(file)){res.writeHead(404);res.end();return;}
    res.setHeader('content-type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true,executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
    for(const role of ['viewer','admin'])for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('https://**',route=>route.abort());
      await page.route('**/api/**',async route=>{
        const url=new URL(route.request().url());let body;
        if(url.pathname.includes('/stock/'))body={ok:true,in_collection_scope:true,
          live:[{kind:'trade_batch',status:'partial',is_stale:true,received_at:'2026-10-23T09:36:00+08:00',summary:{count:2}}],
          research:[{kind:'company_profile',status:'ok',captured_at:'2026-10-23',row_count:1,preview:'<img src=x onerror=alert(1)>'}],
          auction_points:[{price:10},{price:10.1}]};
        else if(url.pathname.endsWith('/boards'))body={ok:true,rows:[]};
        else if(url.pathname.endsWith('/diagnostics'))body={ok:true,scope_count:1,pending:{count:2},collector:{status:'partial'},jobs:[]};
        else if(url.pathname.includes('/research/') || url.pathname.includes('/raw/'))body={ok:true,total:41,rows:[{text:'<script>bad()</script>'}]};
        else if(url.pathname.endsWith('/experiments'))body={ok:true,rows:[{report_id:'sample',day_count:1,complete_days:0,A_count:1,C_count:0,unknown_count:1}]};
        else throw new Error('Unexpected API '+url.pathname);
        await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/market-evidence?role=${role}`);
      await page.fill('#me-code','000001');await page.click('#me-form button[type=submit]');
      await page.waitForFunction(()=>document.querySelector('#me-status').textContent.includes('冻结采集名单中'));
      assert.match(await page.textContent('#me-live'),/不完整/);
      assert.match(await page.textContent('#me-research'),/<img/);
      assert.equal(await page.locator('#me-research img').count(),0);
      assert.equal(await page.locator('#me-diagnostics-button').count(),role==='admin'?1:0);
      await page.click('#me-boards-button');
      if(role==='admin'){
        await page.click('#me-diagnostics-button');await page.waitForFunction(()=>document.querySelector('#me-diagnostics').textContent.includes('待归档 2'));
        await page.click('#me-raw-button');await page.waitForFunction(()=>document.querySelector('#me-raw').textContent.includes('<script>'));
        await page.click('#me-next');await page.waitForFunction(()=>document.querySelector('#me-page').textContent.startsWith('21'));
        await page.click('#me-experiments-button');await page.waitForFunction(()=>document.querySelector('#me-experiments').textContent.includes('尚未测量收益'));
      }
      assert.deepEqual(errors,[]);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
      await page.screenshot({path:path.join(folder,`${role}-${width}.png`),fullPage:true});
      await page.close();
    }
    console.log('Evidence UI: viewer/admin x desktop/mobile, pagination and safe text rendering passed');
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
