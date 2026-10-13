const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(require.resolve('playwright', { paths: [process.argv[2] || process.cwd()] }));
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'outputs/config-sync-ui');
const failure = {trade_date:'20260901',code:'000001',evidence:'minute_bars',reason:'缺少120个交易分钟（13:01:00 至 15:00:00）；eltdx历史源不可用'};
const difference = {current_version:'local-v1',result_version:'remote-v2',retained_local_ids:[],changed_count:1,
  strategies:[{id:'weak_to_strong',name:'弱转强',local_version:'aaa',incoming_version:'bbb',status:'changed',changes:[
    {field:'execution.confirmation_deadline',local:'10:00:00',incoming:'10:30:00'}]}]};
(async()=>{
  const server=http.createServer((req,res)=>{
    const name=req.url.split('?')[0];
    const file=name==='/strategies'?path.join(output,'strategies.html'):name==='/portfolio'?path.join(output,'portfolio.html'):
      name==='/static/css/app.css'?path.join(root,'web/static/css/app.css'):null;
    if(!file){res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type',name.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||'chrome'});
    for(const width of [1440,390]) {
      const page=await browser.newPage({viewport:{width,height:950}});
      await page.route('https://**', route=>route.abort());
      const errors=[];let imports=0;
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/api/**',route=>{
        const url=new URL(route.request().url());let result;
        if(url.pathname.endsWith('/config/export'))result={effective_version:'local-v1',source_path:'webdata/settings/strategy_combinations.yaml'};
        else if(url.pathname.endsWith('/config/compare'))result={ok:true,data:difference};
        else if(url.pathname.endsWith('/config/import')){imports++;result={ok:true,data:{effective_version:'remote-v2',backup_path:'backup.yaml'}};}
        else if(url.pathname==='/api/strategies')result={ok:true,profiles:[]};
        else if(url.pathname.endsWith('/replay/checkpoint'))result={ok:true,data:{blocked_date:'20260901',blocked_reason:'分钟证据缺失',evidence_failures:[failure]}};
        else if(url.pathname.endsWith('/accounts'))result={ok:true,data:[{account_key:'default',name:'模拟交易账户'}]};
        else result={ok:true,data:{positions:[],trades:[],exit_signals:[],summary:{}}};
        return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/strategies`);
      await page.locator('#config-version').getByText('local-v1',{exact:false}).waitFor();
      await page.locator('#config-file').setInputFiles({name:'config.json',mimeType:'application/json',buffer:Buffer.from('{}')});
      await page.getByRole('dialog').waitFor();
      await page.locator('#config-diff').getByText(/10:30:00/).waitFor();
      assert.equal(imports,0);
      assert.match(await page.locator('#config-diff').innerText(),/10:00:00[\s\S]*10:30:00/);
      const dialog=await page.locator('#config-dialog').boundingBox();assert(dialog.width<=width);
      await page.screenshot({path:path.join(output,`compare-${width}.png`)});
      page.once('dialog',dialog=>dialog.accept());await page.locator('#apply-config').click();
      await page.getByText('已导入 remote-v2',{exact:false}).waitFor();assert.equal(imports,1);
      await page.goto(`http://127.0.0.1:${server.address().port}/portfolio`);
      await page.locator('#replay-evidence').waitFor({state:'visible'});
      assert.match(await page.locator('#replay-evidence-rows').innerText(),/20260901[\s\S]*000001[\s\S]*分钟行情[\s\S]*120/);
      await page.screenshot({path:path.join(output,`evidence-${width}.png`)});
      assert.deepEqual(errors,[]);
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
      assert.equal(overflow,false);
      await page.close();
    }
    console.log('Config comparison/import and replay evidence: desktop/mobile passed');
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
