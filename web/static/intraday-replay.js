(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {queued:'等待运行',running:'运行中',done:'已完成',failed:'失败',cancelled:'已取消',interrupted:'任务中断'};
  const signalLabels = {confirmed:'分钟规则确认',observe:'观察',cancelled:'否决',unfilled:'无法成交',missing_evidence:'缺证据'};
  const runtimeLabels = {sent:'有通知发送成功记录',attempt_not_sent:'有通知尝试，未见发送成功记录',confirmed_no_delivery_record:'生产确认，未见通知流水',observation_confirmed:'观察视图曾确认',veto_recorded:'有门禁否决记录',recorded_without_confirmation:'有运行记录，未见确认',no_runtime_evidence:'未见原始运行记录'};
  runtimeLabels.notification_queued='确认通知已入队，尚未见发送结果';
  let runId = '', timer, pending = false, savedRuns = [];
  async function api(url, body) {
    const response = await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
    const value = await response.json();
    if (!response.ok || value.ok === false) throw new Error(value.message || '请求失败');
    return value;
  }
  function table(heads, rows, className='') {
    return `<div class="dr-table ${esc(className)}"><table><thead><tr>${heads.map(v=>`<th>${esc(v)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }
  function missingList(values) {
    const list=values||[];
    return list.slice(0,8).join(', ') + (list.length>8?` 等 ${list.length} 项`:'');
  }
  function sectorPath(detail) {
    if (detail?.assessed === false) return '尚未检查';
    return {red_breadth:'红盘广度',open_rising_3m:'开盘走强'}[detail?.confirmation_path] || '--';
  }
  function percent(value) {
    return value === null || value === undefined ? '--' : `${Number(value).toFixed(2)}%`;
  }
  function runtimeDetail(stock) {
    const d = stock.runtime_diagnostic;
    if (!d) return '';
    const last = (d.notification_attempts || []).at(-1);
    const tone = d.state === 'sent' ? 'text-emerald-300' : 'text-amber-300';
    const observed = d.last_observation;
    const difference = {replay_missing_evidence:'实盘记录与回放分开：本次回放缺少证据，不能据此否定原始确认。',replay_result_differs:'实盘记录与当前规则回放不一致，需核对代码、配置与输入版本。',replay_confirmed_without_runtime_confirmation:'回放曾确认，实时未见生产确认记录；不等于通知渠道发送失败。',replay_confirmed_without_runtime_evidence:'回放曾确认，但缺少原始运行证据，不能判定实盘是否漏推。'}[d.replay_discrepancy] || '';
    return `<p class="text-sm ${tone} mt-2">当日原始运行：${esc(runtimeLabels[d.state]||d.state)} · 确认 ${esc(d.confirm_times.join('、')||'--')} · 范围 ${esc(d.profiles.join('、')||'--')}</p>${observed?`<p class="text-sm text-amber-300 mt-2">最近实际观测 ${esc(observed.observed_at)} · ${esc(observed.reason||'--')} · 行情接收 ${esc(observed.quote_received_at||'--')}${observed.quote_stale_seconds==null?'':` · 报价年龄 ${esc(Number(observed.quote_stale_seconds).toFixed(1))}秒`}</p>`:''}${last?`<p class="text-sm text-slate-400 mt-2">最近通知尝试 ${esc(last.occurred_at)} · ${esc(last.status)} · ${esc(last.reason||'--')}</p>`:''}${d.veto_reasons.length?`<p class="text-sm text-amber-300 mt-2">原始门禁记录：${esc(d.veto_reasons.join('；'))}</p>`:''}${difference?`<p class="text-sm text-amber-300 mt-2">${esc(difference)}</p>`:''}`;
  }
  function confirmationComparison(stock) {
    const d=stock.runtime_diagnostic?.confirmation_comparison;
    const neighbors=[d?.before,d?.after].filter(Boolean);
    const conditionNames={previous_close:'收复昨收',vwap:'VWAP',breakout:'突破',amount_pace:'成交进度值',amount_pace_allowed:'成交进度范围',sector:'板块同步',protection:'结构保护',leader:'核心身份',turnover:'增量成交',local_volume_ratio:'快照局部量比',local_volume_allowed:'快照放量',tradable:'可成交报价',price_quality:'价格质量'};
    const rows=(stock.transitions||[]).flatMap(t=>{
      const point=t.condition_checks?.latest;
      if(!point)return [];
      return [[t.evaluated_at,point.minute,Object.entries(point.checks||{}).map(([key,value])=>`${conditionNames[key]||key}: ${value===null?'未评估':value===true?'通过':value===false?'未通过':value}`).join('；')]];
    });
    const timing=stock.runtime_diagnostic?.last_observation?.evaluation_timing;
    const selection=stock.runtime_diagnostic?.last_observation?.condition_checks;
    const source=selection?.selected_source;
    const fallbackLabels={snapshot_sequence_missing:'连续快照不足',snapshot_timestamp_missing:'缺少快照时间',snapshot_gap:'快照采集空窗',snapshot_stale:'快照过期',snapshot_invalid:'快照质量或来源不符',snapshot_identity_mismatch:'快照股票不符',snapshot_cumulative_fields_missing:'缺少真实累计量额',snapshot_cumulative_fields_invalid:'累计量额无效',snapshot_cumulative_regressed:'累计量额回退',snapshot_session_reference_mismatch:'开盘或昨收参考不一致',snapshot_vwap_invalid:'快照VWAP无效',structural_strategy_uses_minute_confirmation:'结构策略保留分钟确认',historical_minute_diagnostic_without_snapshots:'历史报告仅含分钟证据'};
    return `<p class="text-sm text-slate-400 mt-2">回放曾确认 ${esc(stock.first_confirm_time||'--')} · 回放结束状态 ${esc(signalLabels[stock.closing_status||stock.status]||stock.status)}</p>`+
      (source?`<p class="text-sm text-slate-400 mt-2">实际判断来源 ${esc(source==='eltdx_snapshot'?'eltdx快照主判断':'分钟兜底')}${selection.fallback_reason?` · ${esc(fallbackLabels[selection.fallback_reason]||selection.fallback_reason)}`:''}</p>`:'')+
      (timing?.finished_at?`<p class="text-sm text-slate-400 mt-2">逐股判定完成 ${esc(timing.finished_at)} · 判定 ${esc(timing.duration_ms)}毫秒 · 完成时报价年龄 ${esc(timing.quote_age_at_completion_seconds??'--')}秒</p>`:'')+
      (neighbors.length?`<details><summary>确认附近实际观测（非同一秒还原）</summary>${table(['实际观测','距回放观测秒数','状态','原因','板块宽度覆盖','历史配对覆盖'],neighbors.map(o=>[o.observed_at,o.offset_seconds,o.status,o.reason,percent((o.sector_detail?.current_coverage??o.sector_detail?.coverage??0)*100),percent((o.sector_detail?.trend_coverage??0)*100)]))}</details>`:'')+
      (rows.length?`<details><summary>逐分钟条件明细</summary>${table(['回放观测','行情分钟','条件'],rows,'dr-condition-table')}</details>`:'');
  }
  function openingDetail(stock) {
    const o=stock.opening_evidence;
    if(!o||!Object.keys(o).length)return '';
    const value=n=>n===null||n===undefined?'--':Number(n).toFixed(2);
    const sources={eltdx_batch:'eltdx行情',eltdx_open_archive:'eltdx按日归档',eltdx_quote_cache:'eltdx报价缓存',eltdx_snapshot_cache:'eltdx快照缓存',eltdx_quote_posthoc:'eltdx事后查询',stock_daily_open_cache:'当日日线缓存',stock_daily_silver_open:'当日日线表',tushare_daily_open_posthoc:'盘后日线补充'};
    return `<p class="text-sm text-slate-400 mt-2">开盘 ${esc(value(o.open_price))} · 昨收参考 ${esc(value(o.pre_close))} · 开盘涨跌 ${esc(percent(o.open_gap==null?null:o.open_gap*100))} · 来源 ${esc(sources[o.opening_source]||o.opening_source||'未取得')}</p>`;
  }
  function factorDetail(stock) {
    const factor=stock.board_feedback||stock.last_signal?.board_feedback;
    if(!factor||factor.role!=='risk_factor'||(!factor.active&&factor.assessed!==false))return '';
    const adjustment=Number(factor.score_adjustment||0);
    return `<p class="text-sm text-amber-300 mt-2 dr-factor-warning">风险因子 · 三板负反馈：${esc(factor.active?'命中':'未评估')} · ${esc(factor.reason||'--')}${adjustment<0?` · 强度扣分 ${esc(-adjustment)}`:''}</p>`;
  }
  async function refreshRuns() {
    const value = await api('/api/intraday-replay');
    savedRuns = value.runs;
    for (const id of ['dr-runs','dr-baseline']) {
      const selected = $(id).value;
      $(id).innerHTML = `<option value="">${id === 'dr-runs' ? '选择报告' : '不对照'}</option>` + value.runs
        .filter(r => id === 'dr-runs' || r.state === 'done')
        .map(r=>`<option value="${esc(r.run_id)}">${esc(r.request.market_date)} · ${esc(r.request.profile==='leader_pool'?'龙头池':'决策池')} · ${esc(new Date(r.started_epoch*1000).toLocaleTimeString('zh-CN'))} · ${esc(labels[r.state]||r.state)}</option>`).join('');
      $(id).value = id === 'dr-runs' && runId ? runId : selected;
    }
    return value.runs;
  }
  function renderReport(r) {
    $('dr-summary').innerHTML = [['候选日 / 行情日',`${r.candidate_date} / ${r.market_date}`],['本次分钟回放曾确认',`${r.confirmed_stocks} / ${r.stocks.length}`],['缺证据项',r.failures.length],['当日全天通知发送记录',r.runtime_summary?`${r.runtime_summary.sent_stocks} / ${r.stocks.length}`:'--']].map(([label,v])=>`<div>${esc(label)}<strong>${esc(v)}</strong></div>`).join('');
    const cycles=r.runtime_summary?.cycle_summary;
    if(cycles?.recorded_cycles)$('dr-summary').insertAdjacentHTML('beforeend',`<div>真实后台循环<strong>${esc(cycles.recorded_cycles)}</strong></div><div>整轮耗时中位 / 最大（毫秒）<strong>${esc(cycles.duration_p50_ms)} / ${esc(cycles.duration_max_ms)}</strong></div>`);
    $('dr-evidence').classList.remove('hidden');
    $('dr-limits').innerHTML = r.limitations.map(v=>`<p>${esc(v)}</p>`).join('') + `<p>不发送微信消息，不修改账户。实时推送验证：${r.live_runtime_verified?'已验证':'未验证'}</p><div class="dr-version">输入 ${esc(r.input_hash.slice(0,16))} · 代码 ${esc(r.code_hash.slice(0,16))} · ${esc(r.input_basis)} · 回放至 ${esc(r.until)}</div>`;
    $('dr-failures').innerHTML = r.failures.length ? table(['日期','股票','缺失证据','原因','缺分钟代码','缺昨收代码','未解析板块'],r.failures.map(f=>[f.date,f.code,f.evidence,f.reason,missingList(f.missing_minute_codes),missingList(f.missing_previous_close_codes),missingList(f.unresolved_sector_names)])) : '';
    const recorded = Object.values(r.recorded_runtime||{}).flatMap(source=>source.rows||[]);
    $('dr-failures').innerHTML += `<details><summary>原始运行证据 ${recorded.length} 条</summary>${recorded.length ? table(['股票','范围','原始状态','观测时间','原始确认时间','触发或否决原因','接收时间'],recorded.map(row=>[row.stock_code||row.code,row.profile||row.evidence?.profile,row.evidence?.status||row.evidence?.confirm_status||row.status,row.observed_at||row.first_seen_at,row.evidence?.confirm_time||row.evidence?.minute_closed_at||row.first_confirmed_at,row.evidence?.health_gate_reason||row.evidence?.veto_reason||row.evidence?.trigger_reason||row.evidence?.reason,row.evidence?.quote_received_at])) : '<p class="text-sm text-amber-300 mt-2">未归档原始运行记录，不能据此判断是否触发或被拦截。</p>'}</details>`;
    $('dr-comparison').classList.toggle('hidden',!r.comparison);
    if (r.comparison) $('dr-changes').innerHTML = `<p class="text-sm text-amber-300">${esc(r.comparison.reason)}</p>` + table(['股票','原确认时间','本次确认时间','原状态 / 原因','本次状态 / 原因'],r.comparison.changes.map(c=>[c.code,c.before.first_confirm_time||'--',c.after.first_confirm_time||'--',`${signalLabels[c.before.status]||c.before.status} / ${c.before.reason}`,`${signalLabels[c.after.status]||c.after.status} / ${c.after.reason}`]));
    $('dr-stocks').innerHTML = r.stocks.map(s=>`<details><summary><b>${esc(s.name)} ${esc(s.code)}</b><span>${esc(signalLabels[s.status]||s.status)}</span><span>首次确认 ${esc(s.first_confirm_time||'--')} · 观测 ${esc(s.first_evaluated_at||'--')}</span></summary><p class="text-sm text-slate-400 mt-2">${esc(s.reason)}</p>${s.price_evidence?`<p class="text-sm text-amber-300 mt-2">仅价格条件首次出现 ${esc(s.price_evidence.first_price_trigger||'--')} · 跌破开盘低点 ${esc(s.price_evidence.first_opening_low_break||'--')} · 成交额${s.price_evidence.amount_estimated?'含估算':'为源数据'}</p>`:''}${table(['观测时间','分钟状态','触发 / 否决原因','确认 / 成交时点','板块覆盖','板块路径','相对开盘 / 3分钟','趋势覆盖','数据状态'],s.transitions.map(t=>[t.evaluated_at,signalLabels[t.signal_status]||t.signal_status,t.reason,`${t.confirm_time||'--'} / ${t.entry_time||'--'}`,t.sector_detail?.assessed===false?'尚未检查':`${((t.sector_detail?.coverage||0)*100).toFixed(1)}%`,sectorPath(t.sector_detail),`${percent(t.sector_detail?.open_gain_pct)} / ${percent(t.sector_detail?.three_minute_change_pct)}`,percent(t.sector_detail?.trend_coverage===undefined?null:t.sector_detail.trend_coverage*100),t.data_status]))}</details>`).join('');
    const stockPanels = $('dr-stocks').querySelectorAll(':scope > details');
    r.stocks.forEach((stock,index)=>{
      const summary=stockPanels[index]?.querySelector('summary');
      if(!summary)return;
      const spans=summary.querySelectorAll('span');
      spans[0].textContent=`回放结束：${signalLabels[stock.status]||stock.status}`;
      spans[1].textContent=`回放首次确认 ${stock.first_confirm_time||'--'} · 回放观测 ${stock.first_evaluated_at||'--'}`;
      summary.insertAdjacentHTML('afterend',confirmationComparison(stock)+openingDetail(stock)+factorDetail(stock)+runtimeDetail(stock));
    });
    $('dr-download').classList.remove('hidden');
    $('dr-download').href = `/api/intraday-replay/${encodeURIComponent(r.run_id)}?report=true`;
  }
  async function poll() {
    if (!runId || pending) return;
    pending = true;
    const target = runId;
    try {
      const {data:s} = await api(`/api/intraday-replay/${encodeURIComponent(target)}`);
      if (target !== runId) return;
      const busy = ['running','queued'].includes(s.state);
      $('dr-start').disabled = busy; $('dr-cancel').disabled = !busy;
      $('dr-state').textContent = labels[s.state] || s.state;
      const p = s.progress || {}, percent = s.state === 'done' ? 100 : p.total ? Math.min(100,100*p.completed/p.total) : 0;
      $('dr-progress').value = percent;
      $('dr-progress-text').textContent = p.total ? `${p.completed} / ${p.total} · 当前阶段 ${percent.toFixed(1)}%` : '';
      $('dr-current').textContent = `${p.stage||''} ${p.code||''} ${p.minute||''}${p.minutes_total?` · ${p.minutes_completed}/${p.minutes_total} 分钟`:''}`;
      $('dr-error').textContent = s.error || '';
      if (busy) timer = setTimeout(poll,1500);
      else {
        if (s.state === 'done') {
          const report = (await api(`/api/intraday-replay/${encodeURIComponent(target)}?report=true`)).data;
          if (target !== runId) return;
          renderReport(report);
        }
        await refreshRuns();
      }
    } catch(e) { $('dr-error').textContent = e.message; timer = setTimeout(poll,3000); }
    finally {pending=false;if(target!==runId)timer=setTimeout(poll,0);}
  }
  $('replay-form').addEventListener('submit',async event=>{
    event.preventDefault(); clearTimeout(timer); $('dr-start').disabled=true; $('dr-error').textContent='';
    try {
      const result = await api('/api/intraday-replay',{market_date:$('dr-date').value,profile:$('dr-profile').value,lookback:Number($('dr-lookback').value),until:$('dr-until').value,codes:$('dr-codes').value,fetch_missing:$('dr-fetch').checked,baseline_id:$('dr-baseline').value});
      runId=result.run_id; localStorage.setItem('intraday-diagnostic-run',runId);
      for(const id of ['dr-evidence','dr-comparison','dr-download'])$(id).classList.add('hidden');
      $('dr-summary').innerHTML=''; $('dr-stocks').textContent='等待回放结果';
      await refreshRuns(); await poll();
    } catch(e) { $('dr-error').textContent=e.message; $('dr-start').disabled=false; }
  });
  $('dr-runs').addEventListener('change',()=>{if(!$('dr-runs').value)return; clearTimeout(timer);runId=$('dr-runs').value;localStorage.setItem('intraday-diagnostic-run',runId);poll();});
  $('dr-cancel').addEventListener('click',async()=>{try{await api(`/api/intraday-replay/${runId}/cancel`,{});$('dr-current').textContent='取消已请求，等待当前取数完成';}catch(e){$('dr-error').textContent=e.message;}});
  $('dr-profile').addEventListener('change',()=>{$('dr-lookback').disabled=$('dr-profile').value!=='leader_pool';});
  $('dr-baseline').addEventListener('change',()=>{
    const baseline=savedRuns.find(r=>r.run_id===$('dr-baseline').value);
    if(!baseline)return;
    const r=baseline.request;
    $('dr-date').value=r.market_date;$('dr-profile').value=r.profile;$('dr-lookback').value=String(r.lookback);
    $('dr-until').value=r.until.slice(0,5);$('dr-codes').value=r.codes.join(',');
    $('dr-lookback').disabled=r.profile!=='leader_pool';
  });
  refreshRuns().then(runs=>{
    const saved=localStorage.getItem('intraday-diagnostic-run');
    runId=runs.find(r=>['running','queued'].includes(r.state))?.run_id || runs.find(r=>r.run_id===saved)?.run_id || runs[0]?.run_id || '';
    if(runId)poll();
  }).catch(e=>{$('dr-error').textContent=e.message;});
})();
