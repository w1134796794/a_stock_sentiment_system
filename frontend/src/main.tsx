import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { ColorType, createChart, LineSeries } from "lightweight-charts";
import {
  ArrowRight,
  RefreshCw,
  ShieldAlert,
  X,
} from "lucide-react";
import type {
  IntelligenceData,
  WorkbenchData,
} from "./types";
import "./styles.css";
import "./terminal.css";
import { TerminalOverview } from "./TerminalOverview";

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

function PromotionTrendChart({ rows }: { rows: NonNullable<WorkbenchData["market_context"]["promotion_trend"]>["history"] }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current || !rows?.length) return undefined;
    const chart = createChart(host.current, {
      height: 260,
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0d1318" }, textColor: "#88999f" },
      grid: { vertLines: { color: "#202c32" }, horzLines: { color: "#202c32" } },
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

function IntelligenceView({ data, loading }: { data: IntelligenceData | null; loading: boolean }) {
  if (loading && !data) return <div className="workspace-loading">正在聚合涨停、主线与龙头...</div>;
  if (!data) return <div className="empty-state">市场研判数据暂不可用。</div>;
  return <div className="intelligence-layout">
    <section className="intel-section">
      <div className="intel-section__head"><div><span className="eyebrow">题材强度</span><h3>主线观察</h3></div><a href={`/data/sector/${data.trade_date}`}>查看板块热度 <ArrowRight size={14} /></a></div>
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
      <div className="intel-section__head"><div><span className="eyebrow">角色与生命周期</span><h3>龙头雷达</h3></div><a href={`/dragon?date=${data.trade_date}`}>查看龙头池 <ArrowRight size={14} /></a></div>
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
  const review = root?.dataset.view === "intelligence";
  const initialDate = root?.dataset.initialDate || "";
  const [date, setDate] = useState(initialDate);
  const [data, setData] = useState<WorkbenchData | null>(null);
  const [intelligence, setIntelligence] = useState<IntelligenceData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingIntel, setLoadingIntel] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const requestId = useRef(0);

  async function load(nextDate = date, force = false) {
    const id = ++requestId.current;
    setLoading(true);
    setError("");
    setIntelligence(null);
    try {
      const value = await fetchData<WorkbenchData>(`/api/v1/workbench${nextDate ? `?date=${encodeURIComponent(nextDate)}` : ""}`, { force });
      if (id !== requestId.current) return;
      setData(value);
      setDate(value.trade_date || nextDate);
      setRevision(value => value + 1);
    } catch (reason) {
      if (id === requestId.current) setError(reason instanceof Error ? reason.message : "市场数据加载失败");
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }

  useEffect(() => { void load(initialDate); }, []);
  useEffect(() => {
    if (!data?.trade_date) return;
    let active = true;
    setLoadingIntel(true);
    const selectedDate = data.trade_date;
    void fetchData<IntelligenceData>(`/api/v1/workbench/intelligence?date=${encodeURIComponent(selectedDate)}`, { ttlMs: 30_000, force: true })
      .then(value => { if (active) setIntelligence(value); })
      .catch(() => { if (active) setIntelligence(null); })
      .finally(() => { if (active) setLoadingIntel(false); });
    return () => { active = false; };
  }, [data?.trade_date, revision]);

  if (loading && !data) return <div className="workspace-loading">正在读取市场数据...</div>;
  if (!data) return <div className="workspace-error"><ShieldAlert size={20} />{error || "市场数据暂不可用"}<button onClick={() => void load()}>重试</button></div>;
  const trend = data.market_context?.promotion_trend;
  return <div className="workbench-shell terminal-shell" aria-busy={loading}>
    <div className="terminal-toolbar">
      <div><span className="terminal-eyebrow">{review ? "市场复盘" : "市场全景"}</span><h2>{review ? "市场研判" : "交易总览"}</h2></div>
      <div className="terminal-controls"><label htmlFor="terminal-date">交易日</label><select id="terminal-date" value={date} onChange={event => void load(event.target.value)} aria-label="选择交易日">{Array.from(new Set([date, ...(data.available_dates || [])])).filter(Boolean).map(item => <option key={item} value={item}>{item}</option>)}</select><span className="terminal-data-status">{data.data_status === "ready" ? "已归档" : data.data_status === "partial" ? "部分就绪" : "待更新"}</span><button className="icon-button" type="button" onClick={() => void load(date, true)} aria-label="刷新市场数据" title="刷新市场数据" disabled={loading}><RefreshCw size={16} className={loading ? "terminal-spinning" : ""} /></button></div>
    </div>
    {error && <div className="inline-error" role="alert"><ShieldAlert size={15} />{error}<button aria-label="关闭错误提示" onClick={() => setError("")}><X size={14} /></button></div>}
    {loading && <div className="terminal-pending" role="status">正在读取所选交易日...</div>}
    {!loading && (review ? <IntelligenceView data={intelligence} loading={loadingIntel} /> : <TerminalOverview data={data} intelligence={intelligence} loadingIntel={loadingIntel} chart={trend?.history?.length ? <PromotionTrendChart rows={trend.history} /> : <div className="promotion-trend__empty">暂无接力历史数据</div>} />)}
  </div>;
}

const root = document.getElementById("workbench-root");
if (root) createRoot(root).render(<Workbench />);
