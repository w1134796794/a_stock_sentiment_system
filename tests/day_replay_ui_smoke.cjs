const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(require.resolve('playwright', {paths:[process.argv[2]||process.cwd()]}));
const root=path.resolve(__dirname,'..'), output=path.join(root,'outputs/intraday-replay-ui');
const id='a'.repeat(32);
const request={market_date:'20261009',until:'10:30:00',codes:['002614'],profile:'leader_pool',lookback:10};
const report={run_id:id,market_date:'20261009',candidate_date:'20261008',until:'10:30:00',profile:'leader_pool',
  confirmed_stocks:1,complete:false,input_hash:'1234567890abcdef'.repeat(4),code_hash:'abcdef1234567890'.repeat(4),
  input_basis:'research_candidates_not_frozen',live_runtime_verified:false,
  limitations:['分钟诊断，不构造历史租约，不发送微信、不修改账户'],
  failures:[{date:'20261009',code:'002614',evidence:'sector_minutes',reason:'缺少板块观测证据'}],
  comparison:{comparable:true,reason:'同输入代码回归',changes:[{code:'002614',before:{status:'observe',reason:'旧代码阻断'},after:{status:'confirmed',reason:'规则通过',first_confirm_time:'09:46:00'}}]},
  stocks:[{code:'002614',name:'奥佳华',status:'observe',first_confirm_time:'09:46:00',first_evaluated_at:'09:47:00',reason:'<img src=x onerror=alert(1)> 仅分钟诊断',
    transitions:[{evaluated_at:'09:47:00',signal_status:'confirmed',confirm_time:'09:46:00',entry_time:'',reason:'满足分钟确认条件',data_status:'minute_fallback',sector_detail:{coverage:.75,confirmation_path:'open_rising_3m',open_gain_pct:.6,three_minute_change_pct:.2,trend_coverage:.75}}]}]};
report.board_shadow={status:'missing_board_snapshots',market_date:'20261009',until:'10:30:00',rows:[],timeline:[],failures:[{evidence:'board_snapshot_missing',reason:'No historical board snapshot'}],reconciliation:{}};
const shadow={...report.board_shadow,status:'shadow_only',synthetic:true,rows:[{code:'sh880001',status:'fresh_receipt',received_at:'2026-10-09T09:31:10+08:00',receipt_age_seconds:0,change_pct:2,open_gain_pct:1,observed_window_seconds:10,max_gap_seconds:10}],failures:[],timeline:[{code:'sh880001',evaluated_at:'2026-10-09T09:31:10+08:00',replayed:{status:'fresh_receipt'},captured:null,comparison_state:'missing_live_assessment'}],reconciliation:{missing_live_assessment:1}};
(async()=>{
  const server=http.createServer((req,res)=>{
    let file;
    if(req.url.startsWith('/static/intraday-replay.js'))file=path.join(root,'web/static/intraday-replay.js');
    else if(req.url.startsWith('/static/css/app.css'))file=path.join(root,'web/static/css/app.css');
    else file=path.join(output,'page.html');
    res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  let browser;
  try{
    browser=await chromium.launch({headless:true,channel:'chrome'});
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:950}}),errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('https://**',r=>r.abort());
      let state={run_id:id,request,state:'running',started_epoch:Date.now()/1000,progress:{stage:'逐分钟确认回放',completed:1,total:4,code:'002614'}},posted,activeReport=report;
      await page.route('**/api/intraday-replay**',route=>{
        const url=new URL(route.request().url());let data;
        if(url.pathname.startsWith('/api/intraday-replay/board-shadow/'))data={ok:true,data:shadow};
        else if(url.pathname.endsWith('/cancel')){state={...state,state:'cancelled',error:'用户取消诊断回放'};data={ok:true};}
        else if(route.request().method()==='POST'){
          posted=route.request().postDataJSON();state={...state,state:'running'};
          delete state.error;
          if(posted.replay_mode==='archived_snapshot')activeReport={...report,replay_mode:'archived_snapshot',input_basis:'archived_receipt_time',evaluation_count:2,archive_event_counts:{snapshot:2},stocks:report.stocks.map(s=>({...s,snapshot_count:2,last_evaluated_at:'2026-10-09T10:30:00+08:00',gaps:[{seconds:12}],transitions:s.transitions.map(t=>({...t,trigger_source:'eltdx_snapshot',condition_checks:{latest:{minute:'09:46:03',checks:{vwap:true,breakout:true,sector:true}}}}))}))};
          data={ok:true,run_id:id};
        }
        else if(url.pathname==='/api/intraday-replay')data={ok:true,runs:[state]};
        else data={ok:true,data:url.searchParams.has('report')?activeReport:state};
        return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
      });
      const url=`http://127.0.0.1:${server.address().port}/intraday/replay`;
      await page.goto(url);await page.getByText('当前阶段 25.0%',{exact:false}).waitFor();
      assert.equal(await page.locator('#dr-start').isDisabled(),true);
      await page.reload();await page.getByText('当前阶段 25.0%',{exact:false}).waitFor();
      state={...state,state:'done',progress:{stage:'归档快照驱动回放',completed:2,total:2,code:'002614'}};
      await page.locator('#dr-stocks summary').getByText('回放结束：观察',{exact:true}).waitFor();
      assert.match(await page.locator('#dr-board-report').innerText(),/缺少历史板块快照/);
      await page.locator('#dr-board-start').click();
      await page.locator('#dr-board-report').getByText('sh880001',{exact:true}).first().waitFor();
      assert.match(await page.locator('#dr-board-report').innerText(),/合成样本/);
      assert.match(await page.locator('#dr-board-report').innerText(),/非正式买点/);
      await page.locator('#dr-stocks summary').click();
      assert.equal(await page.locator('#dr-stocks img').count(),0);
      assert.match(await page.locator('#dr-stocks').innerText(),/09:46:00/);
      assert.match(await page.locator('#dr-stocks').innerText(),/开盘走强/);
      assert.match(await page.locator('#dr-stocks').innerText(),/0\.60% \/ 0\.20%/);
      assert.match(await page.locator('#dr-limits').innerText(),/实时推送验证：未验证/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(output,`day-replay-${width}.png`),fullPage:true});
      await page.locator('#dr-stocks').screenshot({path:path.join(output,`day-replay-sector-${width}.png`)});
      await page.evaluate(()=>document.getElementById('main-scroll').scrollTop=0);
      await page.screenshot({path:path.join(output,`day-replay-controls-${width}.png`)});
      await page.locator('#dr-baseline').selectOption(id);
      assert.equal(await page.locator('#dr-codes').inputValue(),'002614');
      await page.locator('#dr-start').click();
      await page.locator('#dr-state').getByText('运行中',{exact:true}).waitFor();
      assert.equal(posted.baseline_id,id);
      await page.locator('#dr-cancel').click();await page.locator('#dr-state').getByText('已取消',{exact:true}).waitFor();
      assert.equal(await page.locator('#dr-start').isDisabled(),false);
      await page.locator('#dr-baseline').selectOption('');
      await page.locator('#dr-fetch').check();
      await page.locator('#dr-mode').selectOption('archived_snapshot');
      assert.equal(await page.locator('#dr-fetch').isChecked(),false);
      assert.equal(await page.locator('#dr-fetch').isDisabled(),true);
      assert.equal(await page.locator('#dr-lookback').isDisabled(),true);
      await page.locator('#dr-until').fill('10:30:03');
      await page.locator('#dr-start').click();
      await page.locator('#dr-state').getByText('运行中',{exact:true}).waitFor();
      assert.equal(posted.replay_mode,'archived_snapshot');
      assert.equal(posted.fetch_missing,false);
      assert.equal(posted.until,'10:30:03');
      state={...state,state:'done'};
      await page.locator('#dr-summary').getByText('归档快照回放曾确认',{exact:false}).waitFor();
      await page.locator('#dr-stocks > details').first().evaluate(el=>el.open=true);
      assert.match(await page.locator('#dr-stocks').innerText(),/归档快照 2 · 空窗 1 段/);
      assert.match(await page.locator('#dr-stocks').innerText(),/快照主判断/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(output,`snapshot-replay-${width}.png`),fullPage:true});
      await page.locator('#dr-stocks').screenshot({path:path.join(output,`snapshot-replay-stocks-${width}.png`)});
      assert.deepEqual(errors,[]);
      await page.close();
    }
    console.log('day replay UI: desktop/mobile, progress restore, report, baseline, cancel, escaping, snapshot mode passed');
  }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
