"""Per-strategy data, training and runtime diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping


class StrategyDiagnosticsService:
    MIN_FILLED_SAMPLES = 200
    MIN_TRADE_DAYS = 60
    MIN_OOS_MONTHS = 3
    FACTOR_SOURCE_GROUPS = {
        "lhb": {
            "match": ("lhb",),
            "sources": ("top_list", "top_inst", "hm_detail"),
            "label": "龙虎榜增强",
        },
        "capital_flow": {
            "match": ("capital_flow",),
            "sources": ("moneyflow_ths", "moneyflow_dc", "sector_moneyflow_ths"),
            "label": "资金流增强",
        },
        "attention": {
            "match": ("attention",),
            "sources": ("ths_hot", "dc_hot"),
            "label": "热度增强",
        },
        "leader": {
            "match": ("kpl", "leader_quality"),
            "sources": ("kpl_list",),
            "label": "龙头榜增强",
        },
        "margin": {
            "match": ("margin",),
            "sources": ("margin_detail",),
            "label": "两融增强",
        },
        "event": {
            "match": ("block_trade", "event_risk"),
            "sources": ("block_trade",),
            "label": "事件风险增强",
        },
    }

    def __init__(self, *, duckdb_path: Path | None = None, cache_dir: Path | None = None) -> None:
        from config.settings import CACHE_DIR, FACTOR_DB_PATH, WEB_DATA_DIR

        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.web_data_dir = Path(WEB_DATA_DIR)

    def build(
        self, profile: Mapping[str, Any], trade_date: str,
        candidates: Iterable[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        strategy_id = str(profile.get("id") or "default")
        factors = self._required_factors(profile)
        source_coverage = self.source_coverage(profile, str(trade_date))
        unavailable_optional = {
            factor
            for group in (source_coverage.get("groups") or {}).values()
            if not group.get("available")
            for factor in (group.get("factors") or [])
        }
        operational_factors = [
            factor for factor in factors if factor not in unavailable_optional
        ]
        factor_coverage = self._factor_coverage(
            str(trade_date), operational_factors,
        )
        factor_coverage["optional_disabled"] = sorted(unavailable_optional)
        factor_coverage["configured"] = len(factors)
        candidate_rows = [dict(row) for row in candidates if isinstance(row, Mapping)]
        market_data = self._market_data_coverage(str(trade_date), candidate_rows)
        model = self._model_status(profile, str(trade_date))
        reasons = []
        if factor_coverage["coverage_pct"] < 95.0:
            reasons.append("必需因子覆盖不足")
        if market_data["candidate_count"] and market_data["minute_coverage_pct"] < 80.0:
            reasons.append("次日分钟数据覆盖不足")
        if model["runtime_status"] == "fallback_active":
            reasons.append("机器学习未通过发布闸门，规则回退仍可用")
        disabled = source_coverage.get("disabled_enhancements") or []
        if disabled:
            reasons.append("缺源增强已停用：" + "、".join(disabled))
        return {
            "strategy_id": strategy_id,
            "factor_coverage": factor_coverage,
            "market_data_coverage": market_data,
            "source_coverage": source_coverage,
            "model": model,
            "strategy_available": bool(candidate_rows),
            "diagnosis": reasons or ["数据与运行链路正常"],
        }

    def source_coverage(
        self, profile: Mapping[str, Any], trade_date: str,
    ) -> Dict[str, Any]:
        factors = self._required_factors(profile)
        path = self.web_data_dir / "fetch_status" / f"fetch_{trade_date}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            payload = {}
        sources = dict(payload.get("sources") or {})
        groups: Dict[str, Any] = {}
        disabled = []
        required_sources = set()
        for key, spec in self.FACTOR_SOURCE_GROUPS.items():
            matched = [factor for factor in factors if any(token in factor for token in spec["match"])]
            if not matched:
                continue
            names = tuple(spec["sources"])
            required_sources.update(names)
            available = [
                name for name in names
                if bool((sources.get(name) or {}).get("ok"))
            ]
            # Multi-vendor groups remain usable when at least one independent
            # provider is present; single-source groups require that source.
            usable = bool(available)
            if not usable:
                disabled.append(str(spec["label"]))
            groups[key] = {
                "label": spec["label"],
                "factors": matched,
                "sources": list(names),
                "available_sources": available,
                "available": usable,
            }
        available_count = sum(
            1 for name in required_sources if bool((sources.get(name) or {}).get("ok"))
        )
        return {
            "manifest_available": bool(payload),
            "required_source_count": len(required_sources),
            "available_source_count": available_count,
            "coverage_pct": round(available_count / len(required_sources) * 100.0, 2)
            if required_sources else 100.0,
            "groups": groups,
            "disabled_enhancements": disabled,
        }

    @classmethod
    def unavailable_factor_tokens(
        cls, profile: Mapping[str, Any], trade_date: str,
    ) -> Dict[str, Any]:
        service = cls()
        coverage = service.source_coverage(profile, trade_date)
        tokens = []
        for key, group in (coverage.get("groups") or {}).items():
            if not group.get("available"):
                tokens.extend(cls.FACTOR_SOURCE_GROUPS[key]["match"])
        return {"tokens": sorted(set(tokens)), **coverage}

    @staticmethod
    def _required_factors(profile: Mapping[str, Any]) -> list[str]:
        factors = {
            str(row.get("factor") or "")
            for row in (profile.get("ranking_factors") or [])
            if isinstance(row, Mapping)
        }
        for group in (
            "required_filters", "exclusion_filters", "veto_rules", "evidence_rules",
        ):
            factors.update(
                str(row.get("factor") or "")
                for row in (profile.get(group) or [])
                if isinstance(row, Mapping)
            )
        return sorted(item for item in factors if item)

    def _factor_coverage(self, trade_date: str, factors: list[str]) -> Dict[str, Any]:
        if not factors or not self.duckdb_path.exists():
            return {"coverage_pct": 0.0 if factors else 100.0, "required": len(factors), "missing": factors}
        try:
            import duckdb  # type: ignore

            con = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                stock_columns = {
                    str(row[0])
                    for row in con.execute("DESCRIBE factor_stock_wide").fetchall()
                }
                market_columns = {
                    str(row[0])
                    for row in con.execute("DESCRIBE factor_market_wide").fetchall()
                }
                stock_rows = int(con.execute(
                    "SELECT COUNT(*) FROM factor_stock_wide "
                    "WHERE CAST(trade_date AS VARCHAR)=?",
                    [trade_date],
                ).fetchone()[0] or 0)
                ratios: Dict[str, float] = {}
                for factor in factors:
                    if factor in stock_columns:
                        value = con.execute(
                            f'SELECT AVG(CASE WHEN "{factor}" IS NULL THEN 0.0 ELSE 1.0 END) '
                            "FROM factor_stock_wide WHERE CAST(trade_date AS VARCHAR)=?",
                            [trade_date],
                        ).fetchone()[0]
                        ratios[factor] = float(value or 0.0)
                        continue
                    if factor in market_columns:
                        value = con.execute(
                            f'SELECT MAX(CASE WHEN "{factor}" IS NULL THEN 0.0 ELSE 1.0 END) '
                            "FROM factor_market_wide WHERE CAST(trade_date AS VARCHAR)=?",
                            [trade_date],
                        ).fetchone()[0]
                        ratios[factor] = float(value or 0.0)
                        continue
                    row = con.execute(
                        """
                        SELECT entity_type,
                               AVG(CASE WHEN COALESCE(raw_value, score) IS NULL
                                   THEN 0.0 ELSE 1.0 END)
                        FROM factor_value_long
                        WHERE CAST(trade_date AS VARCHAR)=? AND factor_id=?
                        GROUP BY entity_type
                        ORDER BY CASE entity_type WHEN 'stock' THEN 1 ELSE 2 END
                        LIMIT 1
                        """,
                        [trade_date, factor],
                    ).fetchone()
                    ratios[factor] = float(row[1] or 0.0) if row else 0.0
            finally:
                con.close()
            missing = [name for name, ratio in ratios.items() if ratio <= 0.0]
            low_coverage = [
                name for name, ratio in ratios.items() if 0.0 < ratio < 0.95
            ]
            present_ratio = sum(ratios.values()) / max(len(factors), 1)
            return {
                "coverage_pct": round(present_ratio * 100.0, 2) if stock_rows else 0.0,
                "required": len(factors),
                "available_factors": sum(ratio > 0 for ratio in ratios.values()),
                "rows": stock_rows,
                "missing": missing,
                "low_coverage": low_coverage,
                "factor_coverage": {
                    name: round(ratio * 100.0, 2)
                    for name, ratio in ratios.items()
                },
            }
        except Exception as exc:
            return {"coverage_pct": 0.0, "required": len(factors), "missing": factors, "error": str(exc)}

    def _market_data_coverage(self, trade_date: str, candidates: list[Dict[str, Any]]) -> Dict[str, Any]:
        from backtest.trade_calendar import TradeCalendar

        entry_date = TradeCalendar().next(trade_date)
        minute_dir = self.cache_dir / "stock" / "tick"
        auction_dir = self.cache_dir / "stock" / "auction"
        codes = {str(row.get("code") or row.get("代码") or "").split(".", 1)[0].zfill(6) for row in candidates}
        codes.discard("000000")
        minute = auction = 0
        for code in codes:
            suffix = "SH" if code.startswith(("5", "6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ"
            ts_code = f"{code}.{suffix}"
            minute += int((minute_dir / f"{ts_code}_{entry_date}.csv").exists())
            auction += int((auction_dir / f"{ts_code}_{entry_date}.json").exists())
        total = len(codes)
        return {
            "candidate_count": total,
            "entry_date": entry_date,
            "minute_count": minute,
            "minute_coverage_pct": round(minute / total * 100.0, 2) if total else 0.0,
            "auction_count": auction,
            "auction_coverage_pct": round(auction / total * 100.0, 2) if total else 0.0,
        }

    def _model_status(self, profile: Mapping[str, Any], trade_date: str) -> Dict[str, Any]:
        from core.factors.factor_library import DynamicWeightRepository

        requested = str(profile.get("weight_source") or "ic_ir").lower()
        weight_profile = str(profile.get("weight_profile") or profile.get("id") or "default")
        artifact = DynamicWeightRepository().resolve(trade_date, weight_profile)
        payload = dict(artifact.payload) if artifact is not None else {}
        candidate_model = dict(payload.get("candidate_model") or {})
        if requested in {"manual", "ic_ir"}:
            runtime_status = "rules_active"
        elif candidate_model.get("active"):
            runtime_status = "normal"
        else:
            runtime_status = "fallback_active"
        oos_months = len(candidate_model.get("monthly_rank_ic") or [])
        training_days = int(candidate_model.get("training_days") or payload.get("training_days") or 0)
        filled_samples = int(
            candidate_model.get("train_rows") or payload.get("training_rows") or 0
        )
        readiness = (
            filled_samples >= self.MIN_FILLED_SAMPLES
            and training_days >= self.MIN_TRADE_DAYS
            and oos_months >= self.MIN_OOS_MONTHS
        )
        return {
            "requested": requested,
            "runtime_status": runtime_status,
            "runtime_text": {
                "normal": "模型正常", "rules_active": "规则模式",
                "fallback_active": "规则回退可用",
            }[runtime_status],
            "artifact_date": str(getattr(artifact, "effective_date", "") or ""),
            "model_status": str(candidate_model.get("status") or ("not_requested" if requested in {"manual", "ic_ir"} else "missing")),
            "filled_samples": filled_samples,
            "training_days": training_days,
            "oos_months": oos_months,
            "publish_ready": bool(readiness and candidate_model.get("active")),
            "rank_ic": float(candidate_model.get("rank_ic") or 0.0),
            "top_decile_excess_return_pct": round(float(candidate_model.get("top_decile_excess_return") or 0.0) * 100.0, 2),
        }


__all__ = ["StrategyDiagnosticsService"]
