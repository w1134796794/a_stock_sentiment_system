"""Phase 3 screening engine over Phase 2 gold factor tables."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import yaml
from loguru import logger

from core.screening.explanations import build_screening_reasons
from core.screening.screening_models import FilterTrace, ScreeningResult


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

    def __init__(
        self,
        *,
        duckdb_path: Optional[Path] = None,
        profile_path: Optional[Path] = None,
        output_dir: Optional[Path] = None,
    ):
        from config.settings import BASE_DIR, FACTOR_DB_PATH, WEB_DATA_DIR

        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
        self.profile_path = Path(profile_path or BASE_DIR / "config" / "screening_profiles.yaml")
        self.output_dir = Path(output_dir or WEB_DATA_DIR / "screening")

    def run(
        self,
        trade_date: str,
        *,
        profile: str = "default",
        candidate_codes: Optional[Iterable[str]] = None,
        persist: bool = True,
    ) -> ScreeningResult:
        trade_date = str(trade_date)
        profiles = self.load_profiles()
        profile_name = profile if profile in profiles else "default"
        if profile_name not in profiles:
            return ScreeningResult(
                trade_date=trade_date,
                profile=profile,
                ok=False,
                message=f"screening profile 不存在: {profile}",
            )
        cfg = profiles[profile_name] or {}

        result = ScreeningResult(trade_date=trade_date, profile=profile_name)
        try:
            candidates = self.load_candidates(trade_date, candidate_codes=candidate_codes)
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
            result.message = "未读取到个股指标数据，输出空筛选结果"
            if persist:
                result.output_path = str(self.persist_result(result))
            return result

        neutral_score = _to_float((cfg.get("missing") or {}).get("neutral_score"), 50.0)
        working = candidates.copy()
        working["_screening_score"] = self._ranking_score(working, cfg, neutral_score)
        reasons: Dict[str, List[str]] = {str(row.code): [] for row in working.itertuples()}
        rejected: List[Dict[str, Any]] = []

        working = self._apply_hard_filters(working, cfg, neutral_score, reasons, rejected, result)
        result.after_hard_filter = int(len(working))

        working = self._apply_priority_filters(working, cfg, neutral_score, reasons, rejected, result)
        result.after_priority_filter = int(len(working))

        final = self._rank(working, cfg, neutral_score, reasons)
        result.final = final
        result.rejected = rejected[:200]
        result.message = f"筛选完成，输入 {result.input_count}，最终 {len(final)}"

        if persist:
            result.output_path = str(self.persist_result(result))
        return result

    def load_profiles(self) -> Dict[str, Dict[str, Any]]:
        if not self.profile_path.exists():
            logger.warning(f"[ScreeningEngine] profile 文件不存在: {self.profile_path}")
            return {}
        with self.profile_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("screening_profiles") or {}

    def load_candidates(
        self,
        trade_date: str,
        *,
        candidate_codes: Optional[Iterable[str]] = None,
    ) -> pd.DataFrame:
        if not self.duckdb_path.exists():
            return pd.DataFrame()

        import duckdb  # type: ignore

        con = duckdb.connect(str(self.duckdb_path))
        try:
            stock_wide = self._read_table(con, "factor_stock_wide", trade_date)
            value_long = self._read_table(con, "factor_value_long", trade_date)
        finally:
            con.close()
        if stock_wide.empty:
            return pd.DataFrame()

        stock_wide["code"] = stock_wide["code"].map(_normalize_code)
        base = stock_wide.drop_duplicates("code").set_index("code", drop=False).copy()

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

            market_scores = value_long[value_long["entity_type"] == "market"].copy()
            for _, row in market_scores.iterrows():
                factor_id = str(row.get("factor_id") or "")
                if factor_id:
                    base[factor_id] = _to_float(row.get("score"), 50.0)

        alias_map = {
            "stk_total_score": "total_score",
            "stk_liquidity_percentile": "liquidity_score",
            "stk_sector_resonance_score": "sector_resonance_score",
            "stk_amount_ratio_5d": "amount_ratio_score",
            "stk_vol_ratio_5d": "vol_ratio_score",
            "stk_new_high_20d": "new_high_score",
            "stk_pct_chg_1d": "pct_score",
        }
        for factor, wide_col in alias_map.items():
            if factor not in base.columns and wide_col in base.columns:
                base[factor] = base[wide_col]
            elif factor in base.columns and wide_col in base.columns:
                base[factor] = pd.to_numeric(base[factor], errors="coerce").fillna(base[wide_col])

        if candidate_codes:
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
                passed = working.sort_values("_screening_score", ascending=False).head(target_keep).copy()
                failed = working[~working["code"].isin(set(passed["code"]))]

            for row in passed.itertuples():
                reasons.setdefault(str(row.code), []).append(str(rule.get("reason") or f"通过优先过滤：{name}"))
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
    ) -> List[Dict[str, Any]]:
        if df.empty:
            return []
        ranked = df.copy()
        ranked["_screening_score"] = self._ranking_score(ranked, cfg, neutral_score)
        ranking_cfg = cfg.get("ranking") or {}
        top_n = int(ranking_cfg.get("top_n") or 10)
        ranked = ranked.sort_values(["_screening_score", "stk_total_score"], ascending=[False, False]).head(top_n)
        final: List[Dict[str, Any]] = []
        metric_cols = list((ranking_cfg.get("weights") or {}).keys())
        context_cols = [
            "pct_chg",
            "vol_ratio",
            "amount_ratio",
            "new_high_ratio",
            "liquidity_score",
            "sector_resonance_score",
        ]
        for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
            code = str(row.get("code") or "")
            metrics = {col: _to_float(row.get(col), 50.0) for col in metric_cols}
            context = {
                col: _to_float(row.get(col), 0.0)
                for col in context_cols
                if col in ranked.columns
            }
            score = round(_to_float(row.get("_screening_score"), 0.0), 4)
            base_reasons = reasons.get(code, [])[:8]
            final.append({
                "code": code,
                "ts_code": str(row.get("ts_code") or ""),
                "name": str(row.get("name") or ""),
                "score": score,
                "rank": rank,
                "gold_rank": int(_to_float(row.get("rank"), rank)),
                "reasons": build_screening_reasons(
                    metrics=metrics,
                    context=context,
                    score=score,
                    rank=rank,
                    base_reasons=base_reasons,
                ),
                "rule_reasons": base_reasons,
                "metrics": metrics,
                "context": context,
            })
        return final

    def _ranking_score(self, df: pd.DataFrame, cfg: Dict[str, Any], neutral_score: float) -> pd.Series:
        weights = (cfg.get("ranking") or {}).get("weights") or {"stk_total_score": 1.0}
        total_weight = sum(max(float(w), 0.0) for w in weights.values())
        if total_weight <= 0:
            return pd.Series([neutral_score] * len(df), index=df.index)
        score = pd.Series([0.0] * len(df), index=df.index, dtype=float)
        for factor, weight in weights.items():
            weight = max(float(weight), 0.0)
            values = pd.to_numeric(df.get(factor, neutral_score), errors="coerce").fillna(neutral_score)
            score += values * weight
        return score / total_weight

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
