(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {queued:'等待运行',running:'运行中',done:'已完成',failed:'失败',cancelled:'已取消',interrupted:'任务中断'};
  const signalLabels = {confirmed:'分钟规则确认',observe:'观察',cancelled:'否决',unfilled:'无法成交',missing_evidence:'缺证据'};
  let runId = '', timer, pending = false, savedRuns = [];
  async function api(url, body) {
    const response = await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
    const value = await response.json();
    if (!response.ok || value.ok === false) throw new Error(value.message || '请求失败');
    return value;
  }
  function table(heads, rows) {
    return `<div class="dr-table"><table><thead><tr>${heads.map(v=>`<th>${esc(v)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
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
    $('dr-summary').innerHTML = [['候选日 / 行情日',`${r.candidate_date} / ${r.market_date}`],['规则曾确认',`${r.confirmed_stocks} / ${r.stocks.length}`],['缺证据项',r.failures.length],['分钟数据检查',r.complete?'检查通过':'数据不完整']].map(([label,v])=>`<div>${esc(label)}<strong>${esc(v)}</strong></div>`).join('');
    $('dr-evidence').classList.remove('hidden');
    $('dr-limits').innerHTML = r.limitations.map(v=>`<p>${esc(v)}</p>`).join('') + `<p>不发送微信消息，不修改账户。实时推送验证：${r.live_runtime_verified?'已验证':'未验证'}</p><div class="dr-version">输入 ${esc(r.input_hash.slice(0,16))} · 代码 ${esc(r.code_hash.slice(0,16))} · ${esc(r.input_basis)} · 回放至 ${esc(r.until)}</div>`;
    $('dr-failures').innerHTML = r.failures.length ? table(['日期','股票','缺失证据','原因'],r.failures.map(f=>[f.date,f.code,f.evidence,f.reason])) : '';
    const recorded = Object.values(r.recorded_runtime||{}).flatMap(source=>source.rows||[]);
    $('dr-failures').innerHTML += `<details><summary>原始运行证据 ${recorded.length} 条</summary>${recorded.length ? table(['股票','原始确认时间','门禁否决','接收时间','通知状态'],recorded.map(row=>[row.stock_code||row.code,row.evidence?.confirm_time,row.evidence?.health_gate_reason||row.evidence?.veto_reason,row.evidence?.quote_received_at,row.evidence?.notification_status])) : '<p class="text-sm text-slate-400 mt-2">没有本地原始运行记录；需要对应服务器日志或证据库。</p>'}</details>`;
    $('dr-comparison').classList.toggle('hidden',!r.comparison);
    if (r.comparison) $('dr-changes').innerHTML = `<p class="text-sm text-amber-300">${esc(r.comparison.reason)}</p>` + table(['股票','原确认时间','本次确认时间','原状态 / 原因','本次状态 / 原因'],r.comparison.changes.map(c=>[c.code,c.before.first_confirm_time||'--',c.after.first_confirm_time||'--',`${signalLabels[c.before.status]||c.before.status} / ${c.before.reason}`,`${signalLabels[c.after.status]||c.after.status} / ${c.after.reason}`]));
    $('dr-stocks').innerHTML = r.stocks.map(s=>`<details><summary><b>${esc(s.name)} ${esc(s.code)}</b><span>${esc(signalLabels[s.status]||s.status)}</span><span>首次确认 ${esc(s.first_confirm_time||'--')} · 观测 ${esc(s.first_evaluated_at||'--')}</span></summary><p class="text-sm text-slate-400 mt-2">${esc(s.reason)}</p>${s.price_evidence?`<p class="text-sm text-amber-300 mt-2">仅价格条件首次出现 ${esc(s.price_evidence.first_price_trigger||'--')} · 跌破开盘低点 ${esc(s.price_evidence.first_opening_low_break||'--')} · 成交额${s.price_evidence.amount_estimated?'含估算':'为源数据'}</p>`:''}${table(['观测时间','分钟状态','触发 / 否决原因','确认 / 成交时点','板块覆盖','数据状态'],s.transitions.map(t=>[t.evaluated_at,signalLabels[t.signal_status]||t.signal_status,t.reason,`${t.confirm_time||'--'} / ${t.entry_time||'--'}`,`${((t.sector_detail?.coverage||0)*100).toFixed(1)}%`,t.data_status]))}</details>`).join('');
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
