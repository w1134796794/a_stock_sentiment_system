(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {auction_series:'竞价过程',opening_match:'09:25正式撮合',trade_batch:'成交明细',depth:'五档盘口',
    bars_raw:'不复权行情',bars_qfq:'前复权行情',bars_hfq:'后复权行情',corporate_actions:'权息',adjustment_factors:'复权系数',
    money_flow:'资金流',company_profile:'公司资料',announcements:'公告',finance_report:'财报',topics:'题材',security_status:'证券状态',
    ok:'有效',empty:'空结果',partial:'不完整',missing:'未采集',unsupported:'不支持',network_error:'网络异常',request_error:'请求异常',parse_error:'解析异常'};
  Object.assign(labels,{disabled:'已关闭',not_started:'未启动',inactive:'未运行',backpressure:'积压或磁盘不足',done:'已完成',running:'运行中',failed:'失败'});
  Object.assign(labels,{missing_collection_plan:'缺少当日采集计划',missing_board_plan:'缺少板块对照计划',
    unmapped:'未匹配通达信板块',requires_review:'候选映射待核验',fresh_receipt:'新鲜',stale:'已过期',
    not_in_collection_scope:'未纳入采集（映射未核验）'});
  const display = v => v === null || v === undefined ? '--' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  const table = (id,heads,rows) => {
    const node=$(id); if (!node) return;
    node.replaceChildren();
    if (!rows.length) { node.textContent='暂无已采集数据。'; return; }
    const tab=document.createElement('table'); tab.className='w-full text-xs text-left';
    const header=tab.createTHead().insertRow();
    heads.forEach(head=>{const th=document.createElement('th');th.textContent=head;th.className='p-2 text-slate-400';header.append(th);});
    const body=tab.createTBody();
    rows.forEach(values=>{const tr=body.insertRow();values.forEach(v=>{const td=tr.insertCell();if(v instanceof Node)td.append(v);else td.textContent=display(v);td.className='p-2 border-t border-slate-800 break-all';});});
    node.append(tab);
  };
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const amount = value => !numeric(value)?'--':value>=10000?`${(value/10000).toFixed(2)}万元`:`${value.toFixed(2)}元`;
  const pct = value => numeric(value)?`${(value*100).toFixed(1)}%`:'--';
  function summaryText(row) {
    const x=row.summary;
    if(!x || !Object.keys(x).length) return '暂无可用摘要';
    if(row.kind==='trade_batch') return `本批新增 ${x.count ?? '--'} 笔 · 估算金额 ${amount(x.amount_estimated_yuan)} · 买方向占比 ${pct(x.buy_amount_share)}`;
    if(row.kind==='opening_match') return `撮合价 ${amount(x.price)} · 撮合额 ${amount(x.amount_yuan)} · ${x.volume_hand ?? '--'} 手`;
    if(row.kind==='auction_series') return `观察点 ${x.point_count ?? '--'} 个 · 尾段价格变化 ${numeric(x.tail_price_change_pct)?x.tail_price_change_pct.toFixed(2)+'%':'--'} · 拟撮合量变化 ${x.matched_volume_change_hand ?? '--'} 手`;
    if(row.kind==='depth') return `价差 ${amount(x.spread_yuan)} · 买五档 ${x.bid_depth_hand ?? '--'} 手 / 卖五档 ${x.ask_depth_hand ?? '--'} 手 · 失衡 ${pct(x.imbalance)}`;
    return '仅观察';
  }
  async function get(url) {
    const r=await fetch(url,{credentials:'same-origin'});const value=await r.json();
    if (!r.ok || value.ok===false) throw new Error(value.message || value.reason || '证据暂不可读');
    return value;
  }
  const date = () => $('me-date').value;
  const code = () => $('me-code').value;
  const params = () => new URLSearchParams({market_date:date()});
  let generation=0, offset=0, total=0, boardRequest=0, rawRequest=0;
  const rawLabels = {event_time:'行情时间',received_at:'接收时间',time_precision:'时间精度',price:'价格（元）',
    matched_volume_hand:'拟撮合量（手）',matched_amount_estimated_yuan:'拟撮合额（估算元）',
    unmatched_volume_hand:'未撮合量（手）',unmatched_direction_raw:'未撮合方向（原始）',event_kind:'事件类型',
    trade_time:'成交时间',time:'时间',volume_hand:'成交量（手）',amount_yuan:'成交额（元）',
    amount_estimated_yuan:'成交额（估算元）',side:'方向',code:'代码',name:'名称',source:'来源',
    open:'开盘价',high:'最高价',low:'最低价',close:'收盘价',volume:'成交量（原口径）',amount:'成交额（原口径）'};
  function rawTable(rows) {
    const keys=Array.from(new Set(rows.flatMap(row=>Object.keys(row && typeof row==='object'?row:{value:row}))));
    table('me-raw',[...keys.map(key=>rawLabels[key]||key),'原始记录'],rows.map(source=>{
      const row=source && typeof source==='object'?source:{value:source};
      const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
      summary.textContent='查看';pre.textContent=JSON.stringify(source,null,2);pre.className='whitespace-pre-wrap break-all';details.append(summary,pre);
      return [...keys.map(key=>{
        const value=row[key];
        return numeric(value) && !Number.isInteger(value)?value.toLocaleString('zh-CN',{useGrouping:false,maximumFractionDigits:/price|open|high|low|close/.test(key)?4:2}):value;
      }),details];
    }));
    const tab=$('me-raw').querySelector('table');
    if(tab){tab.style.minWidth=`${Math.max(720,keys.length*120)}px`;tab.querySelectorAll('th').forEach(th=>{th.style.position='sticky';th.style.top='0';th.style.background='#0f172a';});}
  }
  function resetRaw() {
    ++rawRequest;offset=0;total=0;
    if($('me-raw')){$('me-raw').textContent='请选择数据类型并读取。';$('me-page').textContent='';$('me-prev').disabled=true;$('me-next').disabled=true;}
  }
  async function boards() {
    const id=++boardRequest,selected=date();
    $('me-board-status').textContent='正在读取板块对照…';$('me-boards').replaceChildren();
    try {
      const r=await get(`/api/market-evidence/boards?market_date=${encodeURIComponent(selected)}`);
      if(id!==boardRequest || selected!==date())return;
      $('me-board-status').textContent=(r.rows||[]).length?'当日冻结板块映射及最新热缓存；过期或未采集不等于历史无归档。此对照不代表正式买点确认。':(r.reason||labels[r.status]||'当日没有板块对照条目。');
      table('me-boards',['同花顺板块','映射状态','通达信板块代码','报价状态','涨跌幅','接收时间','成员覆盖 / 上涨比例'],(r.rows||[]).flatMap(x=>{
        const quotes=x.tdx_quotes?.length?x.tdx_quotes:[{}];
        return quotes.map(q=>[x.ths_name,x.score_eligible?'映射已核验':labels[x.status]||x.status,
          q.code||'--',labels[q.status]||q.status||'未采集',numeric(q.change_pct)?`${q.change_pct.toFixed(2)}%`:'--',q.received_at,
          `${pct(x.ths_breadth?.coverage)} / ${pct(x.ths_breadth?.advancing_ratio)}`]);
      }));
    }catch(e){if(id===boardRequest && selected===date())$('me-board-status').textContent=`板块证据读取失败：${e.message}；请检查当日冻结计划和证据存储。`;}
  }
  function curve(points) {
    const svg=$('me-auction-chart');svg.replaceChildren();svg.hidden=!points.length;
    const valid=points.filter(p=>Number.isFinite(p.price));if (!valid.length) return;
    const prices=valid.map(p=>p.price),lo=Math.min(...prices),range=Math.max(...prices)-lo || 1;
    const line=document.createElementNS('http://www.w3.org/2000/svg','polyline');
    line.setAttribute('points',valid.map((p,i)=>`${10+i*580/Math.max(1,valid.length-1)},${125-(p.price-lo)*110/range}`).join(' '));
    line.setAttribute('fill','none');line.setAttribute('stroke','#34d399');line.setAttribute('stroke-width','2');svg.append(line);
  }
  async function load() {
    const gen=++generation;$('me-status').textContent='正在读取已采集证据…';
    try {
      const r=await get(`/api/market-evidence/stock/${encodeURIComponent(code())}?${params()}`);
      if (gen!==generation) return;
      $('me-status').textContent=r.in_collection_scope?'该股票在当日冻结采集名单中。':'该股票不在当日冻结采集名单中；不代表停牌或退市。';
      table('me-live',['证据','状态','新鲜度','接收时间','摘要'],r.live.map(x=>[labels[x.kind],labels[x.status]||x.status,x.is_stale===null?'--':x.is_stale?'已过期':'新鲜',x.received_at,summaryText(x)]));
      table('me-research',['数据','状态','抓取时间','记录数','口径 / 资料摘录'],r.research.map(x=>[labels[x.kind],labels[x.status]||x.status,x.captured_at,x.row_count,x.preview||`${x.period || '--'} / ${x.adjust || '仅观察'}`]));
      const buys=r.depth_levels?.buy_levels||[],sells=r.depth_levels?.sell_levels||[];
      table('me-depth',['档位','买价（元）','买量（手）','卖价（元）','卖量（手）'],Array.from({length:Math.max(buys.length,sells.length)},(_,i)=>[i+1,buys[i]?.price,buys[i]?.volume_hand,sells[i]?.price,sells[i]?.volume_hand]));
      curve(r.auction_points || []);
    } catch(e) {if(gen===generation) $('me-status').textContent=e.message;}
  }
  const bind = (id,fn) => {if($(id)) $(id).addEventListener('click',async()=>{try{await fn();}catch(e){$('me-status').textContent=e.message;}});};
  $('me-form').addEventListener('submit',e=>{e.preventDefault();resetRaw();load();void boards();});
  $('me-date').addEventListener('change',()=>{++generation;resetRaw();void boards();});
  $('me-code').addEventListener('change',()=>{++generation;resetRaw();});
  bind('me-boards-button',boards);
  bind('me-diagnostics-button',async()=>{
    const r=await get(`/api/admin/market-evidence/diagnostics?${params()}`);
    $('me-diagnostics').textContent=`名单 ${r.scope_count} 只；待归档 ${r.pending?.count ?? '--'} 条；采集状态 ${labels[r.collector?.status] || r.collector?.status || '暂无'}`;
    table('me-jobs',['补数任务','状态','更新时间','任务数'],r.jobs.map(x=>[x.job_id,x.state,x.updated_at,x.tasks.length]));
  });
  async function raw() {
    if(!/^\d{8}$/.test(date())||!/^\d{6}$/.test(code()))throw new Error('请先填写行情日和六位股票代码');
    const id=++rawRequest,selectedDate=date(),selectedCode=code(),selectedOffset=offset;
    const kind=$('me-raw-kind').value;
    const live=kind.startsWith('live:');
    const path=live?`/api/admin/market-evidence/raw/${encodeURIComponent(code())}/${kind.slice(5)}`:`/api/admin/market-evidence/research/${encodeURIComponent(code())}/${kind}`;
    $('me-raw').textContent='正在读取…';$('me-prev').disabled=true;$('me-next').disabled=true;
    let r;
    try{r=await get(`${path}?offset=${selectedOffset}&limit=20&${params()}`);}
    catch(e){if(id===rawRequest){$('me-raw').textContent=`读取失败：${e.message}`;$('me-page').textContent='';}return;}
    if(id!==rawRequest || date()!==selectedDate || code()!==selectedCode || kind!==$('me-raw-kind').value)return;
    total=r.total;rawTable(r.rows||[]);$('me-page').textContent=total?`${offset+1}—${Math.min(offset+(r.rows||[]).length,total)} / ${total}`:'0 / 0';
    $('me-prev').disabled=offset===0;$('me-next').disabled=offset+20>=total;
  }
  bind('me-raw-button',async()=>{offset=0;await raw();});
  if($('me-raw-kind'))$('me-raw-kind').addEventListener('change',resetRaw);
  bind('me-prev',async()=>{offset=Math.max(0,offset-20);await raw();});
  bind('me-next',async()=>{if(offset+20<total){offset+=20;await raw();}});
  bind('me-experiments-button',async()=>{
    const r=await get('/api/admin/market-evidence/experiments');
    table('me-experiments',['报告','日期数','完整日','A确认','C保留','无法判定','经济指标'],r.rows.map(x=>[x.report_id,x.day_count,x.complete_days,x.A_count,x.C_count,x.unknown_count,x.profitability_measured?'已导入逐笔回测，非组合收益':'尚未测量收益']));
  });
  const query=new URLSearchParams(location.search);if(/^\d{8}$/.test(query.get('market_date')||''))$('me-date').value=query.get('market_date');
  if(/^\d{6}$/.test(query.get('code')||'')){$('me-code').value=query.get('code');load();}
  resetRaw();void boards();
})();
