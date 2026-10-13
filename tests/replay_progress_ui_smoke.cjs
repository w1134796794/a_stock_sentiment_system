const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(require.resolve('playwright', {paths:[process.argv[2] || process.cwd()]}));
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'outputs/config-sync-ui');
const failure = {trade_date:'20260903',code:'000001',evidence:'minute_bars',reason:'下午分钟缺失'};
const run = (state, mode, progress, extra={}) => ({state,next:0,lines:['正在检查分钟证据'],paper_replay:{
  state,mode,progress,stage:progress.stage,start_date:'20260901',end_date:'20260907',
  started_epoch:Date.now()/1000-120,updated_epoch:Date.now()/1000,...extra}});
(async()=>{
  const server=http.createServer((req,res)=>{
    const file=req.url.startsWith('/static/css/app.css')?path.join(root,'web/static/css/app.css'):path.join(output,'portfolio.html');
    res.setHeader('Content-Type',file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true,channel:'chrome'});
    for(const width of [1440,390]) {
      const page=await browser.newPage({viewport:{width,height:950}});
      await page.route('https://**', route=>route.abort());
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      let rebuildBody;
      let status=run('running','incremental',{stage:'检查或补齐分钟证据',total_days:5,completed_days:2,trade_date:'20260903',code:'000001',stocks_completed:4,stocks_total:12});
      await page.route('**/api/**',route=>{
        const url=new URL(route.request().url());let result;
        if(url.pathname.endsWith('/replay/status'))result=status;
        else if(url.pathname==='/api/portfolio/rebuild' && route.request().method()==='POST') {
          rebuildBody=route.request().postDataJSON();
          status=run('running','rebuild',{stage:'运行分钟回放',completed:0,total:5,unit:'交易日'});
          result={ok:true,message:'已启动独立重建'};
        }
        else if(url.pathname.endsWith('/replay/checkpoint'))result={ok:true,data:{last_completed_date:'20260902',next_trade_date:'20260903'}};
        else if(url.pathname==='/api/portfolio/replay' && route.request().method()==='POST') {
          status=run('running','incremental',{stage:'准备逐日接力',total_days:5,completed_days:0});result={ok:true,message:'已启动'};
        }else if(url.pathname.endsWith('/accounts'))result={ok:true,data:[{account_key:'default',name:'模拟账户'},{account_key:'history_test',name:'历史账户'}]};
        else result={ok:true,data:{positions:[],trades:[],exit_signals:[],summary:{}}};
        return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
      });
      const url=`http://127.0.0.1:${server.address().port}/portfolio`;
      await page.goto(url);await page.locator('#replay-percent').getByText('40.0%',{exact:true}).waitFor();
      assert.match(await page.evaluate(()=>tradeQuantity({shares:140.25,metadata:{quantity_basis:'historical_reference_adjusted'}})),/140\.25.*参考价调整数量/);
      assert.doesNotMatch(await page.evaluate(()=>tradeQuantity({shares:100})),/参考价调整数量/);
      assert.equal(await page.locator('#replay-progress-bar').getAttribute('value'),'40');
      assert.equal(await page.locator('#start-replay').isDisabled(),true);
      assert.match(await page.locator('#replay-current').innerText(),/20260903.*000001/);
      await page.reload();await page.locator('#replay-percent').getByText('40.0%',{exact:true}).waitFor();
      await page.screenshot({path:path.join(output,`replay-progress-${width}.png`)});
      status=run('prefetching','rebuild',{stage:'补齐分钟行情',completed:3,total:10,unit:'证据项',trade_date:'20260903',code:'000001.SZ',
        optional_evidence_warnings:[{trade_date:'20260714',ts_code:'000004.SZ',scope:'sector_peer',reason:'参考样本分钟数据为空'}]});
      await page.evaluate(()=>pollReplay());
      await page.locator('#replay-count').getByText('3 / 10 证据项',{exact:true}).waitFor();
      assert.match(await page.locator('#replay-count').innerText(),/3 \/ 10 证据项/);
      assert.equal(await page.locator('#replay-progress-bar').getAttribute('value'),'30');
      assert.equal(await page.locator('#replay-peer-warnings').isVisible(),true);
      await page.locator('#replay-peer-warnings summary').click();
      assert.match(await page.locator('#replay-peer-rows').innerText(),/20260714.*000004.SZ.*参考样本/);
      assert.equal(await page.locator('#replay-evidence').isVisible(),false);
      await page.screenshot({path:path.join(output,`replay-peer-warning-${width}.png`)});
      status=run('running','rebuild',{stage:'运行分钟回放',completed:1,total:5,unit:'交易日',trade_date:'20260902'});
      await page.evaluate(()=>pollReplay());
      await page.locator('#replay-stage').getByText('运行分钟回放',{exact:true}).waitFor();
      assert.equal(await page.locator('#replay-progress-bar').getAttribute('value'),'20');
      status=run('blocked','rebuild',{stage:'回放证据不完整',completed:1,total:5,unit:'交易日'},
        {blocked_date:'20260903',error:'分钟证据不足',evidence_failures:[failure],finished_epoch:Date.now()/1000});
      await page.evaluate(()=>pollReplay());
      await page.locator('#replay-run-error').getByText('停在 20260903：分钟证据不足',{exact:true}).waitFor();
      assert.equal(await page.locator('#replay-progress-bar').getAttribute('value'),'20');
      assert.equal(await page.locator('#start-replay').isDisabled(),false);
      assert.match(await page.locator('#replay-run-error').innerText(),/20260903.*分钟证据不足/);
      await page.screenshot({path:path.join(output,`replay-blocked-${width}.png`)});
      await page.evaluate(()=>{replayPollPending=true;setTimeout(()=>{replayPollPending=false;},600);});
      await page.locator('#start-replay').click();
      await page.locator('#replay-stage').getByText('准备逐日接力',{exact:true}).waitFor();
      assert.equal(await page.locator('#start-replay').isDisabled(),true);
      status=run('done','incremental',{stage:'逐日提交完成',total_days:5,completed_days:5},
        {result:{last_completed_date:'20260907',fill_count:3},finished_epoch:Date.now()/1000});
      await page.evaluate(()=>pollReplay());
      await page.locator('#replay-run-state').getByText('已完成',{exact:true}).waitFor();
      assert.equal(await page.locator('#replay-progress-bar').getAttribute('value'),'100');
      await page.locator('#portfolio-account').selectOption('history_test');
      assert.equal(await page.locator('#start-replay').isDisabled(),true);
      assert.equal(await page.locator('#start-rebuild').isDisabled(),false);
      await page.locator('#replay-start').fill('20260901');
      await page.locator('#replay-end').fill('20260907');
      await page.locator('#rebuild-skip-candidates').check();
      page.on('dialog',dialog=>dialog.accept());
      await page.locator('#start-rebuild').click();
      await page.locator('#replay-stage').getByText('运行分钟回放',{exact:true}).waitFor();
      assert.equal(rebuildBody.missing_evidence_policy,'skip_candidates');
      assert.equal(await page.locator('#rebuild-skip-candidates').isDisabled(),true);
      const quality={policy:'skip_candidates',status:'incomplete',skipped_candidate_count:1,
        candidate_samples:4,covered_candidate_count:3,candidate_coverage_pct:75,
        skipped_candidates:[{...failure,scope:'candidate',action:'skipped'}],full_sample_metrics_valid:false};
      status=run('done','rebuild',{stage:'独立历史重建完成',completed:5,total:5,unit:'交易日'},
        {data_quality:quality,finished_epoch:Date.now()/1000});
      await page.evaluate(()=>pollReplay());
      await page.locator('#replay-quality-label').getByText('数据不完整 · 仅供研究',{exact:true}).waitFor();
      assert.match(await page.locator('#replay-quality-summary').innerText(),/跳过 1 \/ 4.*75.0%/);
      assert.match(await page.locator('#replay-quality-warning').innerText(),/不能代表完整候选样本/);
      await page.locator('#replay-skips-panel summary').click();
      assert.match(await page.locator('#replay-skips-rows').innerText(),/20260903.*000001.*分钟行情.*下午分钟缺失/s);
      await page.screenshot({path:path.join(output,`replay-skip-quality-${width}.png`)});
      status=run('done','rebuild',{stage:'独立历史重建完成',completed:5,total:5,unit:'交易日'},
        {data_quality:{...quality,costs_verified:false,cost_warning:'该历史批次缺少成交成本金额，账户手续费未验证；当前收益不是完整含费收益。'}});
      await page.evaluate(()=>pollReplay());
      await page.locator('#replay-quality-label').getByText('成本未验证 · 仅供研究',{exact:true}).waitFor();
      assert.match(await page.locator('#replay-quality-warning').innerText(),/手续费未验证/);
      await page.screenshot({path:path.join(output,`replay-cost-warning-${width}.png`)});
      await page.evaluate(()=>{
        document.querySelector('#panel-trades').classList.remove('hidden');
        document.querySelector('#trades-empty').classList.add('hidden');
        document.querySelector('#trades').innerHTML=tradeRow({code:'688286',name:'测试股份',action:'sell',trade_date:'20260702',trade_time:'09:31:00',price:90.319,shares:2519.622047,amount:227683,metadata:{quantity_basis:'historical_reference_adjusted'}});
      });
      await page.locator('#panel-trades').scrollIntoViewIfNeeded();
      if(width<500)await page.locator('#panel-trades .pf-table-wrap').evaluate(el=>{el.scrollLeft=400;});
      assert.match(await page.locator('#trades').innerText(),/2,519\.622047.*参考价调整数量/s);
      await page.locator('#panel-trades').screenshot({path:path.join(output,`replay-adjusted-quantity-${width}.png`)});
      assert.deepEqual(errors,[]);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
      await page.close();
    }
    console.log('Replay progress, refresh recovery, stages, blocked/done, duplicate prevention: desktop/mobile passed');
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
