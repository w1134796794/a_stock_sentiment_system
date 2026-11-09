import { useId, type ReactNode } from "react";
import { ArrowRight, ArrowUpRight, ArrowDownRight, BarChart3, Minus, TrendingUp } from "lucide-react";
import type { IntelligenceData, WorkbenchData } from "./types";

const number = (value: number | null | undefined, digits = 0) => value == null || !Number.isFinite(Number(value)) ? "--" : Number(value).toFixed(digits);
const percent = (value: number | null | undefined) => value == null ? "--" : `${number(value, 1)}%`;
const signed = (value: number | null | undefined) => value == null ? "--" : `${value >= 0 ? "+" : ""}${number(value, 2)}%`;
const bound = (value: number) => Math.max(0, Math.min(100, value));
function clock(value: string | undefined) {
  const text = String(value || "");
  if (/^\d{2}:\d{2}:\d{2}$/.test(text)) return text;
  const digits = text.replace(/:00$/, "").replace(/\.0$/, "");
  if (!/^\d{5,6}$/.test(digits)) return "--";
  const padded = digits.padStart(6, "0");
  return `${padded.slice(0, 2)}:${padded.slice(2, 4)}:${padded.slice(4)}`;
}

function MarketGauge({ score }: { score?: number | null }) {
  const mask = useId();
  const valid = score != null && Number.isFinite(Number(score));
  return <div className="terminal-gauge" aria-label={`市场评分 ${valid ? number(score) : "未评估"}`}>
    <svg viewBox="0 0 240 140" aria-hidden="true">
      <defs><mask id={mask}><path d="M 22 118 A 98 98 0 0 1 218 118" pathLength="100" stroke="white" strokeWidth="14" strokeDasharray="3 1" /></mask></defs>
      <path className="gauge-track" d="M 22 118 A 98 98 0 0 1 218 118" pathLength="100" />
      {valid && <path className="gauge-fill" mask={`url(#${mask})`} d="M 22 118 A 98 98 0 0 1 218 118" pathLength="100" strokeDasharray={`${bound(Number(score))} 100`} />}
    </svg>
    <div><span>市场评分</span><strong>{number(score)}<small>/ 100</small></strong></div>
  </div>;
}

export function TerminalOverview({ data, intelligence, loadingIntel, chart }: {
  data: WorkbenchData; intelligence: IntelligenceData | null; loadingIntel: boolean; chart: ReactNode;
}) {
  const { market } = data;
  const context = data.market_context || {};
  const profit = context.profit_effect || {};
  const trend = context.promotion_trend || {};
  const position = market.position_scale == null ? null : bound(market.position_scale * 100);
  const tiers = [
    ["一进二", "rate_1to2", "mint"], ["二进三", "rate_2to3", "amber"],
    ["三进四", "rate_3to4", "red"], ["高位晋级", "rate_high", "blue"],
  ] as const;
  const breadth = [context.up_count, context.down_count, context.flat_count];
  const completeBreadth = breadth.every(value => value != null && Number.isFinite(value) && value >= 0);
  const total = completeBreadth ? breadth.reduce<number>((sum, value) => sum + Number(value), 0) : 0;
  const labels = ["上涨", "下跌", "平盘"];
  const echelon = intelligence?.limitup.echelon || [];
  const amount = context.amount_text || (market.amount_yuan == null ? "--" : `${(market.amount_yuan / 1e12).toFixed(2)}万亿`);
  return <div className="terminal-overview">
    <section className="terminal-hero" aria-label="市场状态与风险预算">
      <div className="terminal-regime"><span className="terminal-eyebrow">今日市场</span>
        <div><h2>{market.regime_label || "状态待确认"}</h2><b>{market.emotion_phase}</b></div>
        <p>{data.market_brief || "市场证据待更新"}</p>
        {!!market.risk_flags?.length && <div className="terminal-risks">{market.risk_flags.map(flag => <span key={flag}>{flag}</span>)}</div>}
      </div>
      <MarketGauge score={market.market_score} />
      <div className="terminal-budget"><span>建议总仓位</span><strong>{position == null ? "--" : `${Math.round(position)}%`}</strong>
        <div className="budget-bars" aria-hidden="true">{Array.from({ length: 40 }, (_, index) => <i className={position != null && index < Math.round(position / 2.5) ? "filled" : ""} key={index} />)}</div>
        <small>市场风险预算</small>
      </div>
    </section>

    <section className="terminal-indices" aria-label="三大指数">
      {["上证", "深证", "创业板"].map(name => {
        const item = context.indices?.find(index => index.name === name);
        const tone = item?.pct == null ? "" : item.pct >= 0 ? "rise" : "fall";
        return <article key={name}><div><span>{name === "上证" ? "上证指数" : name === "深证" ? "深证成指" : "创业板指"}</span><strong className={tone}>{number(item?.close, 2)}</strong></div>
          <div className={`index-change ${tone}`}>{item?.pct == null ? <Minus size={20} /> : item.pct >= 0 ? <ArrowUpRight size={20} /> : <ArrowDownRight size={20} />}<b>{signed(item?.pct)}</b></div>
        </article>;
      })}
    </section>

    <section className="terminal-metrics" aria-label="市场核心指标">
      <article><span>赚钱效应</span><strong>{number(profit.score)}</strong><small>{profit.label || "未评估"}</small></article>
      <article><span>上涨占比</span><strong className="rise">{percent(profit.up_ratio ?? (total ? Number(context.up_count) / total * 100 : null))}</strong><small>全市场参与面</small></article>
      <article><span>涨停 / 跌停</span><strong><em className="rise">{number(market.limit_up_count)}</em><em className="metric-divider"> / </em><em className="fall">{number(market.limit_down_count)}</em></strong><small>市场高度与风险</small></article>
      <article><span>连板晋级</span><strong>{percent(context.promotion?.overall)}</strong><small>昨日梯队接力</small></article>
      <article><span>炸板率</span><strong className="caution">{percent(profit.broken_rate ?? market.broken_rate)}</strong><small>封板稳定性</small></article>
      <article><span>市场成交 <BarChart3 size={13} /></span><strong>{amount}</strong><small>{context.vol_word || "量能待更新"}{context.vol_pct == null ? "" : ` ${signed(context.vol_pct)}`}</small></article>
    </section>

    <div className="terminal-middle">
      <section className="terminal-trend" aria-label="连板晋级趋势">
        <header><h3>接力情绪 <span>近5日</span></h3><div>{trend.label || "未评估"}<b>{number(trend.score)}</b></div></header>
        <div className="terminal-legend">{tiers.map(([label, , tone]) => <span className={tone} key={label}><i />{label}</span>)}</div>
        {chart}
      </section>
      <section className="terminal-tiers" aria-label="连板晋级梯队"><header><h3>晋级结构</h3><small>晋级 / 昨日样本</small></header>
        {tiers.map(([label, key, tone]) => {
          const value = context.promotion?.[key];
          const success = context.promotion?.[`${key}_success`];
          const sample = context.promotion?.[`${key}_sample`];
          return <div className="tier-row" key={key}><span>{label}</span><div className={`tier-track ${tone}`}><i style={{ width: `${value == null ? 0 : bound(value)}%` }} /></div><strong>{percent(value)}</strong><small>{number(success)} / {number(sample)}</small></div>;
        })}
      </section>
    </div>

    <div className="terminal-bottom">
      <section className="terminal-breadth" aria-label="市场广度"><header><h3>市场广度</h3><TrendingUp size={17} /></header>
        <div className="breadth-values">{breadth.map((value, index) => <div className={index === 0 ? "rise" : index === 1 ? "fall" : "neutral"} key={labels[index]}><span>{labels[index]}</span><strong>{number(value)}</strong><small>{total ? percent(Number(value) / total * 100) : "--"}</small></div>)}</div>
        <div className="breadth-bar" aria-label={total ? `共 ${total} 只` : "市场广度数据不完整"}>{total ? breadth.map((value, index) => <span key={labels[index]} style={{ width: `${Number(value) / total * 100}%` }} className={`breadth-${index}`} />) : <span className="breadth-missing" />}</div>
        <div className="breadth-foot"><span>昨日涨停溢价</span><b className={profit.prev_limit_up_premium == null ? "" : profit.prev_limit_up_premium >= 0 ? "rise" : "fall"}>{signed(profit.prev_limit_up_premium)}</b></div>
      </section>
      <section className="terminal-echelon" aria-label="涨停梯队摘要"><header><h3>连板梯队</h3><a href={`/data/limitup/${data.trade_date}`}>全部梯队 <ArrowRight size={13} /></a></header>
        <div className="echelon-table-scroll"><table><thead><tr><th>梯队</th><th>数量</th><th>代表个股</th><th>首次封板</th><th>开板次数</th></tr></thead><tbody>
          {echelon.filter(row => row.board_height >= 2).slice(0, 5).map(row => <tr key={row.board_height}><td><b>{row.board_height}板</b></td><td>{row.count}</td><td>{row.stocks.slice(0, 2).map(stock => <a key={stock.code} href={`/stock/${stock.code}?date=${data.trade_date}`}>{stock.name}</a>)}</td><td>{clock(row.stocks[0]?.first_time)}</td><td>{row.stocks[0]?.open_times ?? "--"}</td></tr>)}
          {!echelon.some(row => row.board_height >= 2) && <tr><td colSpan={5} className="terminal-empty">{loadingIntel ? "正在读取梯队…" : intelligence ? "暂无连板梯队" : "梯队数据暂不可用"}</td></tr>}
        </tbody></table></div>
      </section>
    </div>
    <footer className="terminal-footer"><span>数据更新 {data.generated_at || "--"}</span><a href={`/market-review?date=${data.trade_date}`}>市场复盘 / 市场研判 <ArrowRight size={14} /></a></footer>
  </div>;
}
