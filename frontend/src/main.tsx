import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { CandlestickSeries, ColorType, createChart, LineSeries } from "lightweight-charts";
import {
  Activity,
  ArrowRight,
  BarChart3,
  ChevronDown,
  ExternalLink,
  Flame,
  RefreshCw,
  Search,
  ShieldAlert,
  Target,
  X,
} from "lucide-react";
import type {
  Candidate,
  IntelligenceData,
  RealtimeData,
  RealtimeRow,
  StockWorkspace,
  WorkbenchData,
} from "./types";
import "./styles.css";

const GROUPS = [
  { key: "重点确认", description: "多策略共识，等待分钟买点", tone: "focus" },
  { key: "盘中观察", description: "具备优势，需要转强或板块确认", tone: "watch" },
  { key: "暂不参与", description: "规则不适用、证据不足或风险偏高", tone: "avoid" },
] as const;

const METRIC_LABELS: Record<string, string> = {
  stk_behavior_attention: "题材注意形成",
  stk_behavior_acceleration: "一致加速",
  stk_behavior_divergence: "分歧释放",
  stk_behavior_repair: "弱转强修复",
  stk_behavior_decay: "拥挤衰退",
  stk_sector_mainline_score: "主线地位",
  stk_sector_resonance_score: "板块共振",
  stk_sector_rotation_momentum: "板块轮动动量",
  stk_relative_strength_sector: "相对板块强度",
  stk_intraday_seal_quality: "封板综合质量",
  stk_amount_ratio_5d: "近5日成交额强度",
  stk_liquidity_percentile: "流动性分位",
  stk_capital_flow_consensus: "资金流共识",
  stk_lhb_sector_resonance: "龙虎榜板块共振",
  stk_lhb_crowding_risk: "龙虎榜拥挤风险",
  stk_kpl_leader_quality: "龙头质量",
  tech_score: "技术综合分",
};

const STATUS_LABELS: Record<string, string> = {
  confirmed: "买点确认",
  observe: "等待确认",
  cancelled: "信号取消",
  unfilled: "信号正确·无法成交",
};

const responseCache = new Map<string, { expiresAt: number; data: unknown }>();
const pendingRequests = new Map<string, Promise<unknown>>();

async function fetchData<T>(
  url: string,
  options: { ttlMs?: number; force?: boolean } = {},
): Promise<T> {
  const ttlMs = options.ttlMs ?? 15_000;
  const cached = responseCache.get(url);
  if (!options.force && cached && cached.expiresAt > Date.now()) return cached.data as T;
  const pending = pendingRequests.get(url);
  if (!options.force && pending) return pending as Promise<T>;

  const request = fetch(url, { headers: { Accept: "application/json" } }).then(async (response) => {
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) throw new Error(`接口返回异常（${response.status}）`);
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error?.message || "数据加载失败");
    responseCache.set(url, { expiresAt: Date.now() + ttlMs, data: payload.data });
    return payload.data as T;
  }).finally(() => pendingRequests.delete(url));
  pendingRequests.set(url, request);
  return request;
}

function signedPct(value: number) {
  return `${value >= 0 ? "+" : ""}${Number(value || 0).toFixed(2)}%`;
}

function money(value: number) {
  const amount = Number(value || 0);
  if (Math.abs(amount) >= 1e8) return `${(amount / 1e8).toFixed(2)}亿`;
  return `${(amount / 1e4).toFixed(0)}万`;
}

function RealtimeBadge({ row }: { row?: RealtimeRow }) {
  if (!row) return null;
  const status = row.is_stale ? "行情过期" : STATUS_LABELS[row.confirm_status] || "等待确认";
  return <span className={`realtime-badge realtime-badge--${row.confirm_status}`}>{status}</span>;
}

function CandidateCard({ row, realtime, onOpen, onPrefetch }: { row: Candidate; realtime?: RealtimeRow; onOpen: () => void; onPrefetch: () => void }) {
  const consensus = `${row.strategy_consensus}/${row.strategy_total || "-"}`;
  const sectorLabel = row.mainline_confirmed ? "所属主线" : "关联题材";
  const sectorName = row.mainline_confirmed ? row.mainline : row.related_themes?.[0];
  return (
    <button className="candidate-card" type="button" onClick={onOpen} onMouseEnter={onPrefetch} onFocus={onPrefetch}>
      <div className="candidate-card__head">
        <div>
          <strong>{row.name || "未命名股票"}</strong>
          <span className="stock-code">{row.code}</span>
        </div>
        <div className="candidate-badges"><RealtimeBadge row={realtime} />{row.crowding_level && row.crowding_level !== "正常" && <span className="crowding-badge">{row.theme_cluster} {row.crowding_level}</span>}<span className="consensus-badge">共识 {consensus}</span></div>
      </div>
      <p className="candidate-conclusion">{realtime?.reason || row.conclusion || "等待盘中条件确认，不主动追价。"}</p>
      <div className="strategy-tags">
        {(row.hit_strategies || []).slice(0, 3).map((strategy) => <span key={strategy}>{strategy}</span>)}
      </div>
      <div className="candidate-grid">
        <div><span>{sectorLabel}</span><b>{sectorName || "待确认"}</b></div>
        <div><span>{realtime ? "实时涨幅" : "板块强度"}</span><b className={realtime && realtime.pct_chg < 0 ? "price-down" : realtime ? "price-up" : ""}>{realtime ? signedPct(realtime.pct_chg) : row.sector_strength ? row.sector_strength.toFixed(1) : "--"}</b></div>
        <div><span>入场模式</span><b>{realtime?.entry_mode_text || row.entry_mode || "盘中确认"}</b></div>
        <div><span>建议仓位</span><b>{row.position || (row.position_cap_pct ? `${row.position_cap_pct}%以内` : "观察")}</b></div>
      </div>
      <div className="candidate-card__foot">
        <span className="condition">确认：{row.confirmation || "满足策略分钟条件"}</span>
        <span className="open-detail">查看证据 <ArrowRight size={14} /></span>
      </div>
    </button>
  );
}

function DailyChart({ rows }: { rows: StockWorkspace["candles"] }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current || !rows.length) return undefined;
    const chart = createChart(host.current, {
      height: 240,
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#091426" }, textColor: "#8190a8" },
      grid: { vertLines: { color: "#18263a" }, horzLines: { color: "#18263a" } },
      rightPriceScale: { borderColor: "#26364e" },
      timeScale: { borderColor: "#26364e", timeVisible: false },
      localization: { locale: "zh-CN" },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#ff667d",
      downColor: "#2ed3a3",
      wickUpColor: "#ff667d",
      wickDownColor: "#2ed3a3",
      borderVisible: false,
    });
    series.setData(rows.map((row) => ({
      time: `${row.trade_date.slice(0, 4)}-${row.trade_date.slice(4, 6)}-${row.trade_date.slice(6, 8)}`,
      open: Number(row.open), high: Number(row.high), low: Number(row.low), close: Number(row.close),
    })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [rows]);
  return <div className="daily-chart" ref={host} />;
}

function PromotionTrendChart({ rows }: { rows: NonNullable<WorkbenchData["market_context"]["promotion_trend"]>["history"] }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current || !rows?.length) return undefined;
    const chart = createChart(host.current, {
      height: 178,
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0d1829" }, textColor: "#8190a8" },
      grid: { vertLines: { color: "#18263a" }, horzLines: { color: "#18263a" } },
      rightPriceScale: { borderColor: "#26364e", scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: "#26364e", timeVisible: false },
      localization: { locale: "zh-CN" },
    });
    const definitions = [
      ["rate_1to2", "#2ed3a3"],
      ["rate_2to3", "#f4bf4f"],
      ["rate_3to4", "#ff667d"],
      ["rate_high", "#6ab7ff"],
    ] as const;
    definitions.forEach(([key, color]) => {
      const series = chart.addSeries(LineSeries, { color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
      series.setData(rows.flatMap((row) => {
        const value = row[key];
        return value == null ? [] : [{
          time: `${row.trade_date.slice(0, 4)}-${row.trade_date.slice(4, 6)}-${row.trade_date.slice(6, 8)}`,
          value: Number(value),
        }];
      }));
    });
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [rows]);
  return <div className="promotion-trend__chart" ref={host} />;
}

function DetailDrawer({ candidate, date, onClose }: { candidate: Candidate | null; date: string; onClose: () => void }) {
  const [stock, setStock] = useState<StockWorkspace | null>(null);
  const [loadingStock, setLoadingStock] = useState(false);
  useEffect(() => {
    if (!candidate) { setStock(null); return; }
    let cancelled = false;
    setLoadingStock(true);
    fetchData<StockWorkspace>(`/api/v1/workbench/stocks/${candidate.code}?date=${encodeURIComponent(date)}`, { ttlMs: 60_000 })
      .then((value) => { if (!cancelled) setStock(value); })
      .catch(() => { if (!cancelled) setStock(null); })
      .finally(() => { if (!cancelled) setLoadingStock(false); });
    return () => { cancelled = true; };
  }, [candidate?.code, date]);
  if (!candidate) return null;
  const evidence = candidate.evidence || {};
  const metrics = Object.entries(evidence.metrics || {}).slice(0, 8);
  const sectorLabel = candidate.mainline_confirmed ? "所属主线" : "关联题材";
  const sectorName = candidate.mainline_confirmed ? candidate.mainline : candidate.related_themes?.[0];
  return (
    <div className="drawer-layer" role="dialog" aria-modal="true" aria-label="候选股证据">
      <button className="drawer-backdrop" type="button" onClick={onClose} aria-label="关闭详情" />
      <aside className="drawer">
        <div className="drawer__head">
          <div><span className="eyebrow">交易证据</span><h2>{candidate.name} <small>{candidate.code}</small></h2></div>
          <button className="icon-button" type="button" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <section className="drawer-summary">
          <div><span>行动分组</span><b>{candidate.action_group}</b></div>
          <div><span>策略共识</span><b>{candidate.strategy_consensus}/{candidate.strategy_total || "-"}</b></div>
          <div><span>{sectorLabel}</span><b>{sectorName || "待确认"}</b></div>
          <div><span>板块强度</span><b>{candidate.sector_strength || "--"}</b></div>
        </section>
        {stock && <div className="stock-tags">{[...(stock.industry || []), ...(stock.concepts || []).slice(0, 5)].map((item) => <span key={item}>{item}</span>)}</div>}
        <section className="drawer-section">
          <h3>交易动作</h3>
          <dl className="action-list">
            <div><dt>命中策略</dt><dd>{candidate.hit_strategies?.join("、") || "未命中生产策略"}</dd></div>
            <div><dt>一句话结论</dt><dd>{candidate.conclusion || "等待盘中确认"}</dd></div>
            <div><dt>确认条件</dt><dd>{candidate.confirmation || "满足策略分钟条件"}</dd></div>
            <div><dt>失效条件</dt><dd>{candidate.invalidation || "板块转弱或个股结构破坏"}</dd></div>
            <div><dt>建议仓位</dt><dd>{candidate.position || "观察"}</dd></div>
          </dl>
        </section>
        <section className="drawer-section">
          <h3>日 K 走势</h3>
          {loadingStock && <div className="chart-loading">正在读取本地行情...</div>}
          {!loadingStock && stock?.candles?.length ? <DailyChart rows={stock.candles} /> : !loadingStock && <div className="chart-loading">暂无本地日 K 数据</div>}
        </section>
        <section className="drawer-section">
          <h3>专业证据</h3>
          <div className="evidence-list">
            {(evidence.rule_reasons || []).map((reason) => <p key={reason} className="positive">{reason}</p>)}
            {(evidence.penalty_reasons || []).map((reason) => <p key={reason} className="negative">{reason}</p>)}
            {!evidence.rule_reasons?.length && !evidence.penalty_reasons?.length && <p>暂无展开证据。</p>}
          </div>
        </section>
        {metrics.length > 0 && <section className="drawer-section"><h3>核心指标</h3><div className="metric-grid">{metrics.map(([key, value]) => <div key={key}><span>{METRIC_LABELS[key] || key}</span><b>{Number(value).toFixed(1)}</b></div>)}</div></section>}
        <a className="stock-link" href={`/stock/${candidate.code}?date=${date}`}>打开完整分时与日K <ExternalLink size={15} /></a>
      </aside>
    </div>
  );
}

function IntelligenceView({ data, loading }: { data: IntelligenceData | null; loading: boolean }) {
  if (loading && !data) return <div className="workspace-loading">正在聚合涨停、主线与龙头...</div>;
  if (!data) return <div className="empty-state">市场研判数据暂不可用。</div>;
  return <div className="intelligence-layout">
    <section className="intel-section">
      <div className="intel-section__head"><div><span className="eyebrow">题材强度</span><h3>主线观察</h3></div><a href="/data/sector">查看板块热度 <ArrowRight size={14} /></a></div>
      <div className="mainline-grid">{data.mainlines.map((row) => <article className="mainline-card" key={row.name}>
        <div><strong>{row.name}</strong><span>强度 {row.strength.toFixed(1)}</span></div>
        <p>{row.focus_count} 只重点 · {row.leader_count} 只龙头 · {row.candidate_count} 只候选</p>
        <div className="stock-tags">{row.stocks.map((stock) => <span key={stock.code}>{stock.name}</span>)}</div>
      </article>)}</div>
      {!data.mainlines.length && <div className="empty-state">当日尚未形成可识别主线。</div>}
    </section>
    <section className="intel-section">
      <div className="intel-section__head"><div><span className="eyebrow">高度与宽度</span><h3>涨停梯队</h3></div><a href={`/data/limitup/${data.trade_date}`}>查看全部 <ArrowRight size={14} /></a></div>
      <div className="echelon-list">{data.limitup.echelon.map((row) => <article key={row.board_height}>
        <div className="echelon-height"><strong>{row.board_height}板</strong><span>{row.count}只</span></div>
        <div className="echelon-stocks">{row.stocks.map((stock) => <a href={`/stock/${stock.code}?date=${data.trade_date}`} key={stock.code}><b>{stock.name}</b><span>{signedPct(stock.pct_chg)}</span></a>)}</div>
      </article>)}</div>
    </section>
    <section className="intel-section">
      <div className="intel-section__head"><div><span className="eyebrow">角色与生命周期</span><h3>龙头雷达</h3></div><a href="/dragon">查看龙头池 <ArrowRight size={14} /></a></div>
      <div className="leader-grid">{data.leaders.rows.map((row) => <a className="leader-card" href={`/stock/${row.code}?date=${data.trade_date}`} key={row.code}>
        <div><strong>{row.name}</strong><span>{row.code}</span><em>{row.pool_type}</em></div>
        <div className="leader-metrics"><span>{row.primary_role || "龙头观察"}</span><b>{row.leader_score.toFixed(1)}</b></div>
        <p>{row.lifecycle_state || row.leader_time_label} · {row.primary_sector || "题材待确认"}</p>
      </a>)}</div>
    </section>
    <section className="intel-section">
      <div className="intel-section__head"><div><span className="eyebrow">席位资金</span><h3>龙虎榜摘要</h3></div><a href={`/data/lhb/${data.trade_date}`}>查看龙虎榜 <ArrowRight size={14} /></a></div>
      <div className="lhb-layout"><div>{data.lhb.stocks.map((row) => <div className="money-row" key={row.code}><span><b>{row.name}</b><small>{row.code}</small></span><strong className={row.net_buy_yuan >= 0 ? "price-up" : "price-down"}>{money(row.net_buy_yuan)}</strong></div>)}</div><div>{data.lhb.actors.map((row) => <div className="money-row" key={row.name}><span><b>{row.name}</b><small>{row.stock_count}只股票</small></span><strong className={row.net_buy_yuan >= 0 ? "price-up" : "price-down"}>{money(row.net_buy_yuan)}</strong></div>)}</div></div>
      {!data.lhb.stocks.length && !data.lhb.actors.length && <div className="empty-state">该交易日暂无龙虎榜数据。</div>}
    </section>
  </div>;
}

function Workbench() {
  const root = document.getElementById("workbench-root");
  const initialDate = root?.dataset.initialDate || "";
  const [date, setDate] = useState(initialDate);
  const [data, setData] = useState<WorkbenchData | null>(null);
  const [intelligence, setIntelligence] = useState<IntelligenceData | null>(null);
  const [realtime, setRealtime] = useState<RealtimeData | null>(null);
  const [view, setView] = useState<"decisions" | "intelligence">("decisions");
  const [loading, setLoading] = useState(true);
  const [loadingIntel, setLoadingIntel] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [expandedAvoid, setExpandedAvoid] = useState(false);
  const [query, setQuery] = useState("");
  const [groupFilter, setGroupFilter] = useState("全部");

  async function load(nextDate = date, force = false) {
    setLoading(true); setError("");
    try {
      const value = await fetchData<WorkbenchData>(`/api/v1/workbench${nextDate ? `?date=${encodeURIComponent(nextDate)}` : ""}`, { force });
      setData(value); setDate(value.trade_date || nextDate); setIntelligence(null);
      const status = document.getElementById("workspace-header-status");
      if (status) status.textContent = `更新于 ${value.generated_at || "--"}`;
    } catch (reason) { setError(reason instanceof Error ? reason.message : "工作台数据加载失败"); }
    finally { setLoading(false); }
  }

  async function loadIntelligence(silent = false) {
    if (intelligence?.trade_date === date) return;
    setLoadingIntel(true);
    try { setIntelligence(await fetchData<IntelligenceData>(`/api/v1/workbench/intelligence?date=${encodeURIComponent(date)}`, { ttlMs: 30_000 })); }
    catch (reason) { if (!silent) setError(reason instanceof Error ? reason.message : "市场研判加载失败"); }
    finally { setLoadingIntel(false); }
  }

  async function loadRealtime(force = false) {
    if (!date) return;
    try { setRealtime(await fetchData<RealtimeData>(`/api/v1/workbench/realtime?candidate_date=${encodeURIComponent(date)}&limit=50`, { ttlMs: 1_500, force })); }
    catch { setRealtime(null); }
  }

  async function openCandidate(row: Candidate) {
    setSelected(row);
    try { setSelected(await fetchData<Candidate>(`/api/v1/workbench/candidates/${row.code}?date=${encodeURIComponent(date)}`, { ttlMs: 60_000 })); }
    catch { /* Summary remains usable. */ }
  }

  function prefetchCandidate(row: Candidate) {
    void fetchData<Candidate>(`/api/v1/workbench/candidates/${row.code}?date=${encodeURIComponent(date)}`, { ttlMs: 60_000 }).catch(() => undefined);
    void fetchData<StockWorkspace>(`/api/v1/workbench/stocks/${row.code}?date=${encodeURIComponent(date)}`, { ttlMs: 60_000 }).catch(() => undefined);
  }

  useEffect(() => { void load(initialDate); }, []);
  useEffect(() => { if (date) void loadRealtime(); }, [date]);
  useEffect(() => {
    const seconds = realtime?.refresh_policy?.auto_refresh ? realtime.refresh_policy.interval_seconds : 0;
    if (!seconds) return undefined;
    const timer = window.setTimeout(() => void loadRealtime(), seconds * 1000);
    return () => window.clearTimeout(timer);
  }, [realtime?.generated_at, realtime?.refresh_policy?.auto_refresh, date]);
  useEffect(() => { if (view === "intelligence") void loadIntelligence(); }, [view, date]);
  useEffect(() => {
    if (!date || intelligence?.trade_date === date) return undefined;
    const timer = window.setTimeout(() => void loadIntelligence(true), 700);
    return () => window.clearTimeout(timer);
  }, [date, intelligence?.trade_date]);

  const realtimeMap = useMemo(() => new Map((realtime?.rows || []).map((row) => [row.code, row])), [realtime]);
  const riskFlags = useMemo(() => data?.market.risk_flags || [], [data]);
  const marketContext = data?.market_context || {};
  const indices = (marketContext.indices || []).filter((item) => ["上证", "深证", "创业板"].includes(item.name));
  const promotionRate = marketContext.promotion?.overall;
  const profitEffect = marketContext.profit_effect || {};
  const promotionTrend = marketContext.promotion_trend || {};
  const promotionItems = [
    ["一进二", marketContext.promotion?.rate_1to2],
    ["二进三", marketContext.promotion?.rate_2to3],
    ["三进四", marketContext.promotion?.rate_3to4],
    ["高位晋级", marketContext.promotion?.rate_high],
  ] as const;
  const normalizedQuery = query.trim().toLowerCase();

  if (loading && !data) return <div className="workspace-loading">正在读取今日决策池...</div>;
  if (error && !data) return <div className="workspace-error"><ShieldAlert size={20} />{error}<button onClick={() => void load()}>重试</button></div>;
  if (!data) return null;

  return <div className="workbench-shell">
    <section className="market-hero">
      <div className="market-hero__copy"><div className="eyebrow">今日市场</div><h2>{data.market.regime_label || "状态待确认"}<span>{data.market.emotion_phase || ""}</span></h2><p>{data.market_brief}</p><div className="risk-tags">{riskFlags.length ? riskFlags.map((flag) => <span key={flag}>{flag}</span>) : <span className="quiet">暂无额外风险标签</span>}</div></div>
      <div className="market-score"><span>市场分</span><strong>{Number(data.market.market_score || 0).toFixed(0)}</strong><small>建议总仓 {Math.round((data.market.position_scale || 0) * 100)}%</small></div>
      <div className="market-controls"><select value={date} onChange={(event) => void load(event.target.value)} aria-label="选择交易日">{(data.available_dates || []).map((item) => <option key={item} value={item}>{item}</option>)}</select><button className="icon-button" type="button" onClick={() => void load(date, true)} title="刷新" disabled={loading}><RefreshCw size={17} /></button></div>
    </section>

    <section className="profit-effect" aria-label="市场赚钱效应">
      <div className="profit-effect__score">
        <span>赚钱效应</span>
        <strong className={profitEffect.score == null ? "" : Number(profitEffect.score) >= 60 ? "up" : Number(profitEffect.score) < 45 ? "down" : ""}>{profitEffect.score == null ? "--" : Number(profitEffect.score).toFixed(0)}</strong>
        <div><b>{profitEffect.label || "等待盘后计算"}</b><em>{profitEffect.trend || ""}{profitEffect.change_3d == null ? "" : ` ${Number(profitEffect.change_3d) >= 0 ? "+" : ""}${Number(profitEffect.change_3d).toFixed(1)}分`}</em></div>
      </div>
      <div className="profit-effect__evidence">
        <div><span>上涨占比</span><strong>{profitEffect.up_ratio == null ? "--" : `${Number(profitEffect.up_ratio).toFixed(1)}%`}</strong></div>
        <div><span>昨日涨停溢价</span><strong className={Number(profitEffect.prev_limit_up_premium || 0) >= 0 ? "up" : "down"}>{profitEffect.prev_limit_up_premium == null ? "--" : signedPct(profitEffect.prev_limit_up_premium)}</strong></div>
        <div><span>连板晋级</span><strong>{profitEffect.promotion_rate == null ? "--" : `${Number(profitEffect.promotion_rate).toFixed(1)}%`}</strong><small>{profitEffect.promotion_sample ? `${Number(profitEffect.promotion_success || 0).toFixed(0)}/${Number(profitEffect.promotion_sample).toFixed(0)}` : ""}</small></div>
        <div><span>炸板率</span><strong>{profitEffect.broken_rate == null ? "--" : `${Number(profitEffect.broken_rate).toFixed(1)}%`}</strong></div>
      </div>
    </section>

    <section className="index-strip" aria-label="三大指数">
      {indices.map((item) => <div key={item.name}><span>{item.name}</span><strong className={item.pct >= 0 ? "up" : "down"}>{signedPct(item.pct)}</strong><small>{Number(item.close || 0).toFixed(2)}</small></div>)}
      {!indices.length && <div className="market-data-empty">暂无三大指数数据</div>}
    </section>

    <section className="stat-strip">
      <div><Activity size={17} /><span>涨停 / 跌停</span><strong><em className="up">{data.market.limit_up_count}</em> / <em className="down">{data.market.limit_down_count}</em></strong></div>
      <div><Activity size={17} /><span>上涨 / 下跌</span><strong><em className="up">{marketContext.up_count ?? "--"}</em> / <em className="down">{marketContext.down_count ?? "--"}</em></strong></div>
      <div><Target size={17} /><span>连板晋级率</span><strong>{promotionRate == null ? "--" : `${Number(promotionRate).toFixed(1)}%`}</strong></div>
      <div><BarChart3 size={17} /><span>市场量能</span><strong>{marketContext.vol_word || "--"}{marketContext.vol_pct == null ? "" : ` ${signedPct(marketContext.vol_pct)}`}</strong></div>
      <div><BarChart3 size={17} /><span>炸板率</span><strong>{Number(data.market.broken_rate || 0).toFixed(1)}%</strong></div>
      <div><Target size={17} /><span>可行动</span><strong>{data.decision_summary.actionable}</strong></div>
    </section>

    <section className="promotion-strip" aria-label="连板晋级梯队">
      <span className="promotion-strip__title">晋级梯队</span>
      {promotionItems.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value == null ? "--" : `${Number(value).toFixed(1)}%`}</strong></div>)}
    </section>

    <section className="promotion-trend" aria-label="连板晋级趋势">
      <div className="promotion-trend__summary">
        <span>近5日接力趋势</span>
        <strong>{promotionTrend.score == null ? "--" : Number(promotionTrend.score).toFixed(0)}</strong>
        <div><b>{promotionTrend.label || "等待盘后计算"}</b><em>{promotionTrend.slope == null ? `${promotionTrend.sample_days || 0}日样本` : `日均斜率 ${Number(promotionTrend.slope) >= 0 ? "+" : ""}${Number(promotionTrend.slope).toFixed(1)}点`}</em></div>
      </div>
      <div className="promotion-trend__visual">
        <div className="promotion-trend__legend"><span className="tier-1">一进二</span><span className="tier-2">二进三</span><span className="tier-3">三进四</span><span className="tier-high">高位晋级</span></div>
        {promotionTrend.history?.length ? <PromotionTrendChart rows={promotionTrend.history} /> : <div className="promotion-trend__empty">历史样本尚未形成</div>}
      </div>
    </section>

    <div className="workspace-toolbar">
      <div className="view-tabs" role="tablist"><button className={view === "decisions" ? "active" : ""} onClick={() => setView("decisions")}><Target size={15} />今日决策</button><button className={view === "intelligence" ? "active" : ""} onClick={() => setView("intelligence")}><Flame size={15} />市场研判</button></div>
      <div className={`session-status ${realtime?.refresh_policy?.auto_refresh ? "live" : ""}`}><span />{realtime?.refresh_policy?.session_label || "行情缓存待连接"}{realtime?.generated_at && <small>{realtime.generated_at.slice(11, 19)}</small>}<button type="button" title="刷新缓存行情" onClick={() => void loadRealtime(true)}><RefreshCw size={13} /></button></div>
    </div>

    {error && <div className="inline-error"><ShieldAlert size={15} />{error}<button onClick={() => setError("")}><X size={14} /></button></div>}

    {view === "intelligence" ? <IntelligenceView data={intelligence} loading={loadingIntel} /> : <>
      {(data.crowding_summary || []).some((item) => item.level !== "正常") && <section className="crowding-alert">
        <ShieldAlert size={17} />
        <div>
          <strong>题材拥挤控制已生效</strong>
          <p>{(data.crowding_summary || []).filter((item) => item.level !== "正常").map((item) => `${item.cluster} ${item.count}只（${item.ratio_pct.toFixed(0)}%，${item.level}）`).join("；")}。当前市场每个主题最多保留 {data.cluster_limits?.focus_per_cluster || 1} 只重点、{data.cluster_limits?.active_per_cluster || 2} 只可行动标的，其余降为暂不参与。</p>
        </div>
      </section>}
      <section className="decision-tools">
        <label><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索股票、代码、策略或主线" /></label>
        <div className="group-filter">{["全部", "重点确认", "盘中观察", "暂不参与"].map((item) => <button type="button" key={item} className={groupFilter === item ? "active" : ""} onClick={() => setGroupFilter(item)}>{item}</button>)}</div>
      </section>
      <section className="quick-links">{data.quick_links.map((link) => <a key={link.href} href={link.href}>{link.label}<ArrowRight size={14} /></a>)}</section>
      {GROUPS.map((group) => {
        if (groupFilter !== "全部" && groupFilter !== group.key) return null;
        const rows = (data.groups[group.key] || []).filter((row) => !normalizedQuery || [row.name, row.code, row.mainline, ...(row.hit_strategies || [])].join(" ").toLowerCase().includes(normalizedQuery));
        const collapsed = group.key === "暂不参与" && !expandedAvoid && groupFilter === "全部" && !normalizedQuery;
        return <section className={`decision-section decision-section--${group.tone}`} key={group.key}>
          <div className="decision-section__head"><div><h3>{group.key}<span>{rows.length}</span></h3><p>{group.description}</p></div>{group.key === "暂不参与" && rows.length > 0 && groupFilter === "全部" && !normalizedQuery && <button type="button" className="text-button" onClick={() => setExpandedAvoid(!expandedAvoid)}>{expandedAvoid ? "收起" : "展开"}<ChevronDown size={15} className={expandedAvoid ? "rotate" : ""} /></button>}</div>
          {!collapsed && rows.length > 0 && <div className="candidate-list">{rows.map((row) => <CandidateCard key={row.code} row={row} realtime={realtimeMap.get(row.code)} onOpen={() => void openCandidate(row)} onPrefetch={() => prefetchCandidate(row)} />)}</div>}
          {rows.length === 0 && <div className="empty-state">没有符合当前筛选条件的股票。</div>}
        </section>;
      })}
    </>}
    <DetailDrawer candidate={selected} date={date} onClose={() => setSelected(null)} />
  </div>;
}

const root = document.getElementById("workbench-root");
if (root) createRoot(root).render(<Workbench />);
