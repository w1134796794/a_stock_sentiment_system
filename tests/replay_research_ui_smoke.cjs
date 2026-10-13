const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require(require.resolve('playwright',{paths:[process.argv[2]||process.cwd()]}));
const root=path.resolve(__dirname,'..'),output=path.join(root,'outputs/analog-ui');
const id='a'.repeat(32),labels=['原34%三仓','固定风险','持续确认','候选前半组'];
const report={experiment_id:id,kind:'replay_research',start:'20260701',end:'20261009',status:'incomplete',
  data_hash:'hash-data',configuration_hash:'hash-config',implementation_hash:'hash-code',spec:{fixed_risk_per_trade:.005},
  results:Object.fromEntries(['baseline','fixed_risk','timing','selection'].map((arm,i)=>[arm,{name:labels[i],changed_variable:'仅对照变量',
    complete:i>0,summary:{days:60,return_pct:-15+i*4,max_drawdown_pct:20-i*4,worst_day_pct:-4,drawdown_duration_days:20,
      max_consecutive_losses:4,closed_trades:8,win_rate_pct:25,fees:9000},validation_closed:3,evidence_status:'insufficient_evidence',
    nav:[{total_value:1000000},{total_value:850000+i*40000}],blocked:i===0?[{trade_date:'20260901',code:'000001',evidence:'minute_bars',reason:'缺少下午分钟'}]:[],data_quality:{}}])),
  folds:[{train_start:'20260701',train_end:'20260811',validation_start:'20260819',validation_end:'20260901',selected_arm:null,
    validation:{fixed_risk:{account:{return_pct:-1,max_drawdown_pct:2,closed_trades:3},groups:[{strategy_id:'weak_to_strong',regime:'退潮',candidates:10,confirmed:5,filled:3,closed:2,net_pnl:-100,win_rate_pct:50}]}}}]};
const qualityArms=['baseline','fixed_risk','true_breakout','confirmed_sustain','confirmed_pullback','overnight_risk','strategy_quality','exit_failure'];
const qualityNames=['原34%三仓','固定风险','真突破','持续确认','回踩确认','隔夜风险','分策略选股','失效退出'];
const qualityReport=structuredClone(report);
qualityReport.spec={suite:'confirmation_quality',fixed_risk_per_trade:.005,optimization:{sustain_minutes:2}};
qualityReport.results=Object.fromEntries(qualityArms.map((arm,i)=>[arm,{...structuredClone(report.results.fixed_risk),name:qualityNames[i]}]));
qualityReport.paired_comparisons=qualityArms.slice(1).map((arm,i)=>({arm,control:qualityArms[i],complete:true,
 changed_variable:'预先固定单变量',return_delta_pp:1,drawdown_delta_pp:-1,closed_trades:8,control_closed_trades:9}));
qualityReport.folds[0].selection_status='no_positive_training_edge_or_insufficient_samples';
qualityReport.results.baseline.complete=false;
qualityReport.results.baseline.blocked=report.results.baseline.blocked;
const refinementReport=structuredClone(qualityReport);
refinementReport.spec.suite='return_refinement';
refinementReport.unseen_validation_required=true;
const refinementArms=['strategy_quality','reversal_regime','pullback_quality','exit_failure','exit_patient'];
refinementReport.results=Object.fromEntries(refinementArms.map((arm,i)=>[arm,{...structuredClone(report.results.fixed_risk),name:i?arm:'策略质量原对照'}]));
refinementReport.results.strategy_quality.blocked=report.results.baseline.blocked;
refinementReport.paired_comparisons=refinementArms.slice(1).map((arm)=>({arm,control:arm==='exit_patient'?'exit_failure':'strategy_quality',
 complete:true,changed_variable:'预先固定单变量',return_delta_pp:1,drawdown_delta_pp:-1,closed_trades:8,control_closed_trades:9}));
const tuningReport=structuredClone(refinementReport);
tuningReport.spec.suite='in_sample_tuning';
const tuningArms=['strategy_quality','combined_quality','combined_patient','combined_sustain3','combined_weak_regime'];
tuningReport.results=Object.fromEntries(tuningArms.map((arm,i)=>[arm,{...structuredClone(report.results.fixed_risk),name:i?arm:'策略质量原对照'}]));
tuningReport.results.strategy_quality.blocked=report.results.baseline.blocked;
tuningReport.paired_comparisons=tuningArms.slice(1).map(arm=>({...refinementReport.paired_comparisons[0],arm,control:'strategy_quality'}));
const sizingReport=structuredClone(qualityReport);
sizingReport.spec={suite:'position_risk',fixed_risk_per_trade:.005,optimization:{low_risk:.0025,standard_risk:.005,high_risk:.0075}};
sizingReport.unseen_validation_required=true;
sizingReport.results=Object.fromEntries(['baseline','fixed_risk','tiered_risk'].map((arm,i)=>[arm,{...structuredClone(report.results.fixed_risk),name:['原34%三仓','固定风险仓位','分档动态风险'][i],tail_metrics:{daily_expected_shortfall_95_pct:-1.2,gap_stop_count:10,gap_stop_net_pnl:-12000,sizing_tier_counts:{low:38,standard:0,high:0}}}]));
sizingReport.results.baseline.blocked=report.results.baseline.blocked;
sizingReport.paired_comparisons=[{...refinementReport.paired_comparisons[0],arm:'fixed_risk',control:'baseline'},{...refinementReport.paired_comparisons[0],arm:'tiered_risk',control:'fixed_risk'}];
(async()=>{
 const server=http.createServer((req,res)=>{const file=req.url.startsWith('/static')?path.join(root,'web/static/css/app.css'):path.join(output,'evaluation.html');res.setHeader('Content-Type',file.endsWith('css')?'text/css':'text/html');res.end(fs.readFileSync(file));});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;
 try{browser=await chromium.launch({headless:true,channel:'chrome'});
  for(const suite of ['legacy','confirmation_quality','return_refinement','in_sample_tuning','position_risk'])for(const width of [1440,390]){
   const activeReport=suite==='legacy'?report:suite==='confirmation_quality'?qualityReport:suite==='return_refinement'?refinementReport:suite==='position_risk'?sizingReport:tuningReport;
   const page=await browser.newPage({viewport:{width,height:1100}});const errors=[];let running=false;
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/api/operations/**',async route=>{const req=route.request(),url=new URL(req.url());let body={};
    if(url.pathname==='/api/operations/experiments')body={rows:url.searchParams.get('kind')==='replay'?[{experiment_id:id,frozen_at:'20261010'}]:[]};
    else if(url.pathname==='/api/operations/replay-research/start'){const payload=req.postDataJSON();if(payload.experiment_id)assert.equal(payload.experiment_id,id);else assert.equal(payload.suite,suite);running=true;body={ok:true};}
    else if(url.pathname==='/api/operations/replay-research/status')body=running?{state:'running',experiment_id:id,stage:'固定风险',progress:{completed:5,total:20,trade_date:'20260801'}}:{};
    else body={report:activeReport};
    await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
   });
   await page.goto(`http://127.0.0.1:${server.address().port}/strategy-evaluation`);
   await page.locator('#ev-kind').selectOption('replay');
   if(suite!=='position_risk')await page.locator('#ev-suite').selectOption(suite);
   await page.locator('#ev-research-arms').getByText(`${['return_refinement','in_sample_tuning'].includes(suite)?'策略质量原对照':'原34%三仓'} · 仅对照变量`,{exact:true}).waitFor();
   assert.match(await page.locator('#ev-research-status').innerText(),['return_refinement','in_sample_tuning'].includes(suite)?/仍须未见样本验证/:/数据不完整/);
   if(suite==='in_sample_tuning')assert.match(await page.locator('#ev-research-status').innerText(),/样本内拟合/);
   assert.match(await page.locator('#ev-research-folds').innerText(),suite==='legacy'?/训练样本不足/:/无正期望证据或样本不足/);
   assert.equal(await page.locator('#ev-research-legend > span').count(),suite==='legacy'?4:suite==='confirmation_quality'?8:suite==='position_risk'?3:5);
   assert.equal(await page.locator('#ev-sizing-tail').isVisible(),suite==='position_risk');
   if(suite==='position_risk'){
    assert.equal(await page.locator('#ev-suite').inputValue(),'position_risk');
    assert.match(await page.locator('#ev-research-status').innerText(),/样本不足不加仓/);
    assert.match(await page.locator('#ev-sizing-tail-rows').innerText(),/38 \/ 0 \/ 0 \/ 0/);
   }
   if(suite!=='legacy'){
    assert.equal(await page.locator('#ev-pair-rows tr').count(),suite==='confirmation_quality'?7:suite==='position_risk'?2:4);
    assert.match(await page.locator('#ev-pair-rows').innerText(),/个百分点/);
   }
   await page.locator('#ev-research details summary').click();
   assert.match(await page.locator('#ev-research-evidence').innerText(),/20260901.*000001.*缺少下午分钟/);
   await page.screenshot({path:path.join(output,`research-${suite}-${width}.png`),fullPage:true});
   const nonblank=await page.locator('#ev-research-curve').evaluate(c=>Array.from(c.getContext('2d').getImageData(0,0,c.width,c.height).data).filter((v,i)=>i%4===3&&v>0).length);
   assert(nonblank>100);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
   if(suite==='legacy'||suite==='position_risk')await page.locator('#ev-run').click();
   else{
    await page.locator('#ev-start').fill('2026-07-01');
    await page.locator('#ev-end').fill('2026-10-09');
    await page.locator('#ev-freeze').click();
   }
   await page.locator('#ev-status').getByText('固定风险 · 20260801 · 5/20（当前阶段）',{exact:true}).waitFor();
   assert.equal(await page.locator('#ev-run').isDisabled(),true);
   assert.equal(await page.locator('#ev-freeze').isDisabled(),true);
   assert.equal(await page.locator('#ev-research-progress').getAttribute('value'),'25');
   await page.locator('#ev-kind').selectOption('strategy');
   assert.equal(await page.locator('#ev-research').isVisible(),false);
   assert.deepEqual(errors,[]);await page.close();
  }
  console.log('Frozen research comparison, missing evidence, progress, charts: desktop/mobile passed');
 }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
