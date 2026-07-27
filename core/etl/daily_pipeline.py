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
    stage: str = "full"
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
        if self.stage == "fetch":
            return bool(self.silver_summary.get("ready") or self.silver_summary.get("quality_ok"))
        if self.stage == "factors":
            return bool(self.factor_results and all(item.get("ok") for item in self.factor_results))
        if self.stage == "screening":
            return bool(
                self.screening.get("ok")
                and self.gold_summary.get("ok")
                and self.snapshot_paths
            )
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
    ):
        from config.settings import APP_DB_PATH, FACTOR_DB_PATH, SNAPSHOT_DIR, WEB_DATA_DIR

        self.dm = data_manager
        self.data_prep = DataPrep(data_manager)
        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
        self.web_data_dir = Path(web_data_dir or WEB_DATA_DIR)
        self.snapshot_dir = Path(snapshot_dir or SNAPSHOT_DIR)
        self.app_db_path = Path(app_db_path or APP_DB_PATH)

    def run(self, trade_date: str, prev_trade_date: str = "", *, profile: str = "default") -> ETLDailyResult:
        """Run all three stages in order for automation and CLI compatibility."""
        fetched = self.fetch_data(trade_date, prev_trade_date, skip_existing=True)
        factors = self.compute_factors(trade_date, prev_trade_date, profile=profile)
        selected = self.run_screening(trade_date, prev_trade_date, profile=profile)
        # 仅旧的完整流水线保留候选行情兼容缓存。三个独立阶段均不调用此 DataManager 方法。
        decision_rows = (
            (selected.screening.get("decision_pool") or {}).get("rows")
            or selected.screening.get("final")
            or []
        )
        current_codes = list(dict.fromkeys(
            str(item.get("code") or item.get("代码") or item.get("股票代码") or "")
            for item in decision_rows
            if item.get("code") or item.get("代码") or item.get("股票代码")
        ))
        previous_codes = self._snapshot_plan_codes(prev_trade_date)
        if hasattr(self.dm, "warm_trade_plan_daily_cache"):
            selected.plan_cache_summary = self.dm.warm_trade_plan_daily_cache(
                str(trade_date), current_codes + previous_codes
            )
        selected.stage = "full"
        selected.silver_summary = fetched.silver_summary
        selected.factor_results = factors.factor_results
        selected.warnings = fetched.warnings + factors.warnings + selected.warnings
        return selected

    def fetch_data(
        self,
        trade_date: str,
        prev_trade_date: str = "",
        *,
        skip_existing: bool = True,
    ) -> ETLDailyResult:
        """Fetch all post-close sources and persist the normalized Silver layer."""
        from core.etl.stage_status import fetch_status, write_fetch_manifest

        trade_date = str(trade_date)
        prev_trade_date = str(prev_trade_date or "")
        result = ETLDailyResult(
            trade_date=trade_date, prev_trade_date=prev_trade_date, stage="fetch"
        )
        existing = fetch_status(
            trade_date, db_path=self.duckdb_path, web_data_dir=self.web_data_dir
        )
        if skip_existing and existing.get("complete"):
            existing["skipped"] = True
            result.silver_summary = existing
            logger.info(f"[盘后取数] {trade_date} 本地数据完整，跳过远端接口")
            return result

        logger.info(f"[盘后取数] 开始: {trade_date}, prev={prev_trade_date or '-'}")
        phase_started = time.monotonic()
        zt_pool = self._safe_df(lambda: self.dm.get_limit_up_pool(trade_date), "今日涨停池")
        prev_zt_pool = (
            self._safe_df(lambda: self.dm.get_limit_up_pool(prev_trade_date), "昨日涨停池")
            if prev_trade_date else pd.DataFrame()
        )
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
        persisted = dict(dataset.meta.get("silver_persist") or {})
        write_fetch_manifest(
            trade_date,
            web_data_dir=self.web_data_dir,
            sources=dataset.meta.get("source_fetch_status") or {},
            writes=persisted.get("writes") or {},
        )
        status = fetch_status(
            trade_date, db_path=self.duckdb_path, web_data_dir=self.web_data_dir
        )
        status.update({"persist": persisted, "skipped": False})
        result.silver_summary = status
        if not status.get("ready"):
            result.warnings.append(status.get("message") or "盘后数据不完整")
        if status.get("source_missing"):
            result.warnings.append(
                f"部分增强接口未完成，将在下次取数时重试: {status.get('source_missing')}"
            )
        if status.get("write_missing"):
            result.warnings.append(
                f"Silver 表未写入 DuckDB: {status.get('write_missing')}"
            )
        logger.info(
            f"[盘后取数] 完成: {trade_date}, ready={status.get('ready')}, "
            f"耗时={time.monotonic() - phase_started:.1f}s"
        )
        return result

    def compute_factors(
        self,
        trade_date: str,
        prev_trade_date: str = "",
        *,
        profile: str = "default",
    ) -> ETLDailyResult:
        """Compute factors from local Silver data without calling market APIs."""
        from core.etl.stage_status import fetch_status, require_stage

        trade_date = str(trade_date)
        prev_trade_date = str(prev_trade_date or "")
        result = ETLDailyResult(
            trade_date=trade_date, prev_trade_date=prev_trade_date, stage="factors"
        )
        status = fetch_status(
            trade_date, db_path=self.duckdb_path, web_data_dir=self.web_data_dir
        )
        require_stage(status)
        result.silver_summary = status
        logger.info(f"[因子计算] 开始: {trade_date}")
        phase_started = time.monotonic()
        factor_results = FactorJobRunner(self.duckdb_path).run(trade_date)
        result.factor_results = [item.to_dict() for item in factor_results]
        failed = [item for item in result.factor_results if not item.get("ok")]
        if failed:
            result.warnings.append(f"因子任务失败: {[item.get('name') for item in failed]}")
        logger.info(
            "[因子计算] 模型训练已解耦，本阶段只生成因子；策略模型请在策略训练任务中单独运行"
        )
        logger.info(
            f"[因子计算] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s, "
            f"失败={len(failed)}"
        )
        return result

    def run_screening(
        self,
        trade_date: str,
        prev_trade_date: str = "",
        *,
        profile: str = "default",
        strategy_ids: Optional[List[str]] = None,
        primary_strategy: str = "",
    ) -> ETLDailyResult:
        """Run screening and snapshots from local factor tables only."""
        from snapshot import SnapshotWriter
        from core.etl.stage_status import factor_status, require_stage

        trade_date = str(trade_date)
        prev_trade_date = str(prev_trade_date or "")
        result = ETLDailyResult(
            trade_date=trade_date, prev_trade_date=prev_trade_date, stage="screening"
        )
        require_stage(factor_status(trade_date, db_path=self.duckdb_path))
        from core.screening.strategy_profiles import StrategyProfileRepository

        strategy_repository = StrategyProfileRepository()
        if strategy_ids is None:
            enabled_profiles = strategy_repository.list_profiles(
                enabled_only=True, scope="production",
            )
            primary_profile = next((item for item in enabled_profiles if item.get("primary")), None)
            strategy_ids = [str(item.get("id")) for item in enabled_profiles if item.get("id")]
            if not strategy_ids:
                strategy_ids = [str((primary_profile or {}).get("id") or profile)]
        strategy_ids = strategy_repository.validate_selection(
            strategy_ids, scope="production",
        )
        if primary_strategy not in strategy_ids:
            primary_strategy = next(
                (item["id"] for item in strategy_repository.list_profiles(
                    enabled_only=True, scope="production",
                )
                 if item.get("primary") and item["id"] in strategy_ids),
                strategy_ids[0],
            )
        logger.info(
            f"[选股策略] 开始: {trade_date}, combinations={strategy_ids}, "
            f"primary={primary_strategy}"
        )

        phase_started = time.monotonic()
        logger.info(f"[选股策略][筛选] 开始: {trade_date}")
        strategy_results: Dict[str, Dict[str, Any]] = {}
        screening = None
        for strategy_id in strategy_ids:
            strategy_profile = strategy_repository.get_profile(strategy_id) or {}
            strategy_config = strategy_repository.resolve(strategy_id)
            output_dir = self.web_data_dir / "screening" / "combinations" / strategy_id
            current = ScreeningEngine(
                duckdb_path=self.duckdb_path,
                output_dir=output_dir,
            ).run(
                trade_date,
                profile=strategy_id,
                profile_config=strategy_config,
                persist=True,
            )
            payload = current.to_dict()
            payload["strategy_id"] = strategy_id
            payload["strategy_name"] = str(strategy_profile.get("name") or strategy_id)
            payload["strategy_version"] = str(strategy_profile.get("version") or "")
            payload["strategy_execution"] = dict(strategy_profile.get("execution") or {})
            payload["position_cap_pct"] = float(strategy_profile.get("position_cap_pct") or 0.0)
            for bucket_name in ("final", "candidate_pool"):
                for candidate in payload.get(bucket_name) or []:
                    if not isinstance(candidate, dict):
                        continue
                    candidate["strategy_id"] = strategy_id
                    candidate["strategy_name"] = payload["strategy_name"]
                    candidate["strategy_version"] = payload["strategy_version"]
                    candidate["strategy_execution"] = dict(payload["strategy_execution"])
                    candidate["position_cap_pct"] = payload["position_cap_pct"]
            if current.output_path:
                Path(current.output_path).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            strategy_results[strategy_id] = payload
            logger.info(
                f"[选股策略][组合] {strategy_id}: ok={current.ok}, "
                f"候选={len(payload.get('final') or [])}, {current.message}"
            )
            if strategy_id == primary_strategy:
                screening = current
        failed_strategies = [
            f"{strategy_id}: {payload.get('message') or 'unknown error'}"
            for strategy_id, payload in strategy_results.items()
            if not payload.get("ok")
        ]
        if failed_strategies:
            raise RuntimeError(
                "Strategy combination screening was incomplete: "
                + "; ".join(failed_strategies)
            )
        if screening is None:
            raise RuntimeError("未取得主发布策略结果")
        result.screening = dict(strategy_results[primary_strategy])
        result.screening["strategy_runs"] = [
            {
                "id": strategy_id,
                "name": payload.get("strategy_name") or strategy_id,
                "profile": payload.get("profile"),
                "ok": payload.get("ok"),
                "message": payload.get("message"),
                "input_count": payload.get("input_count"),
                "final_count": len(payload.get("final") or []),
                "output_path": payload.get("output_path"),
                "primary": strategy_id == primary_strategy,
                "weight_source": (payload.get("weight_metadata") or {}).get("requested_weight_source"),
                "model_type": (payload.get("weight_metadata") or {}).get("model_type"),
                "fallback_reason": (payload.get("weight_metadata") or {}).get("fallback_reason"),
            }
            for strategy_id, payload in strategy_results.items()
        ]
        canonical_path = self.web_data_dir / "screening" / f"screening_{trade_date}.json"
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_text(
            json.dumps(result.screening, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        comparison_path = (
            self.web_data_dir / "screening" / "combinations" / f"strategy_runs_{trade_date}.json"
        )
        comparison_path.parent.mkdir(parents=True, exist_ok=True)
        comparison_path.write_text(
            json.dumps(
                {"trade_date": trade_date, "primary": primary_strategy, "results": strategy_results},
                ensure_ascii=False, indent=2, default=str,
            ),
            encoding="utf-8",
        )
        result.screening["comparison_path"] = str(comparison_path)
        try:
            from core.portfolio.decision_pool_service import DecisionPoolService

            profiles_by_id = {
                strategy_id: strategy_repository.get_profile(strategy_id) or {}
                for strategy_id in strategy_results
            }
            market_state = dict(
                (result.screening.get("weight_metadata") or {}).get("market_state_snapshot") or {}
            )
            decision_pool = DecisionPoolService().build(
                strategy_results,
                profiles_by_id,
                market_score=float(market_state.get("score") or 50.0),
                market_regime=str(market_state.get("regime") or ""),
                market_state=market_state,
            )
            decision_path = DecisionPoolService.persist(
                decision_pool,
                self.web_data_dir / "screening",
                trade_date,
            )
            result.screening["decision_pool"] = decision_pool
            result.screening["decision_pool_path"] = str(decision_path)
            production_final = [
                dict(row)
                for row in decision_pool.get("rows") or []
                if row.get("execution_eligible")
            ]
            result.screening["final"] = production_final
            result.screening["final_count"] = len(production_final)
            # Re-write the canonical artifact after attaching the production plan.
            canonical_path.write_text(
                json.dumps(result.screening, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            logger.info(
                f"[选股策略][生产决策池] {trade_date}: "
                f"可执行={decision_pool.get('decision_count', 0)}, path={decision_path}"
            )
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"生产决策池生成失败: {exc}")
            logger.warning(f"[选股策略][生产决策池] 失败: {exc}")
        result.screening["production_engine"] = "rules_only"
        if not screening.ok:
            result.warnings.append(f"筛选失败: {screening.message}")
        logger.info(
            f"[选股策略][筛选] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s, "
            f"候选={len(result.screening.get('final') or [])}"
        )

        # 龙头身份按生命周期留痕，历史结果只在未来数据成熟后回填，用于样本外概率校准。
        try:
            from core.realtime.leader_pool_service import LeaderPoolService
            from core.signals.leader_outcome import LeaderOutcomeTracker

            leader_service = LeaderPoolService(
                screening_dir=self.web_data_dir / "screening", duckdb_path=self.duckdb_path,
            )
            tracker = LeaderOutcomeTracker(self.duckdb_path)
            leader_rows = leader_service.build_pool(trade_date, lookback=20, limit=60).get("rows") or []
            recorded = tracker.record(trade_date, leader_rows)
            matured = tracker.refresh_outcomes(trade_date)
            logger.info(f"[选股策略][龙头生命周期] 记录={recorded}, 成熟样本回填={matured}")
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"龙头生命周期留痕失败: {exc}")
            logger.warning(f"[选股策略][龙头生命周期] 失败: {exc}")

        phase_started = time.monotonic()
        logger.info(f"[选股策略][分析摘要] 开始: {trade_date}")
        result.gold_summary = build_gold_analysis_summary(
            trade_date,
            duckdb_path=self.duckdb_path,
            screening_dir=self.web_data_dir / "screening",
        )
        result.analysis_path = str(self._write_analysis_json(result.gold_summary, self.web_data_dir / "screening", trade_date))
        logger.info(
            f"[选股策略][分析摘要] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s"
        )

        phase_started = time.monotonic()
        logger.info(f"[选股策略][页面快照] 开始: {trade_date}")
        data_dict = self.build_snapshot_data(result)
        result.snapshot_paths = SnapshotWriter(self.snapshot_dir, self.app_db_path, self.duckdb_path).write(data_dict)
        logger.info(
            f"[选股策略][页面快照] 完成: {trade_date}, 耗时={time.monotonic() - phase_started:.1f}s"
        )

        logger.info(
            f"[选股策略] 完成: ok={result.ok}, "
            f"候选={len(result.screening.get('final') or [])}, snapshot={result.snapshot_paths.get('json', '')}"
        )
        return result

    def _refresh_factor_models(
        self,
        result: ETLDailyResult,
        trade_date: str,
        prev_trade_date: str,
        profile: str,
    ) -> None:
        """Legacy compatibility hook; model training is an explicit strategy task."""
        logger.info(
            "[因子计算] 忽略旧的自动训练入口: "
            f"date={trade_date}, prev={prev_trade_date or '-'}, profile={profile or 'default'}"
        )

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
                "forbidden_actions": "10:00前未确认、跌破开盘结构或无可成交分钟时不买入",
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
        market_regime = str((screening.get("weight_metadata") or {}).get("market_regime") or "")
        default_position = _position_label(market_score)
        for item in screening.get("final") or []:
            code = str(item.get("code") or "")
            name = str(item.get("name") or "")
            score = _f(item.get("score"))
            reasons = item.get("reasons") or []
            position_cap = _f(item.get("position_budget_pct"))
            market_total_position_cap = _f(
                item.get("market_total_position_cap_pct"), 100.0
            )
            position = (
                f"试仓 0%-{position_cap:g}%"
                if market_regime == "weak" and position_cap > 0
                else default_position
            )
            rows.append({
                "股票代码": code,
                "股票名称": name,
                "模式类型": str(
                    screening.get("strategy_name")
                    or screening.get("profile")
                    or "默认短线综合"
                ),
                "优先级": item.get("rank"),
                "综合评分": round(score, 2),
                "3日强势成功率%": item.get("candidate_probability"),
                "同市场基准%": item.get("baseline_probability"),
                "相对基准": item.get("probability_lift"),
                "3日预期超额收益%": item.get("expected_return_pct"),
                "止损概率%": item.get("stop_probability"),
                "成功率80%区间": f"{item.get('probability_ci_low', '--')}% - {item.get('probability_ci_high', '--')}%",
                "预期超额80%区间": f"{item.get('return_interval_low_pct', '--')}% - {item.get('return_interval_high_pct', '--')}%",
                "Brier误差": item.get("brier_score"),
                "校准误差ECE%": item.get("ece"),
                "模型漂移": item.get("model_drift_status"),
                "系统结论": item.get("decision_label"),
                "类似样本": item.get("similar_sample_size"),
                "可信等级": item.get("confidence_grade") or "D",
                "建议仓位": position,
                "策略单票仓位上限%": position_cap,
                "市场总仓位上限%": market_total_position_cap,
                "入场区间": "弱转强/强势延续/高开加速按分钟确认",
                "止损": "实时取消线或-3%",
                "止盈": trailing_text,
                "竞价条件": "开盘仅用于信号分层，10:00前按一分钟行情确认",
                "次日预期": "弱转强优先，强势延续和龙头高开加速作为补充",
                "风险提示": "未确认或信号后无可成交分钟时不主动买入",
                "共振板块": item.get("resonance_sectors") or "",
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
