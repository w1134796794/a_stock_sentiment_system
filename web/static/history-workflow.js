(() => {
  const root = document.getElementById('history-workflow');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const apiRoot = '/api/admin/history-workflows';
  const phases = ['prepare', 'prefetch', 'freeze', 'run', 'reconcile'];
  const backfillOnly = root.dataset.backfill === 'true';
  const labels = {idle: '待执行', queued: '等待子进程', running: '执行中', complete: '完成', blocked: '证据不足或执行受阻', interrupted: '执行中断，可续跑', failed: '执行失败'};
  const requested = new URLSearchParams(location.search).get('history_job') || '';
  let selected = /^[0-9a-f]{32}$/.test(requested) ? requested : '', timer = null, pending = false, current = null;
  async function api(path, options = {}) {
    const response = await fetch(apiRoot + path, {headers: {'Content-Type': 'application/json'}, ...options});
    const value = await response.json();
    if (!response.ok || !value.ok) throw new Error(value.message || '请求失败');
    return value.data;
  }
  function table(columns, rows, target = 'hw-detail') {
    const host = $(target); host.replaceChildren();
    if (!rows.length) return;
    const element = document.createElement('table'), head = element.createTHead().insertRow();
    columns.forEach(([title]) => {const th = document.createElement('th'); th.textContent = title; head.append(th);});
    const body = element.createTBody();
    const states = {missing:'缺原始归档',captured:'已冻结',compared:'已对账',incomplete_archive:'归档不完整'};
    rows.forEach(row => {const tr = body.insertRow(); columns.forEach(([, field]) => {tr.insertCell().textContent = String(field === 'status' ? states[row[field]] || row[field] || '--' : row[field] ?? '--');});});
    host.append(element);
  }
  function render(job) {
    current = job;
    const option = Array.from($('hw-jobs').options).find(row => row.value === job.job_id);
    if (option) option.textContent = `${job.start} → ${job.end} · ${labels[job.state] || job.state} · ${job.job_id.slice(0, 8)}`;
    const busy = ['running', 'queued'].includes(job.state);
    const p = job.progress || {};
    $('hw-status').textContent = `${job.start} → ${job.end} · ${labels[job.state] || job.state} · ${p.stage || ''} ${p.trade_date || ''} ${p.code || ''}`;
    if (p.success !== undefined) $('hw-status').textContent += ` · ${p.completed}/${p.total} · 成功 ${p.success} · 失败 ${p.failed} · 停牌跳过 ${p.skipped || 0} · 池内 ${p.pool_success}/${p.pool_total}`;
    if (p.last_result) $('hw-status').textContent += ` · ${p.last_result.valid ? `${p.last_result.rows}条 ${p.last_result.source}` : p.last_result.reason}`;
    $('hw-version').textContent = `任务 ${job.job_id} · 冻结版本 ${job.frozen_version || '--'} · ${job.updated_at}`;
    $('hw-error').textContent = job.error || '';
    $('hw-create').disabled = busy;
    $('hw-cancel').disabled = !busy;
    $('hw-progress').value = p.total > 0 ? Math.min(100, (p.completed || 0) / p.total * 100) : job.state === 'complete' ? 100 : 0;
    root.querySelectorAll('[data-hw-phase]').forEach(button => {
      const phase = button.dataset.hwPhase, index = phases.indexOf(phase);
      button.disabled = busy || (index > 0 && job.stages[phases[index - 1]] !== 'complete');
      button.dataset.state = job.stages[phase] || '';
      button.title = labels[job.stages[phase]] || '未执行';
    });
    $('hw-summary').replaceChildren();
    $('hw-trades').hidden = backfillOnly || job.stages.run !== 'complete';
    if (!backfillOnly && job.summary) {
      [['收益', 'total_return', true], ['最大回撤', 'max_drawdown', true], ['交易笔数', 'total_trades', false]].forEach(([name, key, pct]) => {
        const span = document.createElement('span'), number = Number(job.summary[key]);
        span.textContent = `${name} ${Number.isFinite(number) ? pct ? (number * 100).toFixed(2) + '%' : number : '--'}`;
        $('hw-summary').append(span);
      });
    }
    $('hw-data-summary').replaceChildren();
    if (job.sector_mode) {
      const span = document.createElement('span');
      span.textContent = job.sector_mode === 'trend_score' ? '板块口径：自身趋势评分（可缺失）' : '板块口径：成分宽度必过（旧版）';
      $('hw-data-summary').append(span);
    }
    if (job.potential_stock_count !== undefined) {
      const span = document.createElement('span');
      span.textContent = `交易日 ${job.dates?.length ?? '--'} · 个股范围 ${job.potential_stock_count} · ${job.stages.freeze === 'complete' ? '必要分钟证据校验通过' : '待校验'}`;
      $('hw-data-summary').append(span);
    }
    Object.entries(job.acquisition || {}).forEach(([date, stats]) => {
      const span = document.createElement('span');
      span.textContent = `${date} 分钟：成功 ${stats.success} · 失败 ${stats.failed} · 停牌跳过 ${stats.skipped || 0} · 池内成功 ${stats.pool_success}/${stats.pool_total}`;
      $('hw-data-summary').append(span);
    });
    if ($('hw-backfill-link')) $('hw-backfill-link').href = '/market-backfill?history_job=' + job.job_id;
    if ($('hw-use-backtest')) {
      $('hw-use-backtest').href = '/portfolio/backtest?history_job=' + job.job_id;
      $('hw-use-backtest').hidden = busy || job.stages.freeze !== 'complete';
    }
    ['validation', 'result', 'reconciliation'].forEach(kind => {
      const link = $('hw-' + kind + '-link');
      link.href = apiRoot + '/' + job.job_id + '/report/' + kind;
      link.hidden = kind === 'validation' ? !job.stages.freeze : backfillOnly || (kind === 'result' ? job.stages.run !== 'complete' : job.stages.reconcile !== 'complete');
    });
    const acquisitionFailures = Object.entries(job.acquisition || {}).flatMap(([date, stats]) => (stats.failure_preview || []).map(row => ({...row,trade_date:date,evidence:'minutes'})));
    if (job.failures?.length || acquisitionFailures.length) table([['日期', 'trade_date'], ['股票', 'code'], ['缺少证据', 'evidence'], ['原因', 'reason']], job.failures?.length ? job.failures : acquisitionFailures);
    else if (!backfillOnly && job.reconciliation?.length) table([['日期', 'date'], ['范围', 'profile'], ['股票', 'code'], ['状态', 'status'], ['分钟确认', 'minute_confirm_time'], ['快照确认', 'snapshot_confirm_time'], ['备注', 'reason']], job.reconciliation);
    else if (job.archive_status?.length) table([['日期', 'date'], ['范围', 'profile'], ['原始快照', 'status']], job.archive_status);
    else if (job.pool_scope?.length) table([['行情日', 'date'], ['名单日', 'candidate_date'], ['龙头池', 'leader_pool'], ['决策池', 'decision_pool'], ['去重股票数', 'stock_count'], ['来源', 'basis']], job.pool_scope);
    else $('hw-detail').replaceChildren();
    clearTimeout(timer);
    if (busy) timer = setTimeout(poll, 2500);
  }
  async function poll() {
    if (!selected || pending) return;
    const id = selected;
    pending = true;
    try {
      const value = await api('/' + id);
      if (id === selected) {
        render(value);
        if (value.phase) {
          try {const log = await api('/' + id + '/report/log'); if (id === selected) $('hw-log').textContent = log;}
          catch (_) { /* The child may not yet have opened its log. */ }
        }
        if (!backfillOnly && value.stages.run === 'complete') {
          const report = await api('/' + id + '/report/result');
          if (id === selected) table([['日期','date'],['股票','stock_code'],['名称','stock_name'],['方向','action'],['买入价格','entry_price'],['卖出价格','exit_price'],['盈亏','pnl']], (report.trades || []).slice(-100), 'hw-trades-detail');
        }
      }
    }
    catch (error) {$('hw-error').textContent = error.message; timer = setTimeout(poll, 5000);}
    finally {pending = false; if (id !== selected) setTimeout(poll, 0);}
  }
  async function list() {
    const rows = await api('');
    if (selected && !rows.some(job => job.job_id === selected)) rows.unshift(await api('/' + selected));
    $('hw-jobs').replaceChildren(new Option('选择任务', ''));
    rows.forEach(job => $('hw-jobs').add(new Option(`${job.start} → ${job.end} · ${labels[job.state] || job.state} · ${job.job_id.slice(0, 8)}`, job.job_id)));
    if (!selected && rows.length) selected = rows[0].job_id;
    $('hw-jobs').value = selected;
    if (selected) await poll();
  }
  $('hw-create').addEventListener('click', async () => {
    try {
      const job = await api('', {method: 'POST', body: JSON.stringify({start: $('replay-start').value.trim(), end: $('replay-end').value.trim(), capital: Number(root.dataset.capital)})});
      selected = job.job_id; await list();
    } catch (error) {$('hw-error').textContent = error.message;}
  });
  $('hw-jobs').addEventListener('change', () => {selected = $('hw-jobs').value; clearTimeout(timer); if (selected) poll();});
  $('hw-refresh').addEventListener('click', () => list().catch(error => {$('hw-error').textContent = error.message;}));
  $('hw-cancel').addEventListener('click', async () => {
    try {await api('/' + selected + '/cancel', {method:'POST', body:'{}'}); $('hw-status').textContent = '取消请求已提交，等待当前数据请求结束';}
    catch (error) {$('hw-error').textContent = error.message;}
  });
  root.querySelectorAll('[data-hw-phase]').forEach(button => button.addEventListener('click', async () => {
    if (!selected) return;
    const phase = button.dataset.hwPhase;
    const message = phase === 'prepare' ? '' : phase === 'prefetch' ? '按已保存的历史龙头池和决策池名单补取行情？仅允许盘外执行，不重算因子或重新选股。' : (current?.stages[phase] === 'complete' ? '重新执行此阶段？后续阶段将失效，已冻结的旧版本仍保留。' : '');
    if (message && !window.confirm(message)) return;
    button.disabled = true;
    try {render(await api('/' + selected + '/' + phase, {method: 'POST', body: '{}'})); await poll();}
    catch (error) {$('hw-error').textContent = error.message; await poll();}
  }));
  list().catch(error => {$('hw-error').textContent = error.message;});
})();
