"""Run a compact three-month strategy health backtest after daily generation."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class AutoBacktestReportService:
    def __init__(self, output_dir: Optional[Path] = None) -> None:
        if output_dir is None:
            from config.settings import WEB_DATA_DIR

            output_dir = Path(WEB_DATA_DIR) / "reports" / "strategy_health"
        self.output_dir = Path(output_dir)

    def run(self, end_date: str, *, capital: float = 100_000.0) -> Dict[str, Any]:
        from backtest.backtest_engine import BacktestConfig, BacktestEngine
        from backtest.plan_source import build_backtest_plan_dir
        from config.settings import CACHE_DIR, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
        from core.data.data_manager_main import DataManager
        from core.utils.date_utils import DateUtils
        from risk.capital_presets import apply_capital_preset
        from risk.risk_config import RiskConfig

        dates = DateUtils().get_last_n_trade_dates(65, str(end_date))
        if not dates:
            return self._write_no_data(str(end_date), "交易日历中没有可用日期")
        start_date = min(dates)
        end_date = max(dates)
        plan_dir, file_count, row_count = build_backtest_plan_dir(
            snapshot_dir=Path(SNAPSHOT_DIR),
            output_dir=Path(WEB_DATA_DIR) / "runtime" / "automatic_backtest",
            screening_dir=Path(WEB_DATA_DIR) / "screening",
            start_date=start_date,
            end_date=end_date,
            max_rank=0,
        )
        if file_count <= 0:
            return self._write_no_data(end_date, "最近3个月没有可回测候选")

        dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
        config = BacktestConfig.from_risk_config(
            RiskConfig.load(), initial_capital=capital, risk_control=True,
        )
        preset = apply_capital_preset(config, capital)
        engine = BacktestEngine(dm, config)
        result = engine.run_backtest(start_date, end_date, str(plan_dir))
        closed = [trade for trade in result.get("trade_history") or [] if str(getattr(trade, "action", "")).startswith("SELL")]
        monthly: Dict[str, list[float]] = defaultdict(list)
        for trade in closed:
            month = str(getattr(trade, "date", ""))[:6]
            if month:
                monthly[month].append(_number(getattr(trade, "pnl_pct", 0.0)))
        win_rate_trend = [
            {
                "month": month,
                "trades": len(values),
                "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2),
                "average_return_pct": round(sum(values) / len(values) * 100, 2),
            }
            for month, values in sorted(monthly.items())
        ]
        regime_distribution = self._regime_distribution(start_date, end_date)
        payload = {
            "ok": True,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "start_date": start_date,
            "end_date": end_date,
            "capital_preset": preset.to_dict(),
            "plan_days": file_count,
            "candidate_rows": row_count,
            "closed_trades": int(result.get("closed_trades") or len(closed)),
            "win_rate_pct": round(_number(result.get("win_rate")) * 100, 2),
            "total_return_pct": round(_number(result.get("total_return")) * 100, 2),
            "max_drawdown_pct": round(_number(result.get("max_drawdown")) * 100, 2),
            "win_rate_trend": win_rate_trend,
            "market_regime_distribution": regime_distribution,
        }
        return self._write(payload)

    def _regime_distribution(self, start_date: str, end_date: str) -> Dict[str, int]:
        from config.settings import WEB_DATA_DIR

        counts: Counter[str] = Counter()
        seen_dates = set()
        labels = {"strong": "强势", "neutral": "震荡", "weak": "弱势"}
        for path in sorted((Path(WEB_DATA_DIR) / "screening").glob("screening_*.json")):
            date = path.stem.replace("screening_", "").split("_", 1)[0]
            if not (start_date <= date <= end_date) or date in seen_dates:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                regime = str((payload.get("weight_metadata") or {}).get("market_regime") or "neutral")
                counts[labels.get(regime, "震荡")] += 1
                seen_dates.add(date)
            except (OSError, ValueError, TypeError):
                continue
        return dict(counts)

    def _write_no_data(self, end_date: str, reason: str) -> Dict[str, Any]:
        return self._write({
            "ok": False,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "end_date": end_date,
            "reason": reason,
        })

    def _write(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        end_date = str(payload.get("end_date") or "unknown")
        json_path = self.output_dir / f"strategy_health_{end_date}.json"
        md_path = self.output_dir / f"strategy_health_{end_date}.md"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=self._json_default), encoding="utf-8")
        md_path.write_text(self._markdown(payload), encoding="utf-8")
        latest = self.output_dir / "latest.json"
        latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=self._json_default), encoding="utf-8")
        payload["json_path"] = str(json_path)
        payload["markdown_path"] = str(md_path)
        logger.info(f"[AutoBacktest] 最近3个月策略健康报告已生成: {md_path}")
        return payload

    @staticmethod
    def _markdown(payload: Dict[str, Any]) -> str:
        if not payload.get("ok"):
            return f"# 策略健康报告\n\n未生成：{payload.get('reason') or '数据不足'}。\n"
        lines = [
            f"# 策略健康报告（{payload.get('start_date')} - {payload.get('end_date')}）",
            "",
            f"- 总收益：{payload.get('total_return_pct'):.2f}%",
            f"- 胜率：{payload.get('win_rate_pct'):.2f}%",
            f"- 最大回撤：{payload.get('max_drawdown_pct'):.2f}%",
            f"- 已平仓：{payload.get('closed_trades')} 笔",
            "",
            "## 近期胜率趋势",
            "",
            "| 月份 | 交易数 | 胜率 | 平均收益 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for row in payload.get("win_rate_trend") or []:
            lines.append(f"| {row['month']} | {row['trades']} | {row['win_rate_pct']:.2f}% | {row['average_return_pct']:+.2f}% |")
        lines.extend(["", "## 市场状态分布", ""])
        for name, count in (payload.get("market_regime_distribution") or {}).items():
            lines.append(f"- {name}：{count} 个交易日")
        lines.extend(["", "> 自动报告用于检查策略近期是否仍然有效，不构成投资建议。", ""])
        return "\n".join(lines)

    @staticmethod
    def _json_default(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        return str(value)


__all__ = ["AutoBacktestReportService"]
