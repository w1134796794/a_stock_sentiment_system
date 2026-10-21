const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(require.resolve('playwright', {paths: [process.argv[2] || process.cwd()]}));
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'outputs/intraday-diagnosis-20261021');
fs.mkdirSync(output,{recursive:true});
const report = {
  run_id:'a'.repeat(32), candidate_date:'20261014', market_date:'20261015', until:'10:30:00',
  input_hash:'a'.repeat(64), code_hash:'b'.repeat(64), input_basis:'preopen_frozen',
  confirmed_stocks:0, complete:false, limitations:[], live_runtime_verified:false,
  failures:[{date:'20261015',code:'603533',evidence:'session_open',reason:'缺少当日真实开盘价'}],
  runtime_summary:{sent_stocks:1},
  stocks:[{code:'603533',name:'掌阅科技',status:'missing_evidence',session_open:null,reason:'缺少当日真实开盘价',transitions:[],
    opening_evidence:{open_price:null,pre_close:29.92,open_gap:null,opening_source:''},
    runtime_diagnostic:{state:'sent',confirm_times:['20261015 09:38:00'],profiles:['production'],
      notification_attempts:[{occurred_at:'2026-10-15T09:39:01',status:'sent',reason:'<img src=x onerror=alert(1)>'}],
      veto_reasons:[],replay_discrepancy:'replay_missing_evidence'}},
    {code:'601811',name:'新华文轩',status:'observe',reason:'缺少板块证据',transitions:[],
      opening_evidence:{open_price:24,pre_close:23.25,open_gap:.032258,opening_source:'eltdx_open_archive'},
      runtime_diagnostic:{state:'attempt_not_sent',confirm_times:['09:47:00'],profiles:['production'],
        notification_attempts:[{occurred_at:'2026-10-15T09:48:01',status:'blocked',reason:'collector_stale'}],
        veto_reasons:['采集器门禁'],replay_discrepancy:'replay_missing_evidence'}},
    {code:'002242',name:'九阳股份',status:'observe',reason:'实时行情已过期',transitions:[],
      runtime_diagnostic:{state:'no_runtime_evidence',confirm_times:[],profiles:[],
        notification_attempts:[],veto_reasons:[],replay_discrepancy:''}}],
  recorded_runtime:{confirmation_history:{rows:[]}},
};
(async () => {
  const server = http.createServer((req,res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const file = url.pathname.startsWith('/static/')
      ? path.join(root,'web',url.pathname)
      : path.join(root,'outputs/intraday-replay-ui/page.html');
    if (!fs.existsSync(file)) {res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true,channel:'chrome'});
    for (const width of [1440,390]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      const errors = [];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('https://**',route=>route.abort());
      await page.route('**/api/**',route=> {
        const url=new URL(route.request().url());
        let body;
        if(url.pathname==='/api/intraday-replay') body={runs:[{run_id:report.run_id,state:'done',started_epoch:1,
          request:{market_date:'20261015',profile:'leader_pool',lookback:10,until:'10:30:00',codes:[]}}]};
        else if(url.searchParams.has('report')) body={data:report};
        else body={data:{state:'done',progress:{completed:2,total:2}}};
        return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/intraday/replay`, {waitUntil:'domcontentloaded',timeout:60000});
      await page.locator('#dr-stocks > details').first().waitFor();
      const first=page.locator('#dr-stocks > details').first();
      await first.locator('summary').click();
      assert.match(await first.innerText(),/有通知发送成功记录/);
      assert.match(await first.innerText(),/缺少当日真实开盘价/);
      assert.match(await first.innerText(),/开盘 --.*昨收参考 29.92.*来源 未取得/);
      assert.match(await first.innerText(),/本次回放缺少证据，不能据此否定原始确认/);
      assert.match(await page.locator('#dr-summary').innerText(),/1 \/ 3/);
      assert.equal(await first.locator('img').count(),0);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
      assert.deepEqual(errors,[]);
      const second=page.locator('#dr-stocks > details').nth(1);
      await second.locator('summary').click();
      assert.match(await second.innerText(),/开盘 24.00.*昨收参考 23.25.*来源 eltdx按日归档/);
      const unknown=page.locator('#dr-stocks > details').nth(2);
      await unknown.locator('summary').click();
      assert.equal(await unknown.locator('p.text-emerald-300').count(),0);
      assert.match(await unknown.locator('p.text-amber-300').innerText(),/未见原始运行记录/);
      await page.locator('#dr-failures details > summary').click();
      assert.match(await page.locator('#dr-failures').innerText(),/未归档原始运行记录，不能据此判断是否触发或被拦截/);
      await page.screenshot({path:path.join(output,`replay-runtime-${width}.png`),fullPage:true});
      await page.close();
    }
    console.log('PASS: recorded delivery separated from replay, escaped reasons, desktop/mobile without page errors or overflow');
  } finally {
    if(browser)await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
