"""Daily analysis pipeline based on prefetched data and factor tables.

Main path:
prefetch normalized data -> compute factors -> screen candidates ->
write snapshot-ready analysis payload.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.data.data_prep import DataPrep
from core.factors.jobs.runner import FactorJobRunner
from core.screening.gold_analysis import build_gold_analysis_summary
from core.screening.screening_engine import ScreeningEngine


@dataclass
class ETLDailyResult:
    trade_date: str
    prev_trade_date: str = ""
    silver_summary: Dict[str, Any] = field(default_factory=dict)
    factor_results: List[Dict[str, Any]] = field(default_factory=list)
    screening: Dict[str, Any] = field(default_factory=dict)
    plan_cache_summary: Dict[str, Any] = field(default_factory=dict)
    gold_summary: Dict[str, Any] = field(default_factory=dict)
    snapshot_paths: Dict[str, str] = field(default_factory=dict)
    analysis_path: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(
            self.silver_summary
            and all(item.get("ok") for item in self.factor_results)
            and self.screening.get("ok")
            and self.gold_summary.get("ok")
        )


class ETLDailyPipeline:
    """Run the daily analysis pipeline and write Web-facing artifacts."""

    index_codes = ["000001.SH", "399001.SZ", "399006.SZ"]

    def __init__(
        self,
        data_manager: Any,
        *,
        duckdb_path: Optional[Path] = None,
        web_data_dir: Optional[Path] = None,
        snapshot_dir: Optional[Path] = None,
        app_db_path: Optional[Path] = None,
        kb_db_path: Optional[Path] = None,
        ingest_kb: bool = True,
    ):
        from config.settings import APP_DB_PATH, FACTOR_DB_PATH, KB_DB_PATH, SNAPSHOT_DIR, WEB_DATA_DIR

        self.dm = data_manager
        self.data_prep = DataPrep(data_manager)
        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
        self.web_data_dir = Path(web_data_dir or WEB_DATA_DIR)
        self.snapshot_dir = Path(snapshot_dir or SNAPSHOT_DIR)
        self.app_db_path = Path(app_db_path or APP_DB_PATH)
        self.kb_db_path = Path(kb_db_path or KB_DB_PATH)
        self.ingest_kb = bool(ingest_kb)

    def run(self, trade_date: str, prev_trade_date: str = "", *, profile: str = "default") -> ETLDailyResult:
        from snapshot import SnapshotWriter

        trade_date = str(trade_date)
        prev_trade_date = str(prev_trade_date or "")
        result = ETLDailyResult(trade_date=trade_date, prev_trade_date=prev_trade_date)

        logger.info(f"[数据生成] 开始主流程: {trade_date}, prev={prev_trade_date or '-'}")

        phase_started = time.monotonic()
        logger.info(f"[数据生成][Phase1] 数据预取与 Silver 落盘开始: {trade_date}")
        zt_pool = self._safe_df(lambda: self.dm.get_limit_up_pool(trade_date), "今日涨停池")
        prev_zt_pool = self._safe_df(lambda: self.dm.get_limit_up_pool(prev_trade_date), "昨日涨停池") if prev_trade_date else pd.DataFrame()

        dataset = self.data_prep.build(
            trade_date,
            prev_trade_date,
            zt_pool=zt_pool,
            prev_zt_pool=prev_zt_pool,
            index_codes=self.index_codes,
            prefetch_universe_daily=False,
            persist_silver=True,
            warehouse_path=self.duckdb_path,
            silver_dir=self.web_data_dir / "warehouse" / "silver",
            quality_dir=self.web_data_dir / "etl_quality",
        )
        result.silver_summary = dict(dataset.meta.get("silver_persist") or {})
        if not result.silver_summary:
            result.warnings.append("silver_summary 为空，Phase 1 可能未成功落盘")
        logger.info(
            f"[数据生成][Phase1] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s"
        )

        phase_started = time.monotonic()
        logger.info(f"[数据生成][Phase2] 因子计算开始: {trade_date}")
        factor_results = FactorJobRunner(self.duckdb_path).run(trade_date)
        result.factor_results = [item.to_dict() for item in factor_results]
        failed = [item for item in result.factor_results if not item.get("ok")]
        if failed:
            result.warnings.append(f"因子任务失败: {[item.get('name') for item in failed]}")
        logger.info(
            f"[数据生成][Phase2] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s, "
            f"失败={len(failed)}"
        )

        phase_started = time.monotonic()
        logger.info(f"[数据生成][Phase3] 指标筛选开始: {trade_date}, profile={profile}")
        screening = ScreeningEngine(
            duckdb_path=self.duckdb_path,
            output_dir=self.web_data_dir / "screening",
        ).run(trade_date, profile=profile, persist=True)
        result.screening = screening.to_dict()
        if not screening.ok:
            result.warnings.append(f"筛选失败: {screening.message}")
        logger.info(
            f"[数据生成][Phase3] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s, "
            f"候选={len(result.screening.get('final') or [])}"
        )

        # 当前候选与上一交易日计划的当日行情都从 Phase1 全市场日缓存切片落盘。
        # 后续回测、竞价确认和页面服务不得再为这些股票逐票请求 Tushare daily。
        current_codes = [str(item.get("code") or "") for item in result.screening.get("final") or []]
        previous_codes = self._snapshot_plan_codes(prev_trade_date)
        if hasattr(self.dm, "warm_trade_plan_daily_cache"):
            result.plan_cache_summary = self.dm.warm_trade_plan_daily_cache(
                trade_date, current_codes + previous_codes,
            )
            logger.info(
                f"[数据生成][候选行情缓存] {trade_date}: "
                f"{result.plan_cache_summary.get('cached', 0)}/"
                f"{result.plan_cache_summary.get('requested', 0)} 只已落本地"
            )

        phase_started = time.monotonic()
        logger.info(f"[数据生成][Phase4] 分析摘要开始: {trade_date}")
        result.gold_summary = build_gold_analysis_summary(
            trade_date,
            duckdb_path=self.duckdb_path,
            screening_dir=self.web_data_dir / "screening",
        )
        result.analysis_path = str(self._write_analysis_json(result.gold_summary, self.web_data_dir / "screening", trade_date))
        logger.info(
            f"[数据生成][Phase4] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s"
        )

        phase_started = time.monotonic()
        logger.info(f"[数据生成][Phase5] 页面快照开始: {trade_date}")
        data_dict = self.build_snapshot_data(result)
        result.snapshot_paths = SnapshotWriter(self.snapshot_dir, self.app_db_path, self.duckdb_path).write(data_dict)
        if self.ingest_kb:
            self._ingest_kb(data_dict, self.kb_db_path)
        logger.info(
            f"[数据生成][Phase5] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s"
        )

        logger.info(
            f"[数据生成] 主流程完成: ok={result.ok}, "
            f"候选={len(result.screening.get('final') or [])}, snapshot={result.snapshot_paths.get('json', '')}"
        )
        return result

    def build_snapshot_data(self, result: ETLDailyResult) -> Dict[str, Any]:
        screening = result.screening or {}
        gold = result.gold_summary or {}
        market = gold.get("market") or {}
        market_score = _f(market.get("market_score"))
        emotion = self._build_market_emotion(market_score, market)
        plans_df = self._build_trade_plans(screening, market_score)

        return {
            "date": result.trade_date,
            "engine": "etl",
            "emotion_result": emotion,
            "market_env": {
                "engine": "etl_gold",
                "market_score": market_score,
                "trend_score": _f(market.get("trend_score")),
                "volume_score": _f(market.get("volume_score")),
                "width_score": _f(market.get("width_score")),
                "emotion_score": _f(market.get("emotion_score")),
                "warnings": result.warnings,
            },
            "trade_plans_df": plans_df,
            "etl_screening": screening,
            "etl_gold_summary": gold,
            "enabled_factors": self._screening_factor_ids(screening),
            "factor_profile": screening.get("profile") or "",
            "factor_results_path": "",
            "hot_concepts_df": pd.DataFrame(gold.get("top_sectors") or []),
            "mainline_df": pd.DataFrame(gold.get("top_sectors") or []),
            "patterns": {},
            "risk_gate_result": None,
        }

    @staticmethod
    def _build_market_emotion(market_score: float, market: Dict[str, Any]) -> Dict[str, Any]:
        if market_score >= 70:
            cycle, position, strategy = "上升期", "积极", "优先执行高分候选"
        elif market_score >= 50:
            cycle, position, strategy = "震荡期", "中性", "精选候选，等待实时确认"
        elif market_score >= 35:
            cycle, position, strategy = "防守期", "轻仓", "只观察最高分候选"
        else:
            cycle, position, strategy = "系统性风险", "空仓/观察", "暂停新开仓"
        return {
            "cycle_name": cycle,
            "scores": {"etl_market_score": market_score},
            "metrics": {
                "up_ratio": market.get("up_ratio"),
                "down_ratio": market.get("down_ratio"),
                "avg_pct_chg": market.get("avg_pct_chg"),
                "amount_ratio_5d": market.get("amount_ratio_5d"),
                "limit_up_count": market.get("limit_up_count"),
                "limit_down_count": market.get("limit_down_count"),
            },
            "strategy": {
                "position": position,
                "strategy": strategy,
                "forbidden_actions": "低开候选直接放弃；实时行情取消时不买入",
            },
        }

    @staticmethod
    def _build_trade_plans(screening: Dict[str, Any], market_score: float) -> pd.DataFrame:
        from risk.risk_config import RiskConfig

        risk = RiskConfig.load()
        trailing_text = (
            f"盈利达{risk.trailing_activation:.0%}后，"
            f"从持仓高点回撤{risk.trailing_stop:.0%}退出；持续上涨继续持有"
        )
        rows = []
        position = _position_label(market_score)
        for item in screening.get("final") or []:
            code = str(item.get("code") or "")
            name = str(item.get("name") or "")
            score = _f(item.get("score"))
            reasons = item.get("reasons") or []
            rows.append({
                "股票代码": code,
                "股票名称": name,
                "模式类型": f"指标筛选/{screening.get('profile') or 'default'}",
                "优先级": item.get("rank"),
                "综合评分": round(score, 2),
                "建议仓位": position,
                "入场区间": "竞价高开且实时确认后",
                "止损": "实时取消线或-3%",
                "止盈": trailing_text,
                "竞价条件": "高开才买入，低开直接放弃",
                "次日预期": "按指标评分排序，盘中只做确认/取消",
                "风险提示": "实时行情为取消/观察时不主动买入",
                "筛选理由": "；".join(str(x) for x in reasons[:4]),
            })
        return pd.DataFrame(rows)

    def _snapshot_plan_codes(self, trade_date: str) -> List[str]:
        if not trade_date:
            return []
        path = self.snapshot_dir / f"{trade_date}.json"
        if not path.exists():
            return []
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[数据生成][候选行情缓存] 上一交易日快照读取失败 {path}: {exc}")
            return []
        rows = ((snapshot.get("trade_plans") or {}).get("rows") or [])
        return [
            str(row.get("股票代码") or row.get("代码") or row.get("code") or "")
            for row in rows
            if isinstance(row, dict)
        ]

    @staticmethod
    def _screening_factor_ids(screening: Dict[str, Any]) -> List[str]:
        seen = set()
        factors: List[str] = []
        for item in screening.get("final") or []:
            for factor in (item.get("metrics") or {}).keys():
                if factor not in seen:
                    seen.add(factor)
                    factors.append(factor)
        return factors

    @staticmethod
    def _write_analysis_json(summary: Dict[str, Any], output_dir: Path, trade_date: str) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"analysis_{trade_date}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    @staticmethod
    def _safe_df(fn, label: str) -> pd.DataFrame:
        try:
            df = fn()
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[数据生成] {label} 读取失败，继续预取其他数据: {e}")
            return pd.DataFrame()

    @staticmethod
    def _ingest_kb(data_dict: Dict[str, Any], kb_db_path: Path) -> None:
        try:
            from kb.embeddings import get_embedder
            from kb.ingest import ingest_snapshot
            from kb.store import KBStore
            from snapshot.writer import build_snapshot

            ingest_snapshot(build_snapshot(data_dict), KBStore(kb_db_path), get_embedder())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[数据生成] 知识库灌库失败（不影响主流程）: {e}")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return default


def _position_label(market_score: float) -> str:
    if market_score >= 70:
        return "积极 30%-50%"
    if market_score >= 50:
        return "中性 20%-30%"
    if market_score >= 35:
        return "轻仓 10%-20%"
    return "观察 0%"
