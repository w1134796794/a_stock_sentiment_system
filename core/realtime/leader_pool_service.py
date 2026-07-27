"""Leader pool and intraday strength views built on current factor screening output."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.realtime.entry_signal_service import classify_entry_mode, entry_mode_text
from core.realtime.models import normalize_stock_code
from core.signals.confidence_service import ConfidenceService
from core.signals.leader_outcome import LeaderOutcomeTracker
from core.utils.price_limit import (
    get_price_limit_pct_points,
    limit_progress,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    except Exception:
        return default


def _score_between(value: Any, low: float, high: float) -> float:
    val = _to_float(value)
    if high <= low:
        return 0.0
    return max(0.0, min(100.0, (val - low) / (high - low) * 100.0))


def _weighted_score(parts: Iterable[tuple[Any, float]], default: float = 50.0) -> float:
    total = 0.0
    weight_sum = 0.0
    for value, weight in parts:
        w = max(_to_float(weight), 0.0)
        if w <= 0:
            continue
        total += _to_float(value, default) * w
        weight_sum += w
    if weight_sum <= 0:
        return default
    return max(0.0, min(100.0, total / weight_sum))


def _amount_health_score(value: Any) -> float:
    """Prefer confirmed but not exhausted turnover without a flat 100-point plateau."""
    ratio = _to_float(value, 0.0)
    if ratio <= 0:
        return 0.0
    if ratio < 0.4:
        return ratio / 0.4 * 15.0
    if ratio < 0.8:
        return 15.0 + (ratio - 0.4) / 0.4 * 60.0
    if ratio < 1.15:
        return 75.0 + (ratio - 0.8) / 0.35 * 25.0
    if ratio <= 1.5:
        return 100.0 - (ratio - 1.15) / 0.35 * 15.0
    if ratio <= 2.2:
        return 85.0 - (ratio - 1.5) / 0.7 * 40.0
    if ratio <= 3.0:
        return 45.0 - (ratio - 2.2) / 0.8 * 30.0
    return max(0.0, 15.0 - (ratio - 3.0) * 5.0)


def _pct_text(value: Any) -> str:
    val = _to_float(value)
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


class LeaderPoolService:
    """Build a leader pool from independent market-status evidence.

    Candidate rank is deliberately excluded from leader scoring and classification.
    It remains visible only as an observation-order field. A historical leader must
    have passed the same evidence gates on its source date.
    """

    MAX_CORE_LEADERS = 3
    MAX_SECTOR_LEADERS = 8
    MAX_BOARD_LEADERS = 8
    MAX_TREND_LEADERS = 8
    CORE_SCORE = 72.0
    SECTOR_SCORE = 64.0
    EMOTION_SCORE = 66.0
    BOARD_SCORE = 66.0
    TREND_SCORE = 66.0
    MAX_EMOTION_LEADERS = 4

    def __init__(
        self, *, screening_dir: Optional[Path] = None, duckdb_path: Optional[Path] = None,
    ) -> None:
        from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

        self.screening_dir = Path(screening_dir or WEB_DATA_DIR / "screening")
        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH) if (duckdb_path or screening_dir is None) else None
        self.outcome_tracker = LeaderOutcomeTracker(self.duckdb_path) if self.duckdb_path is not None else None
        self._stats_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._universe_cache: Dict[str, List[Dict[str, Any]]] = {}

    def build_pool(
        self,
        trade_date: Optional[str] = None,
        *,
        lookback: int = 10,
        limit: int = 30,
    ) -> Dict[str, Any]:
        dates = self._recent_dates(trade_date, lookback)
        target = str(trade_date or (dates[-1] if dates else ""))
        history = [(date, self._load_screening(date)) for date in dates]
        by_code: Dict[str, Dict[str, Any]] = {}
        leader_history: Dict[str, List[Dict[str, Any]]] = {}
        candidate_counts: Dict[str, int] = {}

        for date, data in history:
            daily_snapshots: List[Dict[str, Any]] = []
            seen_codes = set()
            source_rows = self._source_rows(date, data)
            for item in source_rows:
                code = normalize_stock_code(item.get("code") or item.get("stock_code") or "", add_suffix=False)
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                bucket = by_code.setdefault(code, {
                    "code": code,
                    "name": item.get("name") or "",
                    "appearances": 0,
                    "dates": [],
                    "scores": [],
                    "ranks": [],
                    "latest": None,
                    "last_item": None,
                    "last_date": "",
                    "snapshots": {},
                    "leader_events": [],
                })
                bucket["name"] = item.get("name") or bucket.get("name") or ""
                bucket["appearances"] += 1
                bucket["dates"].append(date)
                bucket["scores"].append(_to_float(item.get("score")))
                item_rank = int(_to_float(item.get("rank"), 999))
                bucket["ranks"].append(item_rank)
                bucket["last_item"] = item
                bucket["last_date"] = date
                if date == target:
                    bucket["latest"] = item
                candidate_counts[code] = candidate_counts.get(code, 0) + 1
                snapshot = self._leader_snapshot(
                    item,
                    date,
                    candidate_appearances=candidate_counts[code],
                    prior_events=leader_history.get(code) or [],
                )
                bucket["snapshots"][date] = snapshot
                daily_snapshots.append(snapshot)

            core_candidates = [row for row in daily_snapshots if self._qualifies_core(row)]
            core_candidates.sort(key=lambda row: (-_to_float(row.get("leader_score")), row.get("code") or ""))
            core_codes = {row["code"] for row in core_candidates[: self.MAX_CORE_LEADERS]}
            board_candidates = [
                row for row in daily_snapshots
                if row["code"] not in core_codes and self._qualifies_board_leader(row)
            ]
            board_candidates.sort(
                key=lambda row: (
                    -int(row.get("board_height") or 0),
                    -_to_float(row.get("board_role_score")),
                    row.get("code") or "",
                )
            )
            board_codes = {
                row["code"] for row in board_candidates[: self.MAX_BOARD_LEADERS]
            }
            sector_candidates = [
                row for row in daily_snapshots
                if row["code"] not in core_codes
                and row["code"] not in board_codes
                and self._qualifies_sector_leader(row)
            ]
            sector_candidates.sort(key=lambda row: (-_to_float(row.get("leader_score")), row.get("code") or ""))
            sector_codes = set()
            seen_sectors = set()
            for row in sector_candidates:
                sector_key = str(row.get("primary_sector") or row.get("code") or "")
                if sector_key in seen_sectors:
                    continue
                seen_sectors.add(sector_key)
                sector_codes.add(row["code"])
                if len(sector_codes) >= self.MAX_SECTOR_LEADERS:
                    break
            emotion_candidates = [
                row for row in daily_snapshots
                if row["code"] not in core_codes
                and row["code"] not in board_codes
                and row["code"] not in sector_codes
                and self._qualifies_emotion_leader(row)
            ]
            emotion_candidates.sort(
                key=lambda row: (-_to_float(row.get("emotion_role_score")), row.get("code") or "")
            )
            emotion_codes = {
                row["code"] for row in emotion_candidates[: self.MAX_EMOTION_LEADERS]
            }
            trend_candidates = [
                row for row in daily_snapshots
                if row["code"] not in core_codes
                and row["code"] not in board_codes
                and row["code"] not in sector_codes
                and row["code"] not in emotion_codes
                and self._qualifies_trend_leader(row)
            ]
            trend_candidates.sort(
                key=lambda row: (
                    -_to_float(row.get("trend_role_score")),
                    row.get("code") or "",
                )
            )
            trend_codes = set()
            trend_sectors = set()
            for row in trend_candidates:
                sector_key = str(row.get("primary_sector") or row.get("code") or "")
                if sector_key in trend_sectors:
                    continue
                trend_sectors.add(sector_key)
                trend_codes.add(row["code"])
                if len(trend_codes) >= self.MAX_TREND_LEADERS:
                    break
            for snapshot in daily_snapshots:
                pool_type = ""
                if snapshot["code"] in core_codes:
                    pool_type = "核心龙头"
                elif snapshot["code"] in board_codes:
                    pool_type = "连板龙头"
                elif snapshot["code"] in sector_codes:
                    pool_type = "板块龙头"
                elif snapshot["code"] in emotion_codes:
                    pool_type = "情绪龙头"
                elif snapshot["code"] in trend_codes:
                    pool_type = "趋势龙头"
                if not pool_type:
                    continue
                event = {
                    **snapshot,
                    "pool_type": pool_type,
                    "primary_role": self._primary_role(snapshot, pool_type),
                    "leader_roles": self._role_labels(snapshot, pool_type),
                }
                leader_history.setdefault(snapshot["code"], []).append(event)
                by_code[snapshot["code"]]["leader_events"].append(event)

        rows = [self._build_pool_row(raw, target, dates) for raw in by_code.values()]
        rows = [row for row in rows if row is not None]
        rows.sort(
            key=lambda row: (
                row["pool_type_order"],
                int(row.get("leader_age_days") or 0),
                -_to_float(row.get("leader_score")),
                int(row.get("latest_rank") or 999),
            )
        )
        selected = rows[: max(int(limit or 30), 1)]
        for idx, row in enumerate(selected, start=1):
            row["pool_rank"] = idx
            row.pop("pool_type_order", None)

        counts: Dict[str, int] = {}
        role_counts: Dict[str, int] = {}
        for row in selected:
            counts[row["pool_type"]] = counts.get(row["pool_type"], 0) + 1
            for role in row.get("leader_roles") or []:
                role_counts[role] = role_counts.get(role, 0) + 1

        return {
            "ok": bool(selected),
            "trade_date": target,
            "lookback": len(dates),
            "dates": dates,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "counts": counts,
            "role_counts": role_counts,
            "rows": selected,
        }

    @staticmethod
    def _metric(item: Dict[str, Any], factor: str, fallback: float = 50.0) -> float:
        metrics = item.get("metrics") or {}
        context = item.get("context") or {}
        if factor in metrics:
            return _to_float(metrics.get(factor), fallback)
        if factor in context:
            return _to_float(context.get(factor), fallback)
        return _to_float(item.get(factor), fallback)

    def _leader_snapshot(
        self,
        item: Dict[str, Any],
        date: str,
        *,
        candidate_appearances: int,
        prior_events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        metrics = item.get("metrics") or {}
        context = item.get("context") or {}
        code = normalize_stock_code(item.get("code") or item.get("stock_code") or "", add_suffix=False)
        name = str(item.get("name") or "")

        sector_resonance = self._metric(item, "stk_sector_resonance_score", _to_float(context.get("sector_resonance_score"), 50.0))
        sector_mainline = self._metric(item, "stk_sector_mainline_score", sector_resonance)
        sector_persistence = self._metric(item, "stk_sector_persistence_score", sector_resonance)
        sector_status = _weighted_score([
            (sector_mainline, 0.45),
            (sector_persistence, 0.30),
            (sector_resonance, 0.25),
        ])

        pct_chg = _to_float(context.get("pct_chg"), _to_float(item.get("pct_chg"), 0.0))
        limit_pct = get_price_limit_pct_points(code, name) or 10.0
        progress = _to_float(context.get("limit_progress"), limit_progress(pct_chg, code, name))
        limit_quality = max(0.0, min(100.0, progress * 100.0))
        board_score = self._metric(item, "stk_board_position", 50.0)
        board_height = _to_float(context.get("board_height"), _to_float(item.get("board_height"), 0.0))
        seal_quality = self._metric(item, "stk_seal_time_quality", 50.0)
        kpl_score = self._metric(item, "stk_kpl_leader_quality", 50.0)
        attention_score = self._metric(item, "stk_attention_consensus", 50.0)
        behavior_attention = self._metric(item, "stk_behavior_attention", 50.0)
        behavior_acceleration = self._metric(item, "stk_behavior_acceleration", 50.0)
        behavior_divergence = self._metric(item, "stk_behavior_divergence", 50.0)
        behavior_repair = self._metric(item, "stk_behavior_repair", 50.0)
        behavior_decay = self._metric(item, "stk_behavior_decay", 50.0)
        market_status = _weighted_score([
            (board_score, 0.25),
            (seal_quality, 0.15),
            (kpl_score, 0.15),
            (limit_quality, 0.15),
            (attention_score, 0.10),
            (behavior_acceleration, 0.10),
            (behavior_repair, 0.10),
        ])

        prior_count = len(prior_events)
        if prior_count >= 3:
            continuity = 100.0
        elif prior_count == 2:
            continuity = 85.0
        elif prior_count == 1:
            continuity = 70.0
        else:
            continuity = 40.0
        continuity = min(100.0, continuity + min(max(candidate_appearances - 1, 0), 2) * 5.0)
        if board_height >= 2 or kpl_score >= 75:
            continuity = max(continuity, 65.0)

        lhb_present = bool(_to_float(context.get("lhb_present"), _to_float(item.get("lhb_present"), 0.0)))
        lhb_score = self._metric(item, "stk_lhb_composite_score", 50.0)
        institution_score = self._metric(item, "stk_lhb_institution_score", 50.0)
        capital_flow_score = self._metric(item, "stk_capital_flow_consensus", 50.0)
        capital_recognition = _weighted_score([
            (lhb_score if lhb_present else 50.0, 0.40),
            (institution_score if lhb_present else 50.0, 0.20),
            (capital_flow_score, 0.40),
        ])

        amount_ratio = _to_float(context.get("amount_ratio"), 1.0)
        amount_health = _amount_health_score(amount_ratio)
        lhb_crowding_safety = self._metric(item, "stk_lhb_crowding_risk", 100.0)
        event_risk_safety = self._metric(item, "stk_block_trade_risk", 100.0)
        attention_safety = self._metric(item, "stk_attention_crowding_risk", 100.0)
        safety = _weighted_score([
            (amount_health, 0.35),
            (event_risk_safety, 0.25),
            (lhb_crowding_safety, 0.20),
            (attention_safety, 0.10),
            (100.0 - behavior_decay, 0.10),
        ])

        evidence = {
            "板块地位": sector_status >= 65.0,
            "市场辨识度": market_status >= 65.0,
            "持续性": continuity >= 65.0,
            "资金认可": capital_recognition >= 60.0,
        }
        identity_evidence = (
            board_height >= 1
            or progress >= 0.95
            or kpl_score >= 65.0
            or attention_score >= 75.0
            or (progress >= 0.60 and sector_status >= 70.0)
        )
        severe_risk = safety < 40.0 or amount_ratio < 0.5 or amount_ratio > 3.0
        leader_score = _weighted_score([
            (sector_status, 0.30),
            (market_status, 0.25),
            (continuity, 0.20),
            (capital_recognition, 0.15),
            (safety, 0.10),
        ])
        relative_strength = self._metric(item, "stk_relative_strength_sector", 50.0)
        short_term_role_score = _weighted_score([
            (market_status, 0.30),
            (behavior_acceleration, 0.25),
            (seal_quality, 0.20),
            (limit_quality, 0.15),
            (safety, 0.10),
        ])
        sector_role_score = _weighted_score([
            (sector_status, 0.40),
            (sector_resonance, 0.20),
            (relative_strength, 0.15),
            (continuity, 0.15),
            (leader_score, 0.10),
        ])
        emotion_role_score = _weighted_score([
            (attention_score, 0.28),
            (behavior_attention, 0.24),
            (kpl_score, 0.18),
            (board_score, 0.15),
            (behavior_acceleration, 0.15),
        ])
        board_role_score = _weighted_score([
            (min(board_height / 4.0 * 100.0, 100.0), 0.30),
            (board_score, 0.20),
            (seal_quality, 0.18),
            (limit_quality, 0.17),
            (behavior_acceleration, 0.15),
        ])
        trend_role_score = _weighted_score([
            (relative_strength, 0.25),
            (self._metric(item, "stk_new_high_20d", 50.0), 0.20),
            (sector_persistence, 0.20),
            (self._metric(item, "tech_score", 50.0), 0.20),
            (continuity, 0.15),
        ])

        source_rank_value = int(_to_float(item.get("rank"), 999))
        sector_names = str(item.get("resonance_sectors") or "")
        return {
            "code": code,
            "name": name,
            "date": str(date),
            "source_rank": source_rank_value if source_rank_value < 999 else None,
            "resonance_sectors": sector_names,
            "primary_sector": sector_names.split(",", 1)[0].strip(),
            "candidate_score": _to_float(item.get("score"), 0.0),
            "leader_score": round(leader_score, 4),
            "short_term_role_score": round(short_term_role_score, 4),
            "board_role_score": round(board_role_score, 4),
            "trend_role_score": round(trend_role_score, 4),
            "sector_role_score": round(sector_role_score, 4),
            "emotion_role_score": round(emotion_role_score, 4),
            "sector_status_score": round(sector_status, 4),
            "market_status_score": round(market_status, 4),
            "continuity_score": round(continuity, 4),
            "capital_recognition_score": round(capital_recognition, 4),
            "safety_score": round(safety, 4),
            "evidence": evidence,
            "evidence_count": sum(bool(value) for value in evidence.values()),
            "identity_evidence": identity_evidence,
            "severe_risk": severe_risk,
            "pct_chg": round(pct_chg, 4),
            "limit_pct": round(limit_pct, 4),
            "limit_progress": round(progress, 6),
            "amount_ratio": round(amount_ratio, 4),
            "vol_ratio": round(_to_float(context.get("vol_ratio"), 1.0), 4),
            "tech_score": round(self._metric(item, "tech_score", 50.0), 4),
            "liquidity_score": round(self._metric(item, "stk_liquidity_percentile", 50.0), 4),
            "new_high_score": round(self._metric(item, "stk_new_high_20d", 50.0), 4),
            "sector_mainline_score": round(sector_mainline, 4),
            "sector_persistence_score": round(sector_persistence, 4),
            "sector_resonance_score": round(sector_resonance, 4),
            "board_height": int(board_height),
            "board_score": round(board_score, 4),
            "seal_quality_score": round(seal_quality, 4),
            "lhb_present": lhb_present,
            "lhb_score": round(lhb_score, 4),
            "lhb_sector_score": round(self._metric(item, "stk_lhb_sector_resonance", 50.0), 4),
            "lhb_crowding_safety": round(lhb_crowding_safety, 4),
            "capital_flow_score": round(capital_flow_score, 4),
            "attention_score": round(attention_score, 4),
            "kpl_leader_score": round(kpl_score, 4),
            "event_risk_safety": round(event_risk_safety, 4),
            "behavior_state": str(item.get("behavior_state") or ""),
            "behavior_state_label": str(item.get("behavior_state_label") or ""),
            "behavior_attention_score": round(behavior_attention, 4),
            "behavior_acceleration_score": round(behavior_acceleration, 4),
            "behavior_divergence_score": round(behavior_divergence, 4),
            "behavior_repair_score": round(behavior_repair, 4),
            "behavior_decay_score": round(behavior_decay, 4),
            "relative_strength_sector_score": round(relative_strength, 4),
            "candidate_reasons": item.get("reasons") or [],
            "lhb_signal_date": str((item.get("lhb") or {}).get("signal_date") or ""),
            "lhb_effective_date": str((item.get("lhb") or {}).get("effective_date") or ""),
        }

    def _qualifies_core(self, row: Dict[str, Any]) -> bool:
        return bool(
            not row.get("severe_risk")
            and row.get("identity_evidence")
            and _to_float(row.get("leader_score")) >= self.CORE_SCORE
            and _to_float(row.get("sector_status_score")) >= 62.0
            and _to_float(row.get("market_status_score")) >= 60.0
            and int(row.get("evidence_count") or 0) >= 3
        )

    def _qualifies_sector_leader(self, row: Dict[str, Any]) -> bool:
        return bool(
            not row.get("severe_risk")
            and row.get("identity_evidence")
            and _to_float(row.get("leader_score")) >= self.SECTOR_SCORE
            and _to_float(row.get("sector_status_score")) >= 62.0
            and int(row.get("evidence_count") or 0) >= 2
        )

    def _qualifies_emotion_leader(self, row: Dict[str, Any]) -> bool:
        return bool(
            not row.get("severe_risk")
            and row.get("identity_evidence")
            and _to_float(row.get("emotion_role_score")) >= self.EMOTION_SCORE
            and _to_float(row.get("market_status_score")) >= 58.0
        )

    def _qualifies_board_leader(self, row: Dict[str, Any]) -> bool:
        return bool(
            not row.get("severe_risk")
            and row.get("identity_evidence")
            and int(row.get("board_height") or 0) >= 2
            and _to_float(row.get("board_role_score")) >= self.BOARD_SCORE
        )

    def _qualifies_trend_leader(self, row: Dict[str, Any]) -> bool:
        return bool(
            not row.get("severe_risk")
            and row.get("identity_evidence")
            and _to_float(row.get("trend_role_score")) >= self.TREND_SCORE
            and _to_float(row.get("relative_strength_sector_score")) >= 58.0
            and _to_float(row.get("sector_status_score")) >= 55.0
        )

    @staticmethod
    def _role_labels(row: Dict[str, Any], pool_type: str = "") -> List[str]:
        """Roles are evidence tags, intentionally not mutually exclusive."""
        labels: List[str] = []
        if (
            int(row.get("board_height") or 0) >= 2
            and _to_float(row.get("board_role_score")) >= 64.0
        ):
            labels.append("连板龙头")
        if _to_float(row.get("trend_role_score")) >= 66.0:
            labels.append("趋势龙头")
        if _to_float(row.get("short_term_role_score")) >= 67.0:
            labels.append("短线龙头")
        if _to_float(row.get("sector_role_score")) >= 67.0:
            labels.append("板块龙头")
        if _to_float(row.get("emotion_role_score")) >= 68.0:
            labels.append("情绪龙头")
        if pool_type == "核心龙头" and "短线龙头" not in labels:
            labels.insert(0, "短线龙头")
        if pool_type == "板块龙头" and "板块龙头" not in labels:
            labels.append("板块龙头")
        if pool_type == "情绪龙头" and "情绪龙头" not in labels:
            labels.append("情绪龙头")
        if pool_type == "连板龙头" and "连板龙头" not in labels:
            labels.insert(0, "连板龙头")
        if pool_type == "趋势龙头" and "趋势龙头" not in labels:
            labels.insert(0, "趋势龙头")
        return labels

    @staticmethod
    def _primary_role(row: Dict[str, Any], pool_type: str = "") -> str:
        if pool_type == "核心龙头":
            return "短线龙头"
        if pool_type in {"连板龙头", "趋势龙头", "板块龙头", "情绪龙头"}:
            return pool_type
        scores = {
            "连板龙头": _to_float(row.get("board_role_score")),
            "趋势龙头": _to_float(row.get("trend_role_score")),
            "短线龙头": _to_float(row.get("short_term_role_score")),
            "板块龙头": _to_float(row.get("sector_role_score")),
            "情绪龙头": _to_float(row.get("emotion_role_score")),
        }
        return max(scores, key=scores.get)

    def _build_pool_row(self, raw: Dict[str, Any], target: str, dates: List[str]) -> Optional[Dict[str, Any]]:
        events = list(raw.get("leader_events") or [])
        if not events:
            return None

        snapshots = raw.get("snapshots") or {}
        current = snapshots.get(target)
        current_event = next((event for event in reversed(events) if event.get("date") == target), None)
        last_event = events[-1]
        source = current or last_event
        source_date = str(source.get("date") or "")
        latest_rank = current.get("source_rank") if current else None
        source_rank = source.get("source_rank")
        latest_score = _to_float(source.get("candidate_score"), max(raw.get("scores") or [0.0]))
        best_rank = min(raw.get("ranks") or [999])
        avg_score = sum(raw.get("scores") or [0.0]) / max(len(raw.get("scores") or []), 1)
        code = raw.get("code") or ""
        name = raw.get("name") or source.get("name") or ""
        leader_dates = [str(event.get("date") or "") for event in events]
        last_leader_date = str(last_event.get("date") or "")
        target_index = dates.index(target) if target in dates else max(len(dates) - 1, 0)
        leader_index = dates.index(last_leader_date) if last_leader_date in dates else target_index
        leader_age_days = max(target_index - leader_index, 0)
        if current_event:
            pool_type = str(current_event.get("pool_type") or "板块龙头")
            order = {
                "核心龙头": 0,
                "连板龙头": 1,
                "板块龙头": 2,
                "情绪龙头": 3,
                "趋势龙头": 4,
            }.get(pool_type, 5)
            leader_time_label = {
                "核心龙头": "当日核心龙头",
                "连板龙头": "当日连板龙头",
                "板块龙头": "当日板块龙头",
                "情绪龙头": "当日情绪龙头",
                "趋势龙头": "当日趋势龙头",
            }.get(pool_type, "当日龙头")
            prior_events = [event for event in events if str(event.get("date") or "") < target]
            divergence = _to_float(source.get("behavior_divergence_score"), 50.0)
            repair = _to_float(source.get("behavior_repair_score"), 50.0)
            decay = _to_float(source.get("behavior_decay_score"), 50.0)
            if decay >= 68.0 and decay > repair:
                lifecycle_state = "衰退龙头"
            elif divergence >= 62.0 and divergence > repair:
                lifecycle_state = "分歧龙头"
            else:
                lifecycle_state = "确认龙头" if prior_events else "萌芽龙头"
        else:
            pool_type = "近期龙头"
            order = 6
            leader_time_label = "上一交易日龙头" if leader_age_days == 1 else f"{leader_age_days}个交易日前龙头"
            current_sector = _to_float((current or {}).get("sector_status_score"), 0.0)
            current_safety = _to_float((current or {}).get("safety_score"), 0.0)
            behavior_repair = _to_float((current or {}).get("behavior_repair_score"), 0.0)
            behavior_decay = _to_float((current or {}).get("behavior_decay_score"), 50.0)
            lifecycle_state = (
                "分歧龙头"
                if current and current_sector >= 55.0 and current_safety >= 40.0
                and behavior_repair >= behavior_decay
                else "衰退龙头"
            )

        role_event = current_event or last_event
        leader_roles = list(role_event.get("leader_roles") or [])
        if not leader_roles:
            leader_roles = self._role_labels(source, pool_type)
        primary_role = str(
            (current_event or last_event).get("primary_role")
            or self._primary_role(source, pool_type)
        )
        reasons = self._reasons(pool_type, source_rank, source_date, target, last_event, source)
        if leader_roles:
            reasons.insert(0, f"角色标签：{' / '.join(leader_roles)}")
        stats_key = (target, lifecycle_state)
        if stats_key not in self._stats_cache:
            self._stats_cache[stats_key] = self.outcome_tracker.stats(
                lifecycle_state, as_of_date=target,
            ) if self.outcome_tracker is not None else {}
        stats = self._stats_cache[stats_key]
        leader_confidence = ConfidenceService.assess(
            calibrated_probability=stats.get("success_probability", 0.5),
            expected_return=stats.get("expected_return", 0.0),
            stop_probability=stats.get("stop_probability", 0.5),
            average_mfe=stats.get("average_mfe", 0.0),
            average_mae=stats.get("average_mae", 0.0),
            sample_size=stats.get("sample_size", 0),
            data_completeness=min(int(source.get("evidence_count") or 0) / 4.0, 1.0),
            regime_match=_to_float(source.get("sector_status_score"), 50.0) / 100.0,
            tradability=_to_float(source.get("safety_score"), 50.0) / 100.0,
            model_type="leader_lifecycle_oos" if stats else "leader_lifecycle_pending_oos_calibration",
            as_of_date=target,
        )
        return {
            "code": code,
            "name": name,
            "pool_type": pool_type,
            "pool_type_order": order,
            "primary_role": primary_role,
            "leader_roles": leader_roles,
            "role_scores": {
                "连板龙头": round(_to_float(source.get("board_role_score")), 2),
                "趋势龙头": round(_to_float(source.get("trend_role_score")), 2),
                "短线龙头": round(_to_float(source.get("short_term_role_score")), 2),
                "板块龙头": round(_to_float(source.get("sector_role_score")), 2),
                "情绪龙头": round(_to_float(source.get("emotion_role_score")), 2),
            },
            "leader_score": round(_to_float(source.get("leader_score")), 2),
            "latest_rank": latest_rank,
            "source_rank": source_rank,
            "source_date": source_date,
            "is_today_candidate": current is not None,
            "best_rank": best_rank if best_rank != 999 else None,
            "avg_score": round(avg_score, 2),
            "appearances": int(raw.get("appearances") or 0),
            "active_dates": raw.get("dates") or [],
            "leader_dates": leader_dates,
            "last_leader_date": last_leader_date,
            "leader_age_days": leader_age_days,
            "leader_time_label": leader_time_label,
            "lifecycle_state": lifecycle_state,
            "lifecycle_reason": self._lifecycle_reason(lifecycle_state, source, last_event),
            "resonance_sectors": str(source.get("resonance_sectors") or ""),
            "primary_sector": str(source.get("primary_sector") or ""),
            "behavior_state": str(source.get("behavior_state") or ""),
            "behavior_state_label": str(source.get("behavior_state_label") or ""),
            "behavior_attention_score": round(_to_float(source.get("behavior_attention_score")), 2),
            "behavior_acceleration_score": round(_to_float(source.get("behavior_acceleration_score")), 2),
            "behavior_divergence_score": round(_to_float(source.get("behavior_divergence_score")), 2),
            "behavior_repair_score": round(_to_float(source.get("behavior_repair_score")), 2),
            "behavior_decay_score": round(_to_float(source.get("behavior_decay_score")), 2),
            "latest_score": round(latest_score, 2),
            "last_leader_type": str(last_event.get("pool_type") or ""),
            "pct_chg": round(_to_float(source.get("pct_chg")), 2),
            "limit_pct": round(_to_float(source.get("limit_pct"), 10.0), 2),
            "limit_progress": round(_to_float(source.get("limit_progress")), 4),
            "amount_ratio": round(_to_float(source.get("amount_ratio"), 1.0), 2),
            "vol_ratio": round(_to_float(source.get("vol_ratio"), 1.0), 2),
            "tech_score": round(_to_float(source.get("tech_score"), 50.0), 2),
            "liquidity_score": round(_to_float(source.get("liquidity_score"), 50.0), 2),
            "new_high_score": round(_to_float(source.get("new_high_score"), 50.0), 2),
            "sector_status_score": round(_to_float(source.get("sector_status_score"), 50.0), 2),
            "market_status_score": round(_to_float(source.get("market_status_score"), 50.0), 2),
            "continuity_score": round(_to_float(source.get("continuity_score"), 50.0), 2),
            "capital_recognition_score": round(_to_float(source.get("capital_recognition_score"), 50.0), 2),
            "safety_score": round(_to_float(source.get("safety_score"), 50.0), 2),
            "evidence": source.get("evidence") or {},
            "evidence_count": int(source.get("evidence_count") or 0),
            "lhb_present": bool(source.get("lhb_present")),
            "lhb_score": round(_to_float(source.get("lhb_score"), 50.0), 2),
            "lhb_sector_score": round(_to_float(source.get("lhb_sector_score"), 50.0), 2),
            "lhb_crowding_safety": round(_to_float(source.get("lhb_crowding_safety"), 100.0), 2),
            "capital_flow_score": round(_to_float(source.get("capital_flow_score"), 50.0), 2),
            "attention_score": round(_to_float(source.get("attention_score"), 50.0), 2),
            "kpl_leader_score": round(_to_float(source.get("kpl_leader_score"), 50.0), 2),
            "event_risk_safety": round(_to_float(source.get("event_risk_safety"), 100.0), 2),
            "leader_confidence": leader_confidence,
            "confidence_grade": leader_confidence["confidence_grade"],
            "lhb_signal_date": str(source.get("lhb_signal_date") or ""),
            "lhb_effective_date": str(source.get("lhb_effective_date") or ""),
            "action": "按弱转强、强势延续或高开加速的分钟条件确认",
            "reasons": reasons,
            "candidate_reasons": source.get("candidate_reasons") or [],
        }

    @staticmethod
    def _lifecycle_reason(state: str, source: Dict[str, Any], last_event: Dict[str, Any]) -> str:
        if state == "萌芽龙头":
            return "首次通过板块地位、市场辨识度和资金证据门槛"
        if state == "确认龙头":
            return "连续交易日保持独立龙头证据"
        if state == "分歧龙头":
            return "当日身份减弱，但板块承接和接力安全尚未失效"
        return f"最近龙头身份停留在{last_event.get('date') or '--'}，当前证据不足"

    def _source_rows(self, date: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Merge screening rows with the full limit-up/strong-sector universe."""
        preferred = list(data.get("candidate_pool") or data.get("final") or [])
        rows = preferred + self._load_market_universe(date)
        output: List[Dict[str, Any]] = []
        seen = set()
        for item in rows:
            code = normalize_stock_code(item.get("code") or item.get("stock_code") or "", add_suffix=False)
            if not code or code in seen:
                continue
            seen.add(code)
            output.append(item)
        return output

    def _load_market_universe(self, date: str) -> List[Dict[str, Any]]:
        if str(date) in self._universe_cache:
            return self._universe_cache[str(date)]
        if self.duckdb_path is None or not self.duckdb_path.exists():
            return []
        try:
            import duckdb  # type: ignore

            con = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                frame = con.execute(
                    "SELECT * FROM factor_stock_wide WHERE CAST(trade_date AS VARCHAR)=? "
                    "AND (limit_progress >= 0.60 OR board_height >= 1 "
                    "OR (sector_mainline_score >= 70 AND pct_chg >= 3)) "
                    "ORDER BY limit_progress DESC, sector_mainline_score DESC, total_score DESC LIMIT 500",
                    [str(date)],
                ).fetchdf()
            finally:
                con.close()
        except Exception:
            return []
        output: List[Dict[str, Any]] = []
        for row in frame.to_dict("records"):
            output.append({
                "code": row.get("code"), "name": row.get("name"),
                "score": row.get("enhanced_total_score") or row.get("total_score"),
                "rank": row.get("rank"), "resonance_sectors": row.get("resonance_sectors") or "",
                "source_universe": "全量涨停与强板块成分",
                "metrics": {
                    "tech_score": row.get("tech_score"),
                    "stk_liquidity_percentile": row.get("liquidity_score"),
                    "stk_new_high_20d": _to_float(row.get("new_high_ratio"), 0.0) * 100.0,
                    "stk_sector_mainline_score": row.get("sector_mainline_score"),
                    "stk_sector_persistence_score": row.get("sector_persistence_score"),
                    "stk_sector_resonance_score": row.get("sector_resonance_score"),
                    "stk_board_position": row.get("board_score"),
                    "stk_seal_time_quality": row.get("seal_time_score"),
                    "stk_kpl_leader_quality": row.get("leader_quality_score"),
                    "stk_attention_consensus": row.get("attention_score"),
                    "stk_capital_flow_consensus": row.get("capital_flow_consensus_score"),
                    "stk_lhb_composite_score": row.get("lhb_composite_score"),
                    "stk_lhb_institution_score": row.get("institution_net_buy_score"),
                    "stk_lhb_crowding_risk": 100.0 - _to_float(row.get("crowding_penalty_score")),
                    "stk_attention_crowding_risk": 100.0 - _to_float(row.get("attention_crowding_penalty")),
                    "stk_block_trade_risk": 100.0 - _to_float(row.get("event_risk_score")),
                },
                "context": {
                    "pct_chg": row.get("pct_chg"), "limit_progress": row.get("limit_progress"),
                    "amount_ratio": row.get("amount_ratio"), "vol_ratio": row.get("vol_ratio"),
                    "board_height": row.get("board_height"), "lhb_present": row.get("lhb_present"),
                },
            })
        self._universe_cache[str(date)] = output
        return output

    @staticmethod
    def _reasons(
        pool_type: str,
        source_rank: Optional[int],
        source_date: str,
        target: str,
        last_event: Dict[str, Any],
        source: Dict[str, Any],
    ) -> List[str]:
        reasons: List[str] = []
        if source_rank:
            prefix = "当日" if source_date == target else source_date
            reasons.append(f"{prefix}候选排名第 {source_rank}，名次仅作观察顺序")
        reasons.append(
            f"独立龙头评分 {_to_float(source.get('leader_score')):.1f}，归类为{pool_type}"
        )
        dimensions = [
            ("板块地位", source.get("sector_status_score")),
            ("市场辨识度", source.get("market_status_score")),
            ("持续性", source.get("continuity_score")),
            ("资金认可", source.get("capital_recognition_score")),
            ("接力安全", source.get("safety_score")),
        ]
        dimensions.sort(key=lambda item: _to_float(item[1]), reverse=True)
        reasons.append("优势维度：" + "、".join(f"{name}{_to_float(score):.0f}" for name, score in dimensions[:3]))
        evidence = [name for name, passed in (source.get("evidence") or {}).items() if passed]
        if evidence:
            reasons.append("身份依据：" + "、".join(evidence))
        progress = _to_float(source.get("limit_progress"), 0.0)
        limit_pct = _to_float(source.get("limit_pct"), 10.0)
        pct_chg = _to_float(source.get("pct_chg"), 0.0)
        if progress >= 0.95:
            reasons.append(f"接近{limit_pct:.0f}cm涨停，涨停进度 {progress * 100:.0f}%")
        elif progress >= 0.60:
            reasons.append(f"当日涨幅 {_pct_text(pct_chg)}，{limit_pct:.0f}cm涨停进度 {progress * 100:.0f}%")
        last_leader_date = str(last_event.get("date") or "")
        if last_leader_date and last_leader_date != target:
            reasons.append(f"最近一次{last_event.get('pool_type') or '龙头'}身份来自 {last_leader_date}")
        return reasons[:5]

    def _recent_dates(self, trade_date: Optional[str], lookback: int) -> List[str]:
        dates = self._available_dates()
        if not dates:
            return []
        target = str(trade_date or dates[-1])
        if target in dates:
            end_idx = dates.index(target) + 1
        else:
            end_idx = len([d for d in dates if d <= target]) or len(dates)
        return dates[max(0, end_idx - max(int(lookback or 5), 1)):end_idx]

    def _available_dates(self) -> List[str]:
        dates = []
        for path in sorted(self.screening_dir.glob("screening_*.json")):
            stem = path.stem.replace("screening_", "")
            if len(stem) == 8 and stem.isdigit():
                dates.append(stem)
        return sorted(set(dates))

    def _load_screening(self, date: str) -> Dict[str, Any]:
        path = self.screening_dir / f"screening_{date}.json"
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"trade_date": date, "final": []}


class IntradayStrengthService:
    """Realtime strength confirmation over leaders or one strategy candidate set."""

    LEADER_POOL_PROFILE = "leader_pool"

    def __init__(
        self,
        *,
        quote_service: Any = None,
        pool_service: Optional[LeaderPoolService] = None,
        entry_signal_service: Any = None,
        screening_dir: Optional[Path] = None,
    ) -> None:
        self.quote_service = quote_service
        self.pool_service = pool_service or LeaderPoolService()
        self.entry_signal_service = entry_signal_service
        inherited_dir = getattr(self.pool_service, "screening_dir", None)
        self.screening_dir = (
            Path(screening_dir or inherited_dir) if (screening_dir or inherited_dir) else None
        )

    def build(
        self,
        trade_date: Optional[str] = None,
        *,
        market_date: Optional[str] = None,
        lookback: int = 10,
        limit: int = 30,
        profile: str = LEADER_POOL_PROFILE,
    ) -> Dict[str, Any]:
        resolved_profile = str(profile or self.LEADER_POOL_PROFILE)
        if resolved_profile != self.LEADER_POOL_PROFILE:
            return self._build_strategy_observation(
                trade_date,
                market_date=market_date,
                profile=resolved_profile,
                limit=limit,
            )

        pool = self.pool_service.build_pool(trade_date, lookback=lookback, limit=limit)
        rows = pool.get("rows") or []
        quotes = self._quote_map(row.get("code") for row in rows)
        candidate_date = str(pool.get("trade_date") or trade_date or "")
        market_date = str(market_date or candidate_date)
        signals = self.entry_signal_service.evaluate(
            rows, quotes, market_date=market_date,
        ) if self.entry_signal_service is not None else {}
        enriched = [
            self._build_row(
                row,
                quotes.get(row.get("code") or "", {}),
                signals.get(row.get("code") or "", {}),
            )
            for row in rows
        ]
        enriched.sort(key=lambda row: (row["status_order"], -_to_float(row.get("turn_score")), int(row.get("pool_rank") or 999)))
        for row in enriched:
            row.pop("status_order", None)
        counts: Dict[str, int] = {}
        for row in enriched:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "ok": bool(enriched),
            "trade_date": candidate_date,
            "candidate_date": candidate_date,
            "market_date": market_date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "profile": self.LEADER_POOL_PROFILE,
            "strategy": {"id": self.LEADER_POOL_PROFILE, "name": "近期龙头池"},
            "observation_source": self.LEADER_POOL_PROFILE,
            "thresholds": {
                "weak_to_strong": "-3%至+1%",
                "continuation": "+1%至+5%",
                "acceleration": "+5%以上仅龙头/主线核心",
            },
            "counts": counts,
            "rows": enriched,
        }

    def _build_strategy_observation(
        self,
        trade_date: Optional[str],
        *,
        market_date: Optional[str],
        profile: str,
        limit: int,
    ) -> Dict[str, Any]:
        """Observe strategy candidates without changing or reusing leader identity rules."""
        from core.realtime.overlay_service import RealtimeOverlayService

        service = RealtimeOverlayService(
            quote_service=self.quote_service,
            screening_dir=self.screening_dir,
            entry_signal_service=self.entry_signal_service,
        )
        payload = service.build_overlay(
            trade_date,
            market_date=market_date,
            profile=profile,
            limit=limit,
        )
        strategy = dict(payload.get("strategy") or {})
        strategy_name = str(strategy.get("name") or profile)
        rows: List[Dict[str, Any]] = []
        for index, source in enumerate(payload.get("rows") or [], start=1):
            row = dict(source)
            status = str(row.get("confirm_status") or "observe")
            screening_score = _to_float(row.get("screening_score"))
            realtime_score = self._realtime_score(
                row.get("open_gap_pct"),
                row.get("pct_chg"),
                None,
            )
            turn_score = screening_score * 0.45 + realtime_score * 0.55
            if status == "cancelled":
                turn_score = min(turn_score, 45.0)
            row.update({
                "status": status,
                "status_text": {
                    "confirmed": "转强确认",
                    "unfilled": "信号确认·无法成交",
                    "observe": "继续观察",
                    "cancelled": "取消",
                }.get(status, "继续观察"),
                "turn_score": round(max(0.0, min(100.0, turn_score)), 2),
                "leader_score": screening_score,
                "pool_rank": index,
                "source_rank": row.get("screening_rank"),
                "pool_type": "策略候选",
                "leader_time_label": f"{strategy_name}候选",
                "last_leader_date": "",
                "lifecycle_state": "",
                "strategy_id": row.get("strategy_id") or profile,
                "strategy_name": row.get("strategy_name") or strategy_name,
                "observation_source": "strategy_profile",
                "is_leader_observation": False,
                "action": "仅按该策略的分钟入场条件确认，不改变龙头身份",
            })
            rows.append(row)
        rows.sort(
            key=lambda row: (
                {"confirmed": 0, "unfilled": 1, "observe": 2, "cancelled": 3}.get(
                    str(row.get("status") or "observe"), 2
                ),
                -_to_float(row.get("turn_score")),
                int(_to_float(row.get("pool_rank"), 999)),
            )
        )
        counts: Dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "observe")
            counts[status] = counts.get(status, 0) + 1
        return {
            **payload,
            "ok": bool(rows),
            "profile": profile,
            "strategy": strategy,
            "observation_source": "strategy_profile",
            "counts": counts,
            "rows": rows,
        }

    def _quote_map(self, codes: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
        service = self._ensure_quote_service()
        normalized = [normalize_stock_code(code, add_suffix=False) for code in codes or []]
        normalized = [code for code in dict.fromkeys(normalized) if code]
        if service is None or not normalized:
            return {}
        try:
            payload = service.get_quotes(normalized)
        except Exception:
            return {}
        return {
            normalize_stock_code(row.get("code") or "", add_suffix=False): row
            for row in payload.get("quotes") or []
            if row.get("code")
        }

    def _ensure_quote_service(self):
        if self.quote_service is not None:
            return self.quote_service
        try:
            from core.realtime.quote_service import RealtimeQuoteService

            self.quote_service = RealtimeQuoteService()
            return self.quote_service
        except Exception:
            return None

    def _build_row(
        self,
        pool_row: Dict[str, Any],
        quote: Dict[str, Any],
        signal: Dict[str, Any],
    ) -> Dict[str, Any]:
        pre_close = _to_float(quote.get("pre_close"))
        open_price = _to_float(quote.get("open_price"))
        last_price = _to_float(quote.get("last_price"))
        high_price = _to_float(quote.get("high_price"))
        change_pct = _to_float(quote.get("change_pct"))
        open_gap = (open_price / pre_close - 1.0) * 100.0 if open_price > 0 and pre_close > 0 else None
        intraday_lift = (last_price / open_price - 1.0) * 100.0 if last_price > 0 and open_price > 0 else None
        high_from_open = (high_price / open_price - 1.0) * 100.0 if high_price > 0 and open_price > 0 else None
        status = str(signal.get("signal_status") or "observe")
        order = {"confirmed": 0, "unfilled": 1, "observe": 2, "cancelled": 3}.get(status, 2)
        reason = str(signal.get("reason") or "等待当日分钟入场条件")
        mode = str(signal.get("entry_mode") or classify_entry_mode(open_price, pre_close))
        realtime_score = self._realtime_score(open_gap, change_pct, intraday_lift)
        turn_score = _to_float(pool_row.get("leader_score")) * 0.45 + realtime_score * 0.55
        if status == "cancelled":
            turn_score = min(turn_score, 45.0)
        return {
            **pool_row,
            "status": status,
            "status_order": order,
            "status_text": {
                "confirmed": "转强确认",
                "unfilled": "信号确认·无法成交",
                "observe": "继续观察",
                "cancelled": "取消",
            }.get(status, "继续观察"),
            "turn_score": round(max(0.0, min(100.0, turn_score)), 2),
            "reason": reason,
            "entry_mode": mode,
            "entry_mode_text": signal.get("entry_mode_text") or entry_mode_text(mode),
            "confirm_time": signal.get("confirm_time") or "",
            "entry_time": signal.get("entry_time") or "",
            "entry_price": signal.get("entry_price"),
            "success_probability": signal.get("success_probability"),
            "historical_samples": signal.get("historical_samples"),
            "historical_stats_basis": signal.get("historical_stats_basis") or "",
            "average_mfe_pct": signal.get("average_mfe_pct"),
            "average_mae_pct": signal.get("average_mae_pct"),
            "data_completeness": signal.get("data_completeness"),
            "confidence_grade": signal.get("confidence_grade") or "D",
            "confidence": signal.get("confidence") or {},
            "sector_detail": signal.get("sector_detail") or {},
            "quote_time": quote.get("received_at") or quote.get("time") or "",
            "quote_source": quote.get("source") or "",
            "is_stale": bool(quote.get("is_stale")),
            "last_price": round(last_price, 3) if last_price else None,
            "open_price": round(open_price, 3) if open_price else None,
            "pre_close": round(pre_close, 3) if pre_close else None,
            "change_pct": round(change_pct, 2),
            "open_gap_pct": round(open_gap, 2) if open_gap is not None else None,
            "intraday_lift_pct": round(intraday_lift, 2) if intraday_lift is not None else None,
            "high_from_open_pct": round(high_from_open, 2) if high_from_open is not None else None,
        }

    @staticmethod
    def _realtime_score(open_gap: Optional[float], change_pct: float, intraday_lift: Optional[float]) -> float:
        gap_score = 50.0 if open_gap is None else _score_between(open_gap, -1.0, 4.0)
        pct_score = _score_between(change_pct, -2.0, 7.0)
        lift_score = 50.0 if intraday_lift is None else _score_between(intraday_lift, -1.0, 4.0)
        return gap_score * 0.35 + pct_score * 0.50 + lift_score * 0.15
