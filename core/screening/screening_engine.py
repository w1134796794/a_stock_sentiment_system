"""Phase 3 screening engine over Phase 2 gold factor tables."""
from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import yaml
from loguru import logger

from core.screening.enhancements import ENHANCEMENT_DEFINITIONS
from core.screening.explanations import build_screening_reasons
from core.screening.screening_models import FilterTrace, ScreeningResult
from core.signals.confidence_service import ConfidenceService
from core.utils.price_limit import get_price_limit_pct_points, limit_progress


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, Path):
        return str(value)
    if pd.isna(value):
        return None
    return str(value)


def _to_float(value: Any, default: float = 50.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return default


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".")[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


class ScreeningEngine:
    """Apply hard filters, priority filters and ranking from YAML profiles."""

    RAW_VALUE_FACTORS = {
        "stk_lhb_crowding_risk",
        "mkt_limit_up_count",
        "mkt_limit_down_count",
        "mkt_broken_rate",
        "F1_cycle_duration",
        "F2_market_emotion_divergence",
        "prev_limit_up_premium",
        "prev_limit_up_positive",
        "prev_first_board_gap_up",
        "first_board_sector_resonance_ratio",
        "first_board_cluster_count",
        "first_board_follow_through_ratio",
    }

    def __init__(
        self,
        *,
        duckdb_path: Optional[Path] = None,
        profile_path: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        weight_repository: Any = None,
    ):
        from config.settings import BASE_DIR, FACTOR_DB_PATH, WEB_DATA_DIR

        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
        self.profile_path = Path(profile_path or BASE_DIR / "config" / "screening_profiles.yaml")
        self.output_dir = Path(output_dir or WEB_DATA_DIR / "screening")
        if weight_repository is None:
            from core.factors.factor_library import DynamicWeightRepository

            weight_repository = DynamicWeightRepository()
        self.weight_repository = weight_repository
        self._confidence_profile: Dict[str, Any] = {}
        self._weight_metadata: Dict[str, Any] = {}
        self._confidence_model_type = "manual_prior"
        self._confidence_as_of_date = ""
        self._active_regime = "neutral"
        self._market_state_snapshot = None
        self._model_drift: Dict[str, Any] = {"status": "unknown"}
        self._confidence_drift: Dict[str, Any] = {"status": "unknown"}
        self._candidate_model_metadata: Dict[str, Any] = {}
        try:
            from risk.risk_config import RiskConfig
            self._risk_config = RiskConfig.load()
        except Exception:
            self._risk_config = None

    @staticmethod
    def _select_drift_references(
        weight_metadata: Dict[str, Any], market_state: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        by_regime = weight_metadata.get("feature_reference_by_regime") or {}
        selected = by_regime.get(str(market_state)) or {}
        if selected:
            regime_meta = (
                weight_metadata.get("feature_reference_regime_meta") or {}
            ).get(str(market_state)) or {}
            return selected, {
                "reference_scope": "market_regime",
                "reference_regime": str(market_state),
                "reference_sample_size": int(regime_meta.get("sample_size") or 0),
                "reference_trade_days": int(regime_meta.get("trade_days") or 0),
            }
        return weight_metadata.get("feature_reference") or {}, {
            "reference_scope": "global_fallback",
            "reference_regime": str(market_state),
            "reference_sample_size": 0,
            "reference_trade_days": 0,
        }

    def run(
        self,
        trade_date: str,
        *,
        profile: str = "default",
        profile_config: Optional[Dict[str, Any]] = None,
        candidate_codes: Optional[Iterable[str]] = None,
        candidate_frame: Optional[pd.DataFrame] = None,
        persist: bool = True,
    ) -> ScreeningResult:
        trade_date = str(trade_date)
        profiles = self.load_profiles()
        profile_name = str(profile or "default")
        if profile_config is None and profile_name not in profiles:
            return ScreeningResult(
                trade_date=trade_date,
                profile=profile,
                ok=False,
                message=f"screening profile 不存在: {profile}",
            )
        source_config = deepcopy(profile_config) if profile_config is not None else (profiles[profile_name] or {})
        cfg, weight_metadata = self._runtime_profile(source_config, trade_date, profile_name)
        cfg, source_gate = self._disable_missing_source_enhancements(cfg, source_config, trade_date)
        weight_metadata["source_gate"] = source_gate

        result = ScreeningResult(trade_date=trade_date, profile=profile_name)
        result.weight_metadata = weight_metadata
        try:
            candidates = (self.load_candidates(trade_date, candidate_codes=candidate_codes)
                          if candidate_frame is None else candidate_frame.copy(deep=True))
            if candidate_codes is not None:
                candidates = candidates[candidates["code"].map(_normalize_code).isin({_normalize_code(c) for c in candidate_codes})]
            if str(cfg.get("strategy_id") or profile_name) == "weak_to_strong":
                from config.settings import WEAK_TO_STRONG_LEADER_LOOKBACK_DAYS
                from core.realtime.leader_pool_service import LeaderPoolService

                root = self.output_dir.parent.parent if self.output_dir.parent.name == "combinations" else self.output_dir
                eligible = LeaderPoolService(screening_dir=root, duckdb_path=self.duckdb_path).historical_leader_codes(
                    trade_date, lookback=WEAK_TO_STRONG_LEADER_LOOKBACK_DAYS, include_trade_date=False,
                )
                candidates = candidates[candidates["code"].map(_normalize_code).isin(eligible)]
                result.weight_metadata["historical_leader_pool"] = {"lookback": WEAK_TO_STRONG_LEADER_LOOKBACK_DAYS,
                                                                   "eligible": len(eligible), "as_of": trade_date}
        except Exception as e:  # noqa: BLE001
            result.ok = False
            result.message = f"读取指标数据失败: {e}"
            return result
        result.input_count = int(len(candidates))
        if candidates.empty:
            result.after_hard_filter = 0
            result.after_priority_filter = 0
            result.final = []
            result.rejected = []
            result.message = ("历史龙头前置池无匹配候选，未扩大股票池"
                              if "historical_leader_pool" in result.weight_metadata
                              else "未读取到个股指标数据，输出空筛选结果")
            if persist:
                result.output_path = str(self.persist_result(result))
            return result

        market_values = pd.to_numeric(candidates.get("mkt_market_score"), errors="coerce") if "mkt_market_score" in candidates else pd.Series(dtype=float)
        if market_values.empty or market_values.dropna().empty:
            market_values = pd.to_numeric(candidates.get("market_score"), errors="coerce") if "market_score" in candidates else pd.Series([50.0])
        market_score_value = float(market_values.dropna().median()) if not market_values.dropna().empty else 50.0
        from core.models.market_state import MarketStateSnapshot

        regime_model = weight_metadata.get("market_regime_model") or {}
        market_context: Dict[str, Any] = {}
        market_row = candidates.iloc[0] if not candidates.empty else {}
        for key in (
            "market_score_change",
            "cycle_duration",
            "market_emotion_divergence",
            "limit_up_count",
            "limit_down_count",
            "echelon_integrity",
            "prev_limit_up_premium",
            "prev_limit_up_positive",
            "prev_first_board_gap_up",
            "broken_rate",
        ):
            value = market_row.get(key) if hasattr(market_row, "get") else None
            if value is not None and not pd.isna(value):
                market_context[key] = value
        market_state = MarketStateSnapshot.resolve(
            market_score_value,
            trade_date=trade_date,
            regime_model=regime_model,
            context=market_context,
        )
        self._market_state_snapshot = market_state
        self._active_regime = market_state.regime
        weight_metadata["market_state_snapshot"] = market_state.to_dict()
        rules_only = str(cfg.get("strategy_scope") or "") == "production"
        if rules_only:
            self._model_drift = {
                "status": "not_used",
                "reason": "生产链已冻结模型，仅运行规则策略",
            }
            self._confidence_drift = dict(self._model_drift)
            weight_metadata["feature_drift"] = self._model_drift
            weight_metadata["candidate_model"] = {}
            weight_metadata["candidate_model_runtime"] = "rules_only"
            weight_metadata["production_engine"] = "rules_only"
            regime_weights = None
        else:
            from core.signals.trust_algorithms import evaluate_feature_drift

            drift_references, drift_reference_meta = self._select_drift_references(
                weight_metadata, self._active_regime,
            )
            self._model_drift = evaluate_feature_drift(
                candidates, drift_references,
            )
            self._model_drift.update(drift_reference_meta)
            weight_metadata["feature_drift"] = self._model_drift
            self._confidence_drift = dict(self._model_drift)
            regime_weights = (weight_metadata.get("regime_weights") or {}).get(self._active_regime)
        if regime_weights:
            cfg.setdefault("ranking", {})["weights"] = dict(regime_weights)
            weight_metadata["weights"] = dict(regime_weights)
            weight_metadata["source"] = "dynamic_regime_ic_ir"
        # Regime-specific weights are loaded after the initial source gate.
        # Apply the gate again so an unavailable optional feed cannot be
        # reintroduced by a model artifact.
        cfg, source_gate = self._disable_missing_source_enhancements(cfg, source_config, trade_date)
        weight_metadata["source_gate"] = source_gate
        weight_metadata["weights"] = dict((cfg.get("ranking") or {}).get("weights") or {})
        self._weight_metadata = dict(weight_metadata)
        profiles = weight_metadata.get("confidence_profiles") or {}
        self._confidence_profile = dict(profiles.get(self._active_regime) or profiles.get("all") or {})
        self._confidence_model_type = str(weight_metadata.get("model_type") or "manual_prior")
        self._confidence_as_of_date = str(weight_metadata.get("effective_date") or "")
        weight_metadata["market_regime"] = self._active_regime
        weight_metadata["confidence_profile_available"] = bool(self._confidence_profile)

        allowed_regimes = list(cfg.get("allowed_market_regimes") or [])
        regime_applicable = not allowed_regimes or self._active_regime in allowed_regimes
        weight_metadata["strategy_regime_applicable"] = regime_applicable
        if not rules_only and not regime_applicable:
            result.after_hard_filter = 0
            result.after_priority_filter = 0
            result.message = (
                f"当前市场状态 {self._active_regime} 不在策略适用范围 "
                f"{','.join(allowed_regimes)}，本次不输出候选"
            )
            result.weight_metadata = weight_metadata
            if persist:
                result.output_path = str(self.persist_result(result))
            return result

        allowed_phases = list(cfg.get("allowed_emotion_phases") or [])
        phase_applicable = not allowed_phases or market_state.phase in allowed_phases
        weight_metadata["strategy_phase_applicable"] = phase_applicable
        if not rules_only and not phase_applicable:
            result.after_hard_filter = 0
            result.after_priority_filter = 0
            result.message = (
                f"当前{market_state.phase_label}不在策略适用阶段"
                f"{','.join(allowed_phases)}，本次不输出候选"
            )
            result.weight_metadata = weight_metadata
            if persist:
                result.output_path = str(self.persist_result(result))
            return result

        strategy_id = str(cfg.get("strategy_id") or profile_name)
        managed_strategies = {
            "mainline_leader", "first_board_launch", "weak_to_strong",
        }
        strategy_position_multiplier = (
            market_state.strategy_position_multiplier("" if rules_only else strategy_id)
            if strategy_id in managed_strategies else 1.0
        )
        weight_metadata["emotion_phase"] = market_state.phase
        weight_metadata["emotion_phase_label"] = market_state.phase_label
        weight_metadata["market_risk_flags"] = list(market_state.risk_flags)
        weight_metadata["strategy_position_multiplier"] = strategy_position_multiplier
        self._weight_metadata = dict(weight_metadata)
        if (
            not rules_only
            and strategy_id in managed_strategies
            and strategy_position_multiplier <= 0
        ):
            result.after_hard_filter = 0
            result.after_priority_filter = 0
            result.message = (
                f"{market_state.phase_label}触发策略风险否决："
                f"{','.join(market_state.risk_flags) or '阶段不适用'}"
            )
            result.weight_metadata = weight_metadata
            if persist:
                result.output_path = str(self.persist_result(result))
            return result
        base_position_cap = _to_float(cfg.get("position_cap_pct"), 0.0)
        if base_position_cap > 0:
            cfg["position_cap_pct"] = round(
                base_position_cap * strategy_position_multiplier, 4,
            )
        if (
            "echelon_broken" in market_state.risk_flags
            and strategy_id == "mainline_leader"
        ):
            cfg.setdefault("exclusion_filters", []).append({
                "name": "梯队断层回避高位",
                "factor": "stk_board_height",
                "op": ">=",
                "value": 3,
                "reason": "连板梯队断层，仅有高位没有低位承接",
            })
        if "market_emotion_divergence" in market_state.risk_flags:
            ranking = cfg.setdefault("ranking", {})
            ranking["top_n"] = max(1, int(ranking.get("top_n") or 1) // 2)

        neutral_score = _to_float((cfg.get("missing") or {}).get("neutral_score"), 50.0)
        working = candidates.copy()
        working["_screening_score"] = self._ranking_score(working, cfg, neutral_score)
        self._candidate_model_metadata = dict(weight_metadata.get("candidate_model") or {})
        if self._candidate_model_metadata.get("active") and self._model_drift.get("status") == "degraded":
            self._candidate_model_metadata["active"] = False
            self._confidence_model_type = "regime_ic_ir_fallback"
            self._confidence_drift = {
                **self._model_drift,
                "status": "fallback_active",
                "original_status": "degraded",
            }
            weight_metadata["candidate_model_runtime"] = "fallback_drift"
            weight_metadata["fallback_reason"] = "特征漂移超限，已自动切换同市场IC/IR规则筛选"
        if self._candidate_model_metadata.get("active") and weight_metadata.get("artifact_path"):
            try:
                from core.models.candidate_model import CandidateModelRuntime

                runtime = CandidateModelRuntime(
                    self._candidate_model_metadata,
                    base_dir=Path(str(weight_metadata["artifact_path"])).parent,
                )
                model_result = runtime.score(working)
                if model_result.get("available"):
                    working["_lgb_rank_score"] = model_result["rank_score"]
                    working["_meta_probability"] = model_result["probability"]
                    working["_model_expected_return"] = model_result.get("expected_return")
                    working["_model_return_low"] = model_result.get("return_interval_low")
                    working["_model_return_high"] = model_result.get("return_interval_high")
                    working["_model_expected_gross_return"] = model_result.get("expected_gross_return")
                    working["_model_gross_return_low"] = model_result.get("gross_return_interval_low")
                    working["_model_gross_return_high"] = model_result.get("gross_return_interval_high")
                    working["_model_stop_probability"] = model_result.get("stop_probability")
                    working["_shap_explanation"] = model_result["shap"]
                    weight_metadata["candidate_model_runtime"] = "active"
                else:
                    weight_metadata["candidate_model_runtime"] = "fallback_unavailable"
            except Exception as exc:
                logger.warning(f"[ScreeningEngine] LightGBM运行失败，回退IC/IR: {exc}")
                weight_metadata["candidate_model_runtime"] = "fallback_error"
        requested_model = str(weight_metadata.get("requested_weight_source") or "auto")
        runtime_status = str(weight_metadata.get("candidate_model_runtime") or "")
        if (
            requested_model in {"auto", "lightgbm", "xgboost"}
            and (not self._candidate_model_metadata.get("active") or runtime_status.startswith("fallback"))
        ):
            weight_metadata.setdefault("candidate_model_runtime", "fallback_unavailable")
            self._confidence_model_type = "regime_ic_ir_fallback"
            self._confidence_drift = {
                **self._model_drift,
                "status": "fallback_active",
                "original_status": str(self._model_drift.get("status") or "unknown"),
            }
            weight_metadata.setdefault(
                "fallback_reason", "机器学习模型不可用，已使用同市场IC/IR规则筛选",
            )
        elif requested_model in {"manual", "ic_ir"}:
            # Feature drift is a health signal for the optional ML model. A
            # deliberately selected rule engine keeps its own calibrated grade.
            self._confidence_drift = {
                **self._model_drift,
                "status": "rules_active",
                "original_status": str(self._model_drift.get("status") or "unknown"),
            }
        reasons: Dict[str, List[str]] = {str(row.code): [] for row in working.itertuples()}
        rejected: List[Dict[str, Any]] = []

        working = self._apply_hard_filters(working, cfg, neutral_score, reasons, rejected, result)
        working = self._apply_exclusion_filters(working, cfg, neutral_score, rejected, result)
        result.after_hard_filter = int(len(working))

        working = self._apply_priority_filters(working, cfg, neutral_score, reasons, rejected, result)
        result.after_priority_filter = int(len(working))

        result.scenarios = self._rank_scenarios(working, cfg, neutral_score, reasons)
        final = result.scenarios.get("enhanced_all") or result.scenarios.get("lhb_sector") or self._rank(
            working, cfg, neutral_score, reasons,
        )
        result.final = final
        scenario_scores = self._scenario_scores(working, cfg, neutral_score)
        baseline = scenario_scores["lhb_sector"]
        result.candidate_pool = self._rank(
            working, cfg, neutral_score, reasons,
            score_series=baseline + self._enhancement_adjustment(working, cfg),
            base_series=scenario_scores["no_lhb"], model_baseline_series=baseline,
            top_n_override=100,
        )
        result.rejected = rejected[:200]
        result.message = f"筛选完成，输入 {result.input_count}，最终 {len(final)}"

        if persist:
            result.output_path = str(self.persist_result(result))
        return result

    @staticmethod
    def _disable_missing_source_enhancements(
        runtime: Dict[str, Any], profile: Dict[str, Any], trade_date: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Remove unavailable optional evidence instead of treating it as score 50."""
        from core.models.strategy_diagnostics import StrategyDiagnosticsService

        gate = StrategyDiagnosticsService.unavailable_factor_tokens(profile, trade_date)
        tokens = tuple(gate.get("tokens") or ())
        if not tokens:
            return runtime, gate

        def unavailable(factor: Any) -> bool:
            text = str(factor or "")
            return any(token in text for token in tokens)

        cfg = deepcopy(runtime)
        ranking = cfg.setdefault("ranking", {})
        original = dict(ranking.get("weights") or ranking.get("prior_weights") or {})
        kept = {factor: weight for factor, weight in original.items() if not unavailable(factor)}
        total = sum(abs(_to_float(value)) for value in kept.values())
        if total > 0:
            kept = {factor: _to_float(value) / total for factor, value in kept.items()}
            ranking["weights"] = kept
        disabled_filters = []
        for key in ("hard_filters", "priority_filters", "exclusion_filters"):
            rows = list(cfg.get(key) or [])
            disabled_filters.extend(str(row.get("factor") or "") for row in rows if unavailable(row.get("factor")))
            cfg[key] = [row for row in rows if not unavailable(row.get("factor"))]
        gate["disabled_factors"] = sorted(factor for factor in original if unavailable(factor))
        gate["disabled_filters"] = sorted(set(disabled_filters))
        gate["remaining_weight_count"] = len(kept)
        return cfg, gate

    def load_profiles(self) -> Dict[str, Dict[str, Any]]:
        if not self.profile_path.exists():
            logger.warning(f"[ScreeningEngine] profile 文件不存在: {self.profile_path}")
            return {}
        with self.profile_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("screening_profiles") or {}

    def _runtime_profile(
        self, cfg: Dict[str, Any], trade_date: str, profile: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Replace YAML priors with the latest weight version effective on trade_date."""
        runtime = deepcopy(cfg)
        ranking = runtime.setdefault("ranking", {})
        prior = ranking.get("prior_weights") or ranking.get("weights") or {"stk_total_score": 1.0}
        source = "yaml_prior"
        effective_date = ""
        artifact_path = ""
        model_type = "manual_prior"
        from config.settings import FACTOR_WEIGHT_MODE

        requested_source = str(runtime.get("strategy_weight_source") or "auto").lower()
        weight_profile = str(runtime.get("strategy_weight_profile") or profile)
        artifact = None
        if (
            requested_source != "manual"
            and FACTOR_WEIGHT_MODE not in {"prior", "static", "off", "disabled"}
        ):
            artifact = self.weight_repository.resolve(trade_date, weight_profile)
        confidence_artifact = artifact
        if (
            confidence_artifact is None
            and FACTOR_WEIGHT_MODE not in {"prior", "static", "off", "disabled"}
        ):
            confidence_artifact = self.weight_repository.resolve(trade_date, "default")
        if artifact is not None:
            ranking["weights"] = artifact.weights
            source = "dynamic_ic_ir"
            effective_date = artifact.effective_date
            artifact_path = str(artifact.path)
            model_type = str(artifact.payload.get("model_type") or "ic_ir")
        else:
            ranking["weights"] = prior
        confidence_payload = confidence_artifact.payload if confidence_artifact else {}
        if not effective_date and confidence_artifact is not None:
            effective_date = confidence_artifact.effective_date
        candidate_model = dict((artifact.payload.get("candidate_model") if artifact else {}) or {})
        fallback_reason = ""
        if requested_source in {"manual", "ic_ir"}:
            candidate_model = {}
            model_type = "manual_prior" if requested_source == "manual" else "ic_ir"
            source = "manual_prior" if requested_source == "manual" else source
        elif requested_source == "lightgbm":
            if not candidate_model.get("active"):
                fallback_reason = "LightGBM模型不可用，已降级为IC/IR动态权重"
                model_type = "ic_ir_fallback"
        elif requested_source == "xgboost":
            # XGBoost is an explicit challenger slot. Until a dated artifact is
            # published, keep the strategy executable with an audited fallback.
            candidate_model = {}
            fallback_reason = "XGBoost模型尚无已发布版本，已降级为IC/IR动态权重"
            model_type = "xgboost_unavailable_ic_ir_fallback"
            source = "xgboost_fallback_ic_ir"
        return runtime, {
            "source": source,
            "model_type": model_type,
            "requested_weight_source": requested_source,
            "weight_profile": weight_profile,
            "fallback_reason": fallback_reason,
            "effective_date": effective_date,
            "artifact_path": artifact_path,
            "weights": dict(ranking["weights"]),
            "regime_weights": dict((artifact.payload.get("regime_weights") if artifact else {}) or {}),
            "confidence_profiles": dict(confidence_payload.get("confidence_profiles") or {}),
            "confidence_profile_source": (
                weight_profile if artifact is not None else "default_shared_baseline"
                if confidence_artifact is not None else "unavailable"
            ),
            "feature_reference": dict(confidence_payload.get("feature_reference") or {}),
            "feature_reference_by_regime": dict(
                confidence_payload.get("feature_reference_by_regime") or {}
            ),
            "feature_reference_regime_meta": dict(
                confidence_payload.get("feature_reference_regime_meta") or {}
            ),
            "model_diagnostics": dict(confidence_payload.get("model_diagnostics") or {}),
            "candidate_model": candidate_model,
            "market_regime_model": dict(confidence_payload.get("market_regime_model") or {}),
        }

    def load_candidates(
        self,
        trade_date: str,
        *,
        candidate_codes: Optional[Iterable[str]] = None,
    ) -> pd.DataFrame:
        if not self.duckdb_path.exists():
            return pd.DataFrame()

        import duckdb  # type: ignore

        stock_wide = pd.DataFrame()
        market_wide = pd.DataFrame()
        value_long = pd.DataFrame()
        last_error: Optional[Exception] = None
        for attempt in range(4):
            con = None
            try:
                con = duckdb.connect(str(self.duckdb_path))
                stock_wide = self._read_table(con, "factor_stock_wide", trade_date)
                reversal = self._read_table(con, "factor_reversal_stock_wide", trade_date)
                if not stock_wide.empty and not reversal.empty:
                    stock_wide = stock_wide.merge(reversal.drop(columns=["trade_date"], errors="ignore"), on="code", how="left")
                market_wide = self._read_table(con, "factor_market_wide", trade_date)
                value_long = self._read_table(con, "factor_value_long", trade_date)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                message = str(exc).lower()
                retryable = any(
                    marker in message
                    for marker in (
                        "another program is using this file",
                        "\u53e6\u4e00\u4e2a\u7a0b\u5e8f\u6b63\u5728\u4f7f\u7528\u6b64\u6587\u4ef6",
                        "cannot open file",
                        "could not set lock",
                    )
                )
                if not retryable or attempt >= 3:
                    raise
                delay = 0.25 * (attempt + 1)
                logger.warning(
                    f"[ScreeningEngine] \u56e0\u5b50\u5e93\u6682\u65f6\u88ab\u5360\u7528\uff0c{delay:.2f}s \u540e\u91cd\u8bd5 "
                    f"({attempt + 1}/3): {exc}"
                )
                time.sleep(delay)
            finally:
                if con is not None:
                    con.close()
        if last_error is not None:
            raise last_error
        if stock_wide.empty:
            return pd.DataFrame()

        stock_wide["code"] = stock_wide["code"].map(_normalize_code)
        base = stock_wide.drop_duplicates("code").set_index("code", drop=False).copy()
        if not market_wide.empty:
            market_row = market_wide.iloc[-1]
            for column, value in market_row.items():
                if str(column) not in {"trade_date", "computed_at"}:
                    base[str(column)] = value

        if not value_long.empty:
            stock_scores = value_long[value_long["entity_type"] == "stock"].copy()
            if not stock_scores.empty:
                stock_scores["entity_id"] = stock_scores["entity_id"].map(_normalize_code)
                pivot = stock_scores.pivot_table(
                    index="entity_id",
                    columns="factor_id",
                    values="score",
                    aggfunc="last",
                )
                base = base.join(pivot, how="left")
                if "raw_value" in stock_scores.columns:
                    raw_scores = stock_scores[
                        stock_scores["factor_id"].isin(self.RAW_VALUE_FACTORS)
                    ].pivot_table(
                        index="entity_id",
                        columns="factor_id",
                        values="raw_value",
                        aggfunc="last",
                    )
                    for factor in raw_scores.columns:
                        base[factor] = raw_scores[factor]

            market_scores = value_long[value_long["entity_type"] == "market"].copy()
            for _, row in market_scores.iterrows():
                factor_id = str(row.get("factor_id") or "")
                if factor_id:
                    # Keep raw market fields (ratios, percentages and counts)
                    # intact. Their normalized scores belong to separate
                    # factor ids and must not overwrite the market snapshot.
                    if factor_id in self.RAW_VALUE_FACTORS:
                        base[factor_id] = _to_float(row.get("raw_value"), math.nan)
                    elif factor_id not in base.columns:
                        base[factor_id] = _to_float(row.get("score"), 50.0)

        alias_map = {
            "stk_total_score": "total_score",
            "stk_liquidity_percentile": "liquidity_score",
            "stk_sector_resonance_score": "sector_resonance_score",
            "stk_amount_ratio_5d": "amount_ratio_score",
            "stk_vol_ratio_5d": "vol_ratio_score",
            "stk_new_high_20d": "new_high_score",
            "stk_pct_chg_1d": "pct_score",
            "stk_limit_progress": "limit_progress_score",
        }
        for factor, wide_col in alias_map.items():
            if factor not in base.columns and wide_col in base.columns:
                base[factor] = base[wide_col]
            elif factor in base.columns and wide_col in base.columns:
                base[factor] = pd.to_numeric(base[factor], errors="coerce").fillna(base[wide_col])

        if "limit_pct" not in base.columns:
            base["limit_pct"] = base.apply(
                lambda row: get_price_limit_pct_points(row.get("code"), row.get("name"), row.get("pre_close")) or 10.0,
                axis=1,
            )
        if "limit_progress" not in base.columns:
            base["limit_progress"] = base.apply(
                lambda row: limit_progress(row.get("pct_chg"), row.get("code"), row.get("name"), row.get("pre_close")),
                axis=1,
            )
        if "limit_progress_score" not in base.columns:
            progress = pd.to_numeric(base.get("limit_progress"), errors="coerce").fillna(0.0)
            base["limit_progress_score"] = ((progress + 1.0) / 2.0 * 100.0).clip(lower=0, upper=100)
        if "stk_limit_progress" not in base.columns:
            base["stk_limit_progress"] = base["limit_progress_score"]

        if candidate_codes is not None:
            code_set = {_normalize_code(c) for c in candidate_codes if _normalize_code(c)}
            base = base[base["code"].isin(code_set)]
        return base.reset_index(drop=True)

    def persist_result(self, result: ScreeningResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"screening_{result.trade_date}.json"
        path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def compare_value(actual: Any, op: str, expected: Any) -> bool:
        op = str(op or "").strip().lower()
        if op in (">", ">=", "<", "<=", "==", "!=", "="):
            a = _to_float(actual, math.nan)
            e = _to_float(expected, math.nan)
            if math.isnan(a) or math.isnan(e):
                return False
            if op == ">":
                return a > e
            if op == ">=":
                return a >= e
            if op == "<":
                return a < e
            if op == "<=":
                return a <= e
            if op in ("=", "=="):
                return a == e
            return a != e
        if op == "in":
            return actual in (expected or [])
        if op == "not_in":
            return actual not in (expected or [])
        if op == "between":
            values = list(expected or [])
            if len(values) != 2:
                return False
            a = _to_float(actual, math.nan)
            return _to_float(values[0], math.nan) <= a <= _to_float(values[1], math.nan)
        raise ValueError(f"unsupported screening op: {op}")

    @staticmethod
    def _read_table(con, table: str, trade_date: str) -> pd.DataFrame:
        exists = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()[0]
        if not exists:
            return pd.DataFrame()
        return con.execute(f"SELECT * FROM {table} WHERE trade_date = ?", [str(trade_date)]).fetchdf()

    def _apply_hard_filters(
        self,
        df: pd.DataFrame,
        cfg: Dict[str, Any],
        neutral_score: float,
        reasons: Dict[str, List[str]],
        rejected: List[Dict[str, Any]],
        result: ScreeningResult,
    ) -> pd.DataFrame:
        working = df
        for rule in cfg.get("hard_filters") or []:
            before = len(working)
            mask = self._mask(working, rule, neutral_score)
            passed = working[mask].copy()
            failed = working[~mask]
            name = str(rule.get("name") or rule.get("factor") or "硬过滤")
            for row in passed.itertuples():
                reasons.setdefault(str(row.code), []).append(str(rule.get("reason") or f"通过硬过滤：{name}"))
            for row in failed.itertuples():
                rejected.append(self._reject_row(row, "hard_filter", rule, f"未通过硬过滤：{name}"))
            result.traces.append(FilterTrace(
                stage="hard_filter",
                name=name,
                factor=str(rule.get("factor") or ""),
                op=str(rule.get("op") or ""),
                value=rule.get("value"),
                before_count=before,
                passed_count=len(passed),
                kept_count=len(passed),
            ))
            working = passed
            if working.empty:
                break
        return working

    def _apply_exclusion_filters(
        self,
        df: pd.DataFrame,
        cfg: Dict[str, Any],
        neutral_score: float,
        rejected: List[Dict[str, Any]],
        result: ScreeningResult,
    ) -> pd.DataFrame:
        """Reject rows matching any configured exclusion condition."""
        working = df
        for rule in cfg.get("exclusion_filters") or []:
            before = len(working)
            matched = self._mask(working, rule, neutral_score)
            removed = working[matched]
            kept = working[~matched].copy()
            name = str(rule.get("name") or rule.get("factor") or "排除条件")
            for row in removed.itertuples():
                rejected.append(self._reject_row(row, "exclusion_filter", rule, f"命中排除条件：{name}"))
            result.traces.append(FilterTrace(
                stage="exclusion_filter",
                name=name,
                factor=str(rule.get("factor") or ""),
                op=str(rule.get("op") or ""),
                value=rule.get("value"),
                before_count=before,
                passed_count=len(kept),
                kept_count=len(kept),
            ))
            working = kept
            if working.empty:
                break
        return working

    def _apply_priority_filters(
        self,
        df: pd.DataFrame,
        cfg: Dict[str, Any],
        neutral_score: float,
        reasons: Dict[str, List[str]],
        rejected: List[Dict[str, Any]],
        result: ScreeningResult,
    ) -> pd.DataFrame:
        working = df
        filters = sorted(cfg.get("priority_filters") or [], key=lambda r: int(r.get("priority") or 999))
        for rule in filters:
            before = len(working)
            if before <= 1:
                break
            mask = self._mask(working, rule, neutral_score)
            passed = working[mask].copy()
            failed = working[~mask]
            name = str(rule.get("name") or rule.get("factor") or "优先过滤")

            min_keep = int(rule.get("min_keep") or 0)
            max_drop_ratio = rule.get("max_drop_ratio")
            floor_keep = 0
            if max_drop_ratio is not None:
                floor_keep = math.ceil(before * (1.0 - max(0.0, min(float(max_drop_ratio), 1.0))))
            target_keep = max(min_keep, floor_keep)
            target_keep = min(target_keep, before)

            relaxed = False
            if target_keep and len(passed) < target_keep:
                relaxed = True
                condition_codes = set(working.loc[mask, "code"].astype(str)) if "code" in working.columns else set()
                passed = working.sort_values("_screening_score", ascending=False).head(target_keep).copy()
                failed = working[~working["code"].isin(set(passed["code"]))]
            else:
                condition_codes = set(passed["code"].astype(str)) if "code" in passed.columns else set()

            for row in passed.itertuples():
                code = str(row.code)
                if code in condition_codes:
                    reasons.setdefault(code, []).append(str(rule.get("reason") or f"通过优先过滤：{name}"))
                elif relaxed:
                    reasons.setdefault(code, []).append(f"未完全满足{name}，按综合评分保留观察")
            for row in failed.itertuples():
                rejected.append(self._reject_row(row, "priority_filter", rule, f"未通过优先过滤：{name}"))
            result.traces.append(FilterTrace(
                stage="priority_filter",
                name=name,
                factor=str(rule.get("factor") or ""),
                op=str(rule.get("op") or ""),
                value=rule.get("value"),
                before_count=before,
                passed_count=int(mask.sum()),
                kept_count=len(passed),
                relaxed=relaxed,
                message="触发 min_keep/max_drop_ratio，按综合分保留" if relaxed else "",
            ))
            working = passed
        return working

    def _rank(
        self,
        df: pd.DataFrame,
        cfg: Dict[str, Any],
        neutral_score: float,
        reasons: Dict[str, List[str]],
        *,
        score_series: Optional[pd.Series] = None,
        base_series: Optional[pd.Series] = None,
        model_baseline_series: Optional[pd.Series] = None,
        top_n_override: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if df.empty:
            return []
        ranked = df.copy()
        if score_series is None or base_series is None:
            scenario_scores = self._scenario_scores(ranked, cfg, neutral_score)
            base_series = scenario_scores["no_lhb"]
            score_series = scenario_scores["lhb_sector"]
        ranked["_screening_base_score"] = base_series
        if model_baseline_series is None:
            model_baseline_series = score_series
        ranked["_screening_model_baseline_score"] = model_baseline_series
        ranked["_screening_score"] = score_series
        ranked["_lhb_adjustment"] = ranked["_screening_model_baseline_score"] - ranked["_screening_base_score"]
        ranked["_signal_adjustment"] = ranked["_screening_score"] - ranked["_screening_model_baseline_score"]
        ranking_cfg = cfg.get("ranking") or {}
        top_n = int(top_n_override or ranking_cfg.get("top_n") or 10)
        ranked = ranked.sort_values(
            ["_screening_score", "_lhb_adjustment", "stk_total_score"],
            ascending=[False, False, False],
        ).head(top_n)
        try:
            risk_cfg = self._risk_config
            if risk_cfg is None:
                raise RuntimeError("risk config unavailable")
            position_budget_pct = min(
                risk_cfg.fixed_risk_per_trade / max(risk_cfg.hard_stop_loss, 1e-6),
                risk_cfg.kelly_max_position,
                risk_cfg.max_position_per_stock,
            ) * 100.0
            account_risk_pct = risk_cfg.fixed_risk_per_trade * 100.0
        except Exception:
            position_budget_pct = 0.0
            account_risk_pct = 0.0
        position_constraints: List[str] = []
        if self._active_regime == "weak" and position_budget_pct > 8.0:
            position_budget_pct = 8.0
            position_constraints.append("弱市试仓单票不超过8%")
        strategy_position_cap = _to_float(cfg.get("position_cap_pct"), 0.0)
        if strategy_position_cap > 0 and position_budget_pct > strategy_position_cap:
            position_budget_pct = strategy_position_cap
            position_constraints.append(f"策略单票上限{strategy_position_cap:g}%")
        if self._market_state_snapshot is not None:
            position_constraints.append(
                f"{self._market_state_snapshot.phase_label}仓位约束"
            )
        final: List[Dict[str, Any]] = []
        rule_factors = [
            str(rule.get("factor"))
            for section in ("hard_filters", "priority_filters", "evidence_rules", "veto_rules")
            for rule in cfg.get(section) or []
            if rule.get("factor")
        ]
        metric_cols = list(dict.fromkeys(list((ranking_cfg.get("weights") or {}).keys()) + rule_factors + [
            "mkt_market_score",
            "stk_lhb_net_buy_score",
            "stk_lhb_institution_score",
            "stk_lhb_institution_consensus",
            "stk_lhb_repeat_persistence",
            "stk_lhb_sector_resonance",
            "stk_lhb_composite_score",
            "stk_lhb_crowding_risk",
            "stk_capital_flow_consensus",
            "stk_capital_flow_persistence",
            "stk_attention_consensus",
            "stk_attention_crowding_risk",
            "stk_kpl_leader_quality",
            "stk_margin_acceleration",
            "stk_block_trade_risk",
            "stk_behavior_attention",
            "stk_behavior_acceleration",
            "stk_behavior_divergence",
            "stk_behavior_repair",
            "stk_behavior_decay",
        ]))
        context_cols = [
            "pct_chg",
            "vol_ratio",
            "amount_ratio",
            "new_high_ratio",
            "limit_pct",
            "limit_progress",
            "limit_progress_score",
            "liquidity_score",
            "board_height",
            "board_score",
            "seal_time_score",
            "sector_heat_score",
            "sector_persistence_score",
            "sector_mainline_score",
            "sector_resonance_score",
            "lhb_present",
            "lhb_source_available",
            "lhb_net_buy_ratio",
            "institution_net_buy_ratio",
            "appearance_days_5d",
            "crowding_penalty_score",
            "capital_flow_consensus_score",
            "capital_flow_persistence_score",
            "attention_score",
            "attention_crowding_penalty",
            "leader_quality_score",
            "margin_score",
            "event_risk_score",
            "sector_flow_score",
            "behavior_data_completeness",
        ]
        for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
            code = str(row.get("code") or "")
            metrics = {
                col: value if math.isfinite(value) else None
                for col in metric_cols
                for value in [_to_float(row.get(col), math.nan)]
            }
            lhb_present = bool(_to_float(row.get("lhb_present"), 0.0))
            if not lhb_present:
                metrics = {key: value for key, value in metrics.items() if not key.startswith("stk_lhb_")}
            context = {
                col: _to_float(row.get(col), 0.0)
                for col in context_cols
                if col in ranked.columns
            }
            score = round(_to_float(row.get("_screening_score"), 0.0), 4)
            base_reasons = reasons.get(code, [])[:8]
            penalty_reasons = self._penalty_reasons(row, cfg, neutral_score)
            configured_factors = list((ranking_cfg.get("weights") or {}).keys())
            observed = sum(
                1 for factor in configured_factors
                if factor in ranked.columns and pd.notna(row.get(factor))
            )
            completeness = observed / len(configured_factors) if configured_factors else 0.0
            liquidity = _to_float(row.get("stk_liquidity_percentile"), _to_float(row.get("liquidity_score"), 50.0))
            tradability = max(0.5, min(1.0, liquidity / 70.0))
            confidence = ConfidenceService.from_profile(
                self._confidence_profile,
                score=score,
                data_completeness=completeness,
                regime_match=1.0 if self._confidence_profile else 0.75,
                tradability=tradability,
                model_drift=self._confidence_drift,
                model_type=self._confidence_model_type,
                as_of_date=self._confidence_as_of_date,
            )
            profile_sample_size = int(confidence.get("sample_size") or 0)
            meta_probability = row.get("_meta_probability")
            has_individual_stop_model = pd.notna(row.get("_model_stop_probability"))
            from core.models.probability_ensemble import blend_candidate_probability

            raw_meta_probability = (
                _to_float(meta_probability, math.nan)
                if pd.notna(meta_probability) and self._candidate_model_metadata.get("active")
                else None
            )
            ensemble = blend_candidate_probability(
                lightgbm_meta=raw_meta_probability,
                ic_ir_percentile=score / 100.0,
                similar_history=confidence["candidate_probability"] / 100.0,
            )
            if ensemble.get("available"):
                from core.signals.trust_algorithms import beta_binomial_interval

                calibrated_probability = float(ensemble["probability"])
                model_samples = int(self._candidate_model_metadata.get("validation_rows") or 0)
                effective_samples = min(model_samples, max(
                    int(self._candidate_model_metadata.get("validation_days") or 1) * 50, 20,
                )) if raw_meta_probability is not None else profile_sample_size
                probability_interval = beta_binomial_interval(
                    calibrated_probability * effective_samples, effective_samples,
                )
                confidence = ConfidenceService.assess(
                    calibrated_probability=calibrated_probability,
                    baseline_probability=(
                        _to_float(
                            self._candidate_model_metadata.get("baseline_probability"),
                            confidence["baseline_probability"] / 100.0,
                        )
                        if raw_meta_probability is not None
                        else confidence["baseline_probability"] / 100.0
                    ),
                    expected_return=(
                        _to_float(row.get("_model_expected_return"), confidence["expected_return_pct"] / 100.0)
                        if raw_meta_probability is not None
                        else confidence["expected_return_pct"] / 100.0
                    ),
                    expected_gross_return=(
                        _to_float(
                            row.get("_model_expected_gross_return"),
                            confidence.get("expected_gross_return_pct", confidence["expected_return_pct"]) / 100.0,
                        )
                        if raw_meta_probability is not None
                        else confidence.get("expected_gross_return_pct", confidence["expected_return_pct"]) / 100.0
                    ),
                    stop_probability=_to_float(
                        row.get("_model_stop_probability"),
                        confidence["stop_probability"] / 100.0,
                    ),
                    sample_size=effective_samples,
                    average_mfe=confidence["average_mfe_pct"] / 100.0,
                    average_mae=confidence["average_mae_pct"] / 100.0,
                    data_completeness=completeness,
                    regime_match=1.0,
                    tradability=tradability,
                    probability_interval={
                        "low": probability_interval["lower"],
                        "high": probability_interval["upper"],
                    },
                    return_interval={
                        "low": _to_float(
                            row.get("_model_return_low"), confidence["return_interval_low_pct"] / 100.0,
                        ),
                        "high": _to_float(
                            row.get("_model_return_high"), confidence["return_interval_high_pct"] / 100.0,
                        ),
                    },
                    gross_return_interval={
                        "low": _to_float(
                            row.get("_model_gross_return_low"),
                            confidence.get("gross_return_interval_low_pct", confidence["return_interval_low_pct"]) / 100.0,
                        ),
                        "high": _to_float(
                            row.get("_model_gross_return_high"),
                            confidence.get("gross_return_interval_high_pct", confidence["return_interval_high_pct"]) / 100.0,
                        ),
                    },
                    calibration=(
                        self._candidate_model_metadata.get("calibration") or {}
                        if raw_meta_probability is not None
                        else {
                            "brier_score": confidence.get("brier_score"),
                            "ece": (
                                confidence.get("ece") / 100.0
                                if confidence.get("ece") is not None else None
                            ),
                        }
                    ),
                    model_drift=self._confidence_drift,
                    model_type=f"ensemble_{ensemble['mode']}",
                    as_of_date=self._confidence_as_of_date,
                )
            final.append({
                "code": code,
                "ts_code": str(row.get("ts_code") or ""),
                "name": str(row.get("name") or ""),
                "resonance_sectors": str(row.get("resonance_sectors") or ""),
                "behavior_state": str(row.get("behavior_dominant_state") or ""),
                "behavior_state_label": str(row.get("behavior_dominant_label") or ""),
                "behavior_state_probability": round(
                    _to_float(row.get("behavior_dominant_probability")), 2,
                ),
                "behavior_state_probabilities": {
                    state: round(_to_float(row.get(f"behavior_{state}_probability")), 2)
                    for state in ("attention", "acceleration", "divergence", "repair", "decay")
                },
                "score": score,
                "base_score": round(_to_float(row.get("_screening_base_score"), score), 4),
                "model_baseline_score": round(_to_float(row.get("_screening_model_baseline_score"), score), 4),
                "lhb_adjustment": round(_to_float(row.get("_lhb_adjustment"), 0.0), 4),
                "signal_adjustment": round(_to_float(row.get("_signal_adjustment"), 0.0), 4),
                "model_rank_score": round(_to_float(row.get("_lgb_rank_score"), score), 4),
                "meta_label_probability": (
                    round(raw_meta_probability * 100.0, 2)
                    if raw_meta_probability is not None else None
                ),
                "probability_ensemble": ensemble,
                "shap_explanation": row.get("_shap_explanation") if isinstance(row.get("_shap_explanation"), list) else [],
                "rank": rank,
                "gold_rank": int(_to_float(row.get("rank"), rank)),
                "candidate_probability": confidence["candidate_probability"],
                "baseline_probability": confidence["baseline_probability"],
                "probability_lift": confidence["probability_lift"],
                "expected_return_pct": confidence["expected_return_pct"],
                "expected_excess_return_pct": confidence["expected_return_pct"],
                "expected_gross_return_pct": confidence.get("expected_gross_return_pct"),
                "gross_return_interval_low_pct": confidence.get("gross_return_interval_low_pct"),
                "gross_return_interval_high_pct": confidence.get("gross_return_interval_high_pct"),
                "forecast_horizon_days": int(self._weight_metadata.get("horizon_days") or 3),
                "stop_probability": confidence["stop_probability"],
                "stop_probability_source": (
                    "个股样本外止损模型"
                    if has_individual_stop_model
                    else "同市场同评分层历史基准"
                ),
                "stop_probability_definition": "次日可成交价起3日内先触发-4%，且未先达到+6%",
                "similar_sample_size": profile_sample_size,
                "model_validation_sample_size": confidence["sample_size"],
                "average_mfe_pct": confidence["average_mfe_pct"],
                "average_mae_pct": confidence["average_mae_pct"],
                "confidence_grade": confidence["confidence_grade"],
                "confidence_score": confidence["confidence_score"],
                "data_completeness": confidence["data_completeness"],
                "regime_match": confidence["regime_match"],
                "tradability": confidence["tradability"],
                "sample_reliability": confidence["sample_reliability"],
                "model_type": confidence["model_type"],
                "model_as_of_date": confidence["as_of_date"],
                "probability_ci_low": confidence["probability_ci_low"],
                "probability_ci_high": confidence["probability_ci_high"],
                "return_interval_low_pct": confidence["return_interval_low_pct"],
                "return_interval_high_pct": confidence["return_interval_high_pct"],
                "brier_score": confidence["brier_score"],
                "ece": confidence["ece"],
                "model_drift_status": confidence["model_drift_status"],
                "drift_psi": confidence["drift_psi"],
                "drift_ks": confidence["drift_ks"],
                "decision_status": confidence["decision_status"],
                "decision_label": confidence["decision_label"],
                "position_budget_pct": round(position_budget_pct, 2),
                "position_budget_reason": "；".join(position_constraints) or "按账户风险预算计算",
                "market_regime": self._active_regime,
                "emotion_phase": (
                    self._market_state_snapshot.phase
                    if self._market_state_snapshot is not None else ""
                ),
                "emotion_phase_label": (
                    self._market_state_snapshot.phase_label
                    if self._market_state_snapshot is not None else ""
                ),
                "market_position_scale": (
                    self._market_state_snapshot.position_scale
                    if self._market_state_snapshot is not None else 1.0
                ),
                "market_total_position_cap_pct": round(
                    (
                        self._market_state_snapshot.position_scale
                        if self._market_state_snapshot is not None else 1.0
                    ) * 100.0,
                    2,
                ),
                "strategy_position_multiplier": _to_float(
                    self._weight_metadata.get("strategy_position_multiplier"), 1.0,
                ),
                "market_risk_flags": list(
                    self._market_state_snapshot.risk_flags
                    if self._market_state_snapshot is not None else ()
                ),
                "emotion_phase_reasons": list(
                    self._market_state_snapshot.phase_reasons
                    if self._market_state_snapshot is not None else ()
                ),
                "worst_expected_loss_pct": round(
                    min(
                        account_risk_pct,
                        position_budget_pct / 100.0
                        * abs(min(confidence["return_interval_low_pct"], 0.0)),
                    ), 2
                ),
                "trust_layers": confidence["trust_layers"],
                "confidence": confidence,
                "reasons": build_screening_reasons(
                    metrics=metrics,
                    context=context,
                    score=score,
                    rank=rank,
                    base_reasons=base_reasons,
                ),
                "rule_reasons": base_reasons,
                "penalty_reasons": penalty_reasons,
                "metrics": metrics,
                "context": context,
                "reversal_structures": json.loads(row.get("reversal_structures") or "{}")
                if isinstance(row.get("reversal_structures"), str) else {},
                "lhb": {
                    "present": lhb_present,
                    "signal_date": str(row.get("signal_date") or ""),
                    "effective_date": str(row.get("effective_date") or ""),
                    "adjustment": round(_to_float(row.get("_lhb_adjustment"), 0.0), 4),
                },
                "enhancements": {
                    key: round(_to_float(row.get(str(definition["column"])), 0.0), 4)
                    for key, definition in ENHANCEMENT_DEFINITIONS.items()
                },
            })
        return final

    def _rank_scenarios(
        self,
        df: pd.DataFrame,
        cfg: Dict[str, Any],
        neutral_score: float,
        reasons: Dict[str, List[str]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        if df.empty:
            return {key: [] for key in ("no_lhb", "net_buy", "institution", "lhb_sector", "enhanced_all")}
        scores = self._scenario_scores(df, cfg, neutral_score)
        output: Dict[str, List[Dict[str, Any]]] = {}
        for scenario, score_series in scores.items():
            output[scenario] = self._rank(
                df, cfg, neutral_score, reasons,
                score_series=score_series, base_series=scores["no_lhb"], model_baseline_series=score_series,
            )
        output["enhanced_all"] = self._rank(
            df, cfg, neutral_score, reasons,
            score_series=scores["lhb_sector"] + self._enhancement_adjustment(df, cfg),
            base_series=scores["no_lhb"], model_baseline_series=scores["lhb_sector"],
        )
        return output

    def _enhancement_adjustment(self, df: pd.DataFrame, cfg: Optional[Dict[str, Any]] = None) -> pd.Series:
        adjustment = pd.Series(0.0, index=df.index, dtype=float)
        configured = (cfg or {}).get("enhancements") or {}
        enabled_keys = configured.get("enabled")
        enabled_keys = set(enabled_keys) if enabled_keys is not None else None
        factor_switches = {
            "capital_flow": "stk_capital_flow_consensus",
            "attention": "stk_attention_consensus",
            "leader": "stk_kpl_leader_quality",
            "margin": "stk_margin_acceleration",
            "risk": "stk_block_trade_risk",
        }
        for key, definition in ENHANCEMENT_DEFINITIONS.items():
            if enabled_keys is not None and key not in enabled_keys:
                continue
            enabled = True
            try:
                from core.factors.factor_registry import get_factor_registry

                factor = get_factor_registry().get_factor(factor_switches[key])
                enabled = factor is None or bool(factor.enabled)
            except Exception:
                pass
            if enabled:
                adjustment += self._factor_series(df, str(definition["column"]), 0.0)
        return adjustment.clip(lower=-10.0, upper=10.0)

    @staticmethod
    def _factor_series(df: pd.DataFrame, factor: str, neutral_score: float) -> pd.Series:
        """取因子列为数值 Series；缺失列退化为中性分（避免 df.get 返回标量导致 .fillna 崩溃）。"""
        col = df[factor] if factor in df.columns else pd.Series(neutral_score, index=df.index)
        return pd.to_numeric(col, errors="coerce").fillna(neutral_score)

    def _ranking_score(self, df: pd.DataFrame, cfg: Dict[str, Any], neutral_score: float) -> pd.Series:
        return self._scenario_scores(df, cfg, neutral_score)["lhb_sector"]

    def _base_ranking_score(self, df: pd.DataFrame, cfg: Dict[str, Any], neutral_score: float) -> pd.Series:
        ranking_cfg = cfg.get("ranking") or {}
        if bool(ranking_cfg.get("candidate_percentile")):
            return self._candidate_percentile_score(df, cfg, neutral_score)
        weights = ranking_cfg.get("weights") or {"stk_total_score": 1.0}
        total_weight = sum(abs(float(w)) for w in weights.values())
        if total_weight <= 0:
            return pd.Series([neutral_score] * len(df), index=df.index)
        score = pd.Series([0.0] * len(df), index=df.index, dtype=float)
        for factor, weight in weights.items():
            weight = float(weight)
            values = self._factor_series(df, factor, neutral_score)
            component = values if weight >= 0 else 100.0 - values
            score += component * abs(weight)
        score = score / total_weight
        penalty = self._ranking_penalty(df, cfg, neutral_score)
        return (score - penalty).clip(lower=0, upper=100)

    def _scenario_scores(
        self, df: pd.DataFrame, cfg: Dict[str, Any], neutral_score: float,
    ) -> Dict[str, pd.Series]:
        base = self._base_ranking_score(df, cfg, neutral_score)
        if "_lgb_rank_score" in df.columns:
            learned = pd.to_numeric(df["_lgb_rank_score"], errors="coerce").fillna(base)
            base = 0.35 * base + 0.65 * learned
        lhb_cfg = cfg.get("lhb_enhancement") or {}
        if not bool(lhb_cfg.get("enabled", True)):
            return {key: base.copy() for key in ("no_lhb", "net_buy", "institution", "lhb_sector")}
        active = self._factor_series(df, "lhb_present", 0.0) > 0

        def enabled(factor: str) -> bool:
            try:
                from core.factors.factor_registry import get_factor_registry

                definition = get_factor_registry().get_factor(factor)
                return bool(definition.enabled) if definition is not None else True
            except Exception:
                return True

        def delta(factor: str, bonus: float, penalty: float) -> pd.Series:
            if not enabled(factor):
                return pd.Series(0.0, index=df.index, dtype=float)
            values = self._factor_series(df, factor, neutral_score)
            positive = ((values - 50.0) / 50.0).clip(lower=0, upper=1) * max(float(bonus), 0.0)
            negative = ((50.0 - values) / 50.0).clip(lower=0, upper=1) * max(float(penalty), 0.0)
            return (positive - negative).where(active, 0.0)

        components = lhb_cfg.get("components") or {}
        net_cfg = components.get("net_buy") or {}
        inst_cfg = components.get("institution") or {}
        consensus_cfg = components.get("institution_consensus") or {}
        repeat_cfg = components.get("repeat_persistence") or {}
        sector_cfg = components.get("sector_resonance") or {}
        net = delta("stk_lhb_net_buy_score", net_cfg.get("max_bonus", 2.5), net_cfg.get("max_penalty", 3.0))
        institution = delta(
            "stk_lhb_institution_score", inst_cfg.get("max_bonus", 2.5), inst_cfg.get("max_penalty", 2.5)
        )
        consensus = delta(
            "stk_lhb_institution_consensus", consensus_cfg.get("max_bonus", 1.0), consensus_cfg.get("max_penalty", 1.0)
        )
        repeat = delta(
            "stk_lhb_repeat_persistence", repeat_cfg.get("max_bonus", 0.8), repeat_cfg.get("max_penalty", 0.8)
        )
        sector = delta(
            "stk_lhb_sector_resonance", sector_cfg.get("max_bonus", 2.0), sector_cfg.get("max_penalty", 2.0)
        )
        if enabled("stk_lhb_crowding_risk"):
            crowding = (
                self._factor_series(df, "crowding_penalty_score", 0.0).clip(lower=0, upper=100)
                / 100.0 * _to_float(lhb_cfg.get("max_crowding_penalty"), 3.0)
            ).where(active, 0.0)
        else:
            crowding = pd.Series(0.0, index=df.index, dtype=float)
        full = net + institution + consensus + repeat + sector - crowding
        cap = max(_to_float(lhb_cfg.get("max_total_adjustment"), 8.0), 0.0)
        full = full.clip(lower=-cap, upper=cap)
        return {
            "no_lhb": base.clip(lower=0, upper=100),
            "net_buy": (base + net).clip(lower=0, upper=100),
            "institution": (base + institution + consensus).clip(lower=0, upper=100),
            "lhb_sector": (base + full).clip(lower=0, upper=100),
        }

    def _candidate_percentile_score(
        self, df: pd.DataFrame, cfg: Dict[str, Any], neutral_score: float,
    ) -> pd.Series:
        """Rank each factor inside the current candidate pool before weighting."""
        weights = (cfg.get("ranking") or {}).get("weights") or {"stk_total_score": 1.0}
        total_weight = sum(abs(float(weight)) for weight in weights.values())
        if total_weight <= 0 or df.empty:
            return pd.Series([neutral_score] * len(df), index=df.index, dtype=float)
        score = pd.Series(0.0, index=df.index, dtype=float)
        for factor, weight in weights.items():
            weight = float(weight)
            values = self._factor_series(df, factor, neutral_score)
            if values.nunique(dropna=True) <= 1:
                percentile = pd.Series(50.0, index=df.index, dtype=float)
            else:
                percentile = values.rank(method="average", pct=True) * 100.0
            component = percentile if weight >= 0 else 100.0 - percentile
            score += component * abs(weight)
        penalty = self._ranking_penalty(df, cfg, neutral_score)
        return (score / total_weight - penalty).clip(lower=0, upper=100)

    def _ranking_penalty(self, df: pd.DataFrame, cfg: Dict[str, Any], neutral_score: float) -> pd.Series:
        penalty = pd.Series([0.0] * len(df), index=df.index, dtype=float)
        for rule in ((cfg.get("ranking") or {}).get("penalties") or []):
            factor = str(rule.get("factor") or "")
            max_penalty = _to_float(rule.get("max_penalty"), 0.0)
            if not factor or max_penalty <= 0:
                continue
            values = self._factor_series(df, factor, neutral_score)
            if "below" in rule:
                below = _to_float(rule.get("below"), neutral_score)
                floor = _to_float(rule.get("floor"), 0.0)
                if below <= floor:
                    floor = 0.0
                shortfall = ((below - values) / max(below - floor, 1e-9)).clip(lower=0, upper=1)
                penalty += shortfall * max_penalty
            if "above" in rule:
                above = _to_float(rule.get("above"), neutral_score)
                ceiling = _to_float(rule.get("ceiling"), 100.0)
                if ceiling <= above:
                    ceiling = above + 1.0
                excess = ((values - above) / max(ceiling - above, 1e-9)).clip(lower=0, upper=1)
                penalty += excess * max_penalty
        return penalty

    def _penalty_reasons(self, row: Any, cfg: Dict[str, Any], neutral_score: float) -> List[str]:
        out: List[str] = []
        for rule in ((cfg.get("ranking") or {}).get("penalties") or []):
            factor = str(rule.get("factor") or "")
            if not factor:
                continue
            value = _to_float(row.get(factor) if hasattr(row, "get") else getattr(row, factor, None), neutral_score)
            below = _to_float(rule.get("below"), neutral_score) if "below" in rule else None
            above = _to_float(rule.get("above"), neutral_score) if "above" in rule else None
            if below is not None and value < below:
                reason = str(rule.get("reason") or rule.get("name") or f"{factor} 低于 {below}")
                out.append(f"{reason}（{factor}={value:.1f}）")
            if above is not None and value > above:
                reason = str(rule.get("reason") or rule.get("name") or f"{factor} 高于 {above}")
                out.append(f"{reason}（{factor}={value:.1f}）")
        return out

    def _mask(self, df: pd.DataFrame, rule: Dict[str, Any], neutral_score: float) -> pd.Series:
        factor = str(rule.get("factor") or "")
        values = df[factor] if factor in df.columns else pd.Series([neutral_score] * len(df), index=df.index)
        return values.map(lambda v: self.compare_value(v, str(rule.get("op") or ">="), rule.get("value")))

    @staticmethod
    def _reject_row(row: Any, stage: str, rule: Dict[str, Any], reason: str) -> Dict[str, Any]:
        factor = str(rule.get("factor") or "")
        value = getattr(row, factor, None) if hasattr(row, factor) else None
        return {
            "code": str(getattr(row, "code", "")),
            "name": str(getattr(row, "name", "")),
            "stage": stage,
            "factor": factor,
            "value": _to_float(value, 0.0) if value is not None else None,
            "reason": reason,
        }


def run_screening(
    trade_date: str,
    *,
    profile: str = "default",
    duckdb_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    engine = ScreeningEngine(duckdb_path=duckdb_path, output_dir=output_dir)
    return engine.run(trade_date, profile=profile, persist=persist).to_dict()
