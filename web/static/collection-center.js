(() => {
  const root = document.getElementById('collection-center');
  if (!root) return;
  const $ = id => document.getElementById('mc-' + id);
  const tabs = ['postclose', 'preopen', 'intraday', 'backfill'];
  const query = new URLSearchParams(location.search);
  let phase = tabs.includes(query.get('tab')) ? query.get('tab') : location.pathname === '/market-backfill' || query.has('history_job') ? 'backfill' : 'postclose';
  let dates = {}, defaults = {}, timer, revision = 0, working = false, last = null;
  const icons = () => window.lucide?.createIcons();
  const names = {stock_daily_silver:'个股日线', stock_basic_silver:'证券基础资料', daily_basic_silver:'每日指标', sector_daily_silver:'板块日线', limit_up_silver:'涨停池', limit_down_silver:'跌停池', moneyflow_silver:'资金流向', suspend_d_silver:'停复牌', all_daily:'全市场日线', stock_basic:'证券基础资料', daily_basic:'每日指标', suspend_d:'停复牌', limit_up:'涨停池', limit_down:'跌停池', ths_daily:'同花顺板块日线', ths_index:'同花顺板块目录', top_list:'龙虎榜', index_daily:'指数日线'};
  Object.assign(names, {stock_daily:'全市场日线', index_daily_silver:'指数日线', hm_detail:'游资明细', top_inst:'机构龙虎榜', limit_up_concepts:'涨停题材', limit_cpt_list:'最强板块', moneyflow_summary:'资金流汇总', moneyflow_ths:'同花顺个股资金流', moneyflow_dc:'东方财富个股资金流', sector_moneyflow_ths:'板块资金流', ths_hot:'同花顺热度', dc_hot:'东方财富热度', kpl_list:'开盘啦榜单', margin_detail:'融资融券', block_trade:'大宗交易'});
  const stateNames = {idle:'未运行', running:'采集中', done:'已完成', error:'失败', failed:'失败', partial:'部分完成', skipped:'已跳过'};
  const text = (id, value) => { $(id).textContent = value ?? '--'; };
  const dashDate = value => /^\d{8}$/.test(value || '') ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6)}` : value;
  const error = message => { $('error').hidden = !message; text('error', message); };
  async function api(path, options = {}) {
    const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
    if (response.redirected && new URL(response.url).pathname === '/login') throw new Error('登录已过期，请重新登录');
    const value = await response.json();
    if (!response.ok || value.ok === false || value.started === false) throw new Error(value.message || '读取失败');
    return value.data ?? value;
  }
  function metrics(rows) {
    $('metrics').replaceChildren();
    rows.forEach(([label, value, sub, tone]) => {
      const item = document.createElement('div'); item.className = 'mc-metric'; item.dataset.tone = tone || '';
      [['span', label], ['strong', value ?? '--'], ['small', sub]].forEach(([tag, content]) => {
        const el = document.createElement(tag); el.textContent = content; item.append(el);
      });
      $('metrics').append(item);
    });
  }
  function table(columns, rows, empty) {
    const host = $('table'); host.replaceChildren();
    if (!rows.length) {
      const div = document.createElement('div'); div.className = 'mc-empty';
      const icon = document.createElement('i'); icon.dataset.lucide = 'inbox';
      div.append(icon, document.createTextNode(empty || '尚无数据')); host.append(div); icons(); return;
    }
    const el = document.createElement('table'), head = el.createTHead().insertRow();
    columns.forEach(label => { const th = document.createElement('th'); th.textContent = label; head.append(th); });
    const body = el.createTBody();
    rows.forEach(row => {
      const tr = body.insertRow();
      row.forEach(value => {
        const td = tr.insertCell();
        if (value && typeof value === 'object') {
          const badge = document.createElement('span'); badge.className = 'mc-tag'; badge.dataset.tone = value.tone || ''; badge.textContent = value.label; td.append(badge);
        } else td.textContent = value ?? '--';
      });
    }); host.append(el);
  }
  function focus(title, detail, tone, action, disabled = false) {
    text('status-title', title); text('status-detail', detail); $('focus').dataset.tone = tone;
    $('action').querySelector('span').textContent = action;
    $('action').disabled = disabled || working;
    $('action').dataset.secondary = phase === 'intraday';
  }
  function render(value) {
    last = value;
    const task = value.task || {}, plan = value.plan || {}, board = value.boards || {};
    text('updated', `更新 ${value.as_of?.slice(11,19) || '--'}`);
    text('overline', `${dashDate(value.date)} · ${value.trading_day ? '交易日' : value.calendar_verified ? '非交易日' : '日历未验证'}`);
    const relevantTask = phase === 'postclose' || task.market_date === value.date;
    text('task-state', relevantTask ? stateNames[task.state || task.status] || '暂无任务' : '所选日期暂无任务');
    text('task-meta', relevantTask ? [task.started_at && `开始 ${task.started_at}`, task.finished_at && `结束 ${task.finished_at}`, task.error || task.reason].filter(Boolean).join(' · ') : '');
    text('log', relevantTask && task.lines?.length ? task.lines.slice(-120).join('\n') : task.reason || '暂无采集日志');
    if (phase === 'postclose') {
      const data = value.data || {}, rows = data.table_rows || {}, missing = data.missing || [], sources = data.sources || {};
      const busy = task.state === 'running', ready = !!data.ready;
      $('batch-action').disabled = busy || working;
      text('storage', ready ? '盘后基础数据已就绪' : '盘后数据待完成'); $('storage').dataset.ready = ready;
      focus(busy ? `正在采集 ${dashDate(task.date) || ''}` : ready ? data.complete ? '盘后数据已齐备' : '基础数据就绪，增强数据待补齐' : '盘后数据尚未就绪',
        busy ? '独立取数进程运行中，策略任务不随采集启动。' : ready ? data.complete ? '已保存原始数据与 Silver 标准层。因子计算与选股按独立任务运行。' : `待重试：${(data.source_missing || []).map(key => names[key] || key).join('、') || data.message || '部分增强接口'}` : data.message || '尚未完成所选交易日的基础行情采集。',
        busy ? 'neutral' : ready ? 'green' : 'warning', busy ? '采集中' : ready ? '重试缺失数据' : '开始盘后取数', busy || !value.trading_day || value.date > defaults.today);
      const total = Object.keys(rows).length;
      metrics([['基础表就绪', total ? `${total - missing.length} / ${total}` : '--', 'Silver 标准层', ready ? 'green' : ''], ['原始记录', total ? Object.values(rows).reduce((a,b)=>a+Number(b || 0),0).toLocaleString() : '--', '所选交易日'], ['增强接口缺失', data.source_missing?.length ?? '--', '可单独重试', data.source_missing?.length ? 'warning' : ''], ['当前采集任务', stateNames[task.state] || '未运行', '不串联策略']]);
      text('table-title', '盘后数据明细'); text('table-meta', `基础表 ${total} · 原始接口 ${Object.keys(sources).length}`);
      const details = Object.entries(rows).map(([key, count]) => [names[key] || key, count ?? '--', {label:missing.includes(key) ? '缺失' : '已落地', tone:missing.includes(key) ? 'warning' : 'green'}, 'Silver 标准层']);
      Object.entries(sources).forEach(([key, item]) => details.push([names[key] || key, item.rows ?? '--', {label:item.ok ? '已取得' : '待重试', tone:item.ok ? 'green' : 'warning'}, item.error || '原始接口']));
      table(['数据项', '记录数', '状态', '来源 / 缺失原因'], details, '尚未找到盘后采集记录');
    } else if (phase === 'preopen') {
      const exists = !!plan.plan_id, busy = relevantTask && task.status === 'running' && !plan.plan_id;
      text('storage', exists ? '盘前名单已冻结' : '盘前名单待准备'); $('storage').dataset.ready = exists;
      const tooLate = value.date < defaults.today || value.date === defaults.today && (value.as_of?.slice(11,16) || '') >= '09:25';
      focus(busy ? '正在整合盘前数据' : exists ? '当日采集名单已冻结' : '等待整合当日采集名单',
        exists ? `名单日 ${dashDate(plan.candidate_date)} · ${plan.warnings?.join('；') || '已整合龙头池、决策池及所属板块。'}` : tooLate ? '已过盘前截止时间，不允许重建历史盘前证据。' : '读取已生成的历史两池名单，采集上市状态及板块映射；缺少策略产物时只记录缺失。',
        exists ? 'green' : 'warning', busy ? '整合中' : exists ? '名单已冻结' : '准备盘前数据', busy || exists || tooLate || !value.trading_day);
      metrics([['去重个股', plan.stock_count, '当日快照采集范围', 'green'], ['龙头池 / 决策池', exists ? `${plan.leader_count} / ${plan.decision_count}` : '--', '按已生成名单读取'], ['板块映射', board.codes?.length ?? '--', '板块自身行情通道'], ['剔除成员', plan.listing_exclusions?.length ?? '--', '按上市状态证据']]);
      text('table-title', '当日个股采集名单'); text('table-meta', exists ? `冻结 ${plan.frozen_at?.slice(11,19) || '--'} · ${plan.plan_id.slice(0,12)}` : '尚未冻结');
      table(['股票', '代码', '名单来源', '所属板块 / 概念'], (plan.rows || []).map(row => [row.name, row.code, (row.sources || []).map(s=>s==='leader_pool'?'龙头池':s==='decision_pool'?'决策池':s).join('、'), (row.sectors || []).join('、')]), '未找到当日冻结名单');
    } else {
      const c = value.collector || {}, a = value.archive || {}, sameDay = value.live_date_matches;
      const ready = sameDay && value.storage === 'redis' && c.fresh && c.ok && c.lease_owned && a.fresh && a.ok && plan.plan_id;
      text('storage', value.storage === 'redis' ? 'Redis 共享存储' : 'Redis 未连接'); $('storage').dataset.ready = value.storage === 'redis';
      const missing = value.missing || [];
      focus(!sameDay ? '所选日期不是当前实时行情日' : ready ? '采集与增量归档运行正常' : '实时采集链路待就绪', !sameDay ? '下方显示当前进程心跳，不代表所选历史日的采集证据。历史分时请进入补数页签。' : missing.join('；') || c.message || '采集、归档由独立服务托管；策略仅从行情存储读取。', ready ? 'green' : 'warning', '检查采集状态');
      metrics([['个股采集', sameDay && c.fresh ? `${c.count ?? '--'} / ${c.requested_count ?? '--'}` : '--', '最近一批 / 请求数', ready ? 'green' : ''], ['实际采样间隔', value.quotes?.sample_intervals?.p50_seconds == null ? '--' : `${value.quotes.sample_intervals.p50_seconds}s`, '最新已记录间隔'], ['待归档事件', a.fresh ? a.pending ?? '--' : '--', '持久化队列，不等热窗口到期'], ['热窗口', plan.hot_window_seconds ? `${plan.hot_window_seconds}s` : '--', '超时仅退出缓存']]);
      text('table-title', '独立采集通道'); text('table-meta', sameDay ? '状态来自进程心跳' : '当前进程状态，非历史证据');
      const check = item => ({label:item.fresh && item.ok !== false ? '心跳正常' : '未就绪', tone:item.fresh && item.ok !== false ? 'green' : 'warning'});
      table(['通道', '频率 / 机制', '状态', '最近记录 / 原因'], [
        ['个股快照', '3 秒优先', check(c), c.updated_at || '未读取到独立采集器心跳'],
        ['板块自身行情', '独立板块通道', {label:c.tdx_board_shadow?.status === 'ok' ? '已采集' : c.tdx_board_shadow?.status === 'partial' ? '部分取得' : '未评估', tone:c.tdx_board_shadow?.status === 'ok' ? 'green' : ''}, c.tdx_board_shadow?.error || `计划板块 ${board.codes?.length ?? '--'}`],
        ['分钟兜底', '独立同步', check(value.minute_sync || {}), value.minute_sync?.updated_at || '未读取到分钟同步记录'],
        ['增量归档', '采集后异步落地', check(a), a.reason || a.updated_at || '未读取到归档进程心跳']]);
    }
  }
  async function refresh() {
    clearTimeout(timer);
    if (phase === 'backfill' || !$('date').value) return;
    const current = ++revision, selected = phase, date = $('date').value;
    $('refresh').disabled = true;
    try {
      const data = await api(`/api/admin/market-collection/status?phase=${selected}&date=${date.replaceAll('-','')}`);
      if (current !== revision) return;
      error(''); render(data);
      if ((selected === 'preopen' || data.task?.source === 'scheduled') && $('task-details').open) {
        const log = await api(`/api/admin/market-collection/log?date=${data.date}&phase=${selected}`);
        if (current === revision) text('log', log || '暂无采集日志');
      }
    } catch (e) { if (current === revision) { error(e.message); text('storage', '状态读取失败'); $('storage').dataset.ready = false; focus('采集状态读取失败', '未获得有效数据，不能判定为已就绪。', 'warning', '重试读取', true); } }
    finally { $('refresh').disabled = false; if (current === revision) timer = setTimeout(refresh, phase === 'intraday' ? 5000 : 15000); }
  }
  function select(next, updateURL = true) {
    if (dates[phase] !== undefined && $('date').value) dates[phase] = $('date').value;
    phase = next; last = null; revision++; clearTimeout(timer); error('');
    root.querySelectorAll('[data-tab]').forEach(btn => { const active = btn.dataset.tab === phase; btn.setAttribute('aria-selected', String(active)); btn.tabIndex = active ? 0 : -1; });
    $('panel-data').hidden = phase === 'backfill'; $('panel-backfill').hidden = phase !== 'backfill';
    $('panel-data').setAttribute('aria-labelledby', `mc-tab-${phase}`);
    $('batch-details').hidden = phase !== 'postclose';
    if (phase === 'backfill') { text('storage', '历史分钟数据'); $('storage').dataset.ready = false; text('updated', '按任务检查'); }
    const icon = document.createElement('i'); icon.dataset.lucide = phase === 'intraday' ? 'refresh-cw' : phase === 'preopen' ? 'list-checks' : 'download';
    $('action').querySelector('svg, i')?.replaceWith(icon);
    const config = {postclose:['盘后取数','日线 · 基础资料 · 板块 · 资金'],preopen:['盘前整合','已生成两池名单 · 上市状态 · 板块映射'],intraday:['盘中采集','独立采集器 · Redis 热数据 · 异步归档']}[phase];
    if (config) {
      text('phase-title', config[0]); text('phase-schedule', config[1]);
      $('date').value = dates[phase] || dashDate(defaults[phase] || defaults.today || ''); dates[phase] = $('date').value;
      if (!$('batch-start').value) $('batch-start').value = dashDate(defaults.postclose || '');
      if (!$('batch-end').value) $('batch-end').value = dashDate(defaults.postclose || '');
      focus('正在读取采集状态', '', 'neutral', '读取中', true); $('metrics').replaceChildren(); $('table').replaceChildren(); refresh();
    }
    if (updateURL) { const url = new URL(location.href); url.searchParams.set('tab', phase); history.replaceState(null, '', url); }
    icons();
  }
  root.querySelectorAll('[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => select(btn.dataset.tab));
    btn.addEventListener('keydown', event => {
      if (!['ArrowRight','ArrowLeft','Home','End'].includes(event.key)) return;
      event.preventDefault(); const index = tabs.indexOf(phase);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? 3 : (index + (event.key === 'ArrowRight' ? 1 : 3)) % 4;
      select(tabs[next]); $('tab-' + tabs[next]).focus();
    });
  });
  $('date').addEventListener('change', () => { dates[phase] = $('date').value; refresh(); });
  $('refresh').addEventListener('click', () => phase === 'backfill' ? document.getElementById('hw-refresh').click() : refresh());
  $('task-details').addEventListener('toggle', () => { if ($('task-details').open && phase !== 'backfill') refresh(); });
  $('action').addEventListener('click', async () => {
    if (phase === 'intraday') return refresh();
    if (working || !last) return;
    const selected = phase, date = $('date').value.replaceAll('-',''); working = true; $('action').disabled = true;
    try {
      await api(selected === 'postclose' ? '/api/fetch' : '/api/admin/market-collection/preopen', {method:'POST', body:JSON.stringify({date})});
      $('task-details').open = true; error('');
    } catch (e) { error(e.message); }
    finally { working = false; if (phase === selected) refresh(); }
  });
  $('batch-form').addEventListener('submit', async event => {
    event.preventDefault();
    const start = $('batch-start').value, end = $('batch-end').value;
    if (working || !start || !end) return;
    if (start > end) { error('结束日期不能早于开始日期'); return; }
    working = true; $('batch-action').disabled = true;
    try {
      await api('/api/fetch', {method:'POST',body:JSON.stringify({mode:'batch',start_date:start.replaceAll('-',''),end_date:end.replaceAll('-','')})});
      error(''); $('task-details').open = true;
    } catch (e) { error(e.message); }
    finally { working = false; refresh(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
  icons(); select(phase, false);
  api('/api/admin/market-collection/defaults').then(value => { defaults = value; select(phase, false); }).catch(e => error(e.message));
})();
