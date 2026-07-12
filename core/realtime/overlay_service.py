"""Realtime overlay for Phase 5: confirm/cancel/observe precomputed candidates."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.realtime.models import normalize_stock_code


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


class RealtimeOverlayService:
    """Overlay realtime quotes on the requested day's screening candidates only."""

    def __init__(
        self,
        quote_service: Any = None,
        *,
        screening_dir: Optional[Path] = None,
        snapshot_reader: Any = None,
        output_dir: Optional[Path] = None,
        entry_signal_service: Any = None,
    ):
        from config.settings import SNAPSHOT_DIR, WEB_DATA_DIR
        from snapshot.reader import SnapshotReader

        self.quote_service = quote_service
        self.screening_dir = Path(screening_dir or WEB_DATA_DIR / "screening")
        self.snapshot_reader = snapshot_reader or SnapshotReader(SNAPSHOT_DIR)
        self.output_dir = Path(output_dir or WEB_DATA_DIR / "realtime")
        self.entry_signal_service = entry_signal_service

    def build_overlay(
        self,
        trade_date: Optional[str] = None,
        *,
        market_date: Optional[str] = None,
        candidates: Optional[Iterable[Dict[str, Any]]] = None,
        profile: str = "",
        limit: int = 20,
        persist: bool = False,
    ) -> Dict[str, Any]:
        candidate_date = str(trade_date or self._latest_date() or "")
        market_date = str(market_date or candidate_date)
        rows = list(candidates) if candidates is not None else self._load_candidates(candidate_date, profile=profile)
        rows = self._dedupe_candidates(rows)[: max(int(limit or 20), 1)]
        strategy = self._strategy_metadata(profile, rows)
        codes = [r["code"] for r in rows if r.get("code")]

        quotes = self._quote_map(codes)
        if self.entry_signal_service is None:
            signals = {}
        else:
            try:
                signals = self.entry_signal_service.evaluate(
                    rows, quotes, market_date=market_date, execution=strategy["execution"],
                )
            except TypeError:
                # Lightweight third-party/test signal adapters may predate the strategy contract.
                signals = self.entry_signal_service.evaluate(rows, quotes, market_date=market_date)
        overlay_rows = []
        for cand in rows:
            quote = quotes.get(cand.get("code") or "", {})
            signal = signals.get(cand.get("code") or "", {})
            row = self._build_row(candidate_date, market_date, cand, quote, signal)
            row.update({
                "strategy_id": strategy["id"],
                "strategy_name": strategy["name"],
                "strategy_version": strategy["version"],
                "strategy_execution": dict(strategy["execution"]),
                "position_cap_pct": strategy["position_cap_pct"],
            })
            overlay_rows.append(row)

        counts = {
            "confirmed": sum(1 for r in overlay_rows if r["confirm_status"] == "confirmed"),
            "cancelled": sum(1 for r in overlay_rows if r["confirm_status"] == "cancelled"),
            "observe": sum(1 for r in overlay_rows if r["confirm_status"] == "observe"),
            "unfilled": sum(1 for r in overlay_rows if r["confirm_status"] == "unfilled"),
        }
        screening_exists = self._screening_path(candidate_date, strategy["id"]).exists()
        payload = {
            "ok": bool(overlay_rows),
            "trade_date": candidate_date,
            "candidate_date": candidate_date,
            "market_date": market_date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": f"候选策略 · {strategy['name']}" if screening_exists else "候选策略尚未生成",
            "profile": strategy["id"],
            "strategy": strategy,
            "thresholds": {
                "weak_to_strong": "-3%至+1%",
                "continuation": "+1%至+5%",
                "acceleration": "+5%以上仅龙头/主线核心",
            },
            "counts": counts,
            "rows": overlay_rows,
        }
        if persist:
            payload["output_path"] = str(self.persist(payload))
        return payload

    def persist(self, payload: Dict[str, Any]) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        date = str(payload.get("trade_date") or datetime.now().strftime("%Y%m%d"))
        market_date = str(payload.get("market_date") or date)
        profile = str(payload.get("profile") or "default")
        path = self.output_dir / f"overlay_{date}_{market_date}_{profile}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _latest_date(self) -> str:
        try:
            return str(self.snapshot_reader.latest() or "")
        except Exception:
            return ""

    def _load_candidates(self, trade_date: str, *, profile: str = "") -> List[Dict[str, Any]]:
        path = self._screening_path(trade_date, profile)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                final = data.get("final") or []
                if final:
                    strategy_id = str(data.get("strategy_id") or profile or data.get("profile") or "default")
                    strategy_name = str(data.get("strategy_name") or strategy_id)
                    strategy_version = str(data.get("strategy_version") or "")
                    execution = dict(data.get("strategy_execution") or {})
                    position_cap_pct = _to_float(data.get("position_cap_pct"))
                    rows = []
                    for item in final:
                        row = dict(item or {})
                        row.setdefault("strategy_id", strategy_id)
                        row.setdefault("strategy_name", strategy_name)
                        row.setdefault("strategy_version", strategy_version)
                        row.setdefault("strategy_execution", execution)
                        row.setdefault("position_cap_pct", position_cap_pct)
                        rows.append(row)
                    return rows
            except Exception:
                pass
        return []

    def _screening_path(self, trade_date: str, profile: str = "") -> Path:
        if profile:
            combination = self.screening_dir / "combinations" / str(profile) / f"screening_{trade_date}.json"
            if combination.exists():
                return combination
            legacy = self.screening_dir / f"screening_{trade_date}_{profile}.json"
            if legacy.exists():
                return legacy
            if str(profile) != "default":
                return combination
        return self.screening_dir / f"screening_{trade_date}.json"

    def profile_summaries(self, trade_date: str) -> List[Dict[str, Any]]:
        """Return strategy tabs with counts without requesting realtime quotes."""
        try:
            from core.screening.strategy_profiles import StrategyProfileRepository

            profiles = StrategyProfileRepository().list_profiles(enabled_only=True)
        except Exception:
            profiles = []
        summaries: List[Dict[str, Any]] = []
        for profile in profiles:
            path = self._screening_path(trade_date, str(profile.get("id") or ""))
            payload: Dict[str, Any] = {}
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
            summaries.append({
                "id": profile.get("id"),
                "name": profile.get("name"),
                "version": profile.get("version"),
                "execution": profile.get("execution") or {},
                "candidate_count": len(payload.get("final") or []),
                "available": bool(payload),
                "primary": bool(profile.get("primary")),
            })
        return summaries

    @staticmethod
    def _strategy_metadata(profile: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        first = dict(rows[0] or {}) if rows else {}
        profile_id = str(profile or first.get("strategy_id") or first.get("profile") or "default")
        try:
            from core.screening.strategy_profiles import StrategyProfileRepository

            strategy = StrategyProfileRepository().get_profile(profile_id) or {}
        except Exception:
            strategy = {}
        execution = dict(first.get("strategy_execution") or strategy.get("execution") or {})
        return {
            "id": profile_id,
            "name": str(first.get("strategy_name") or strategy.get("name") or profile_id),
            "version": str(first.get("strategy_version") or strategy.get("version") or ""),
            "execution": execution,
            "position_cap_pct": _to_float(first.get("position_cap_pct"), _to_float(strategy.get("position_cap_pct"))),
        }

    def _quote_map(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        service = self._ensure_quote_service()
        if service is None or not codes:
            return {}
        try:
            result = service.get_quotes(codes)
        except Exception:
            return {}
        return {
            normalize_stock_code(row.get("code") or "", add_suffix=False): row
            for row in (result.get("quotes") or [])
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
        candidate_date: str,
        market_date: str,
        candidate: Dict[str, Any],
        quote: Dict[str, Any],
        signal: Dict[str, Any],
    ) -> Dict[str, Any]:
        open_price = _to_float(quote.get("open_price"))
        pre_close = _to_float(quote.get("pre_close"))
        last_price = _to_float(quote.get("last_price"))
        change_pct = _to_float(quote.get("change_pct"))
        gap_pct = (open_price / pre_close - 1.0) * 100.0 if open_price > 0 and pre_close > 0 else None
        status = str(signal.get("signal_status") or "observe")
        reason = str(signal.get("reason") or "等待当日分钟入场条件")
        return {
            "trade_date": candidate_date,
            "candidate_date": candidate_date,
            "market_date": market_date,
            "code": candidate.get("code") or "",
            "name": quote.get("name") or candidate.get("name") or "",
            "screening_rank": candidate.get("rank"),
            "screening_score": candidate.get("score"),
            "resonance_sectors": candidate.get("resonance_sectors") or "",
            "received_at": quote.get("received_at") or quote.get("time") or "",
            "last_price": last_price,
            "open_price": open_price,
            "pre_close": pre_close,
            "pct_chg": change_pct,
            "open_gap_pct": gap_pct,
            "sector_rt_score": None,
            "is_stale": bool(quote.get("is_stale")),
            "confirm_status": status,
            "reason": reason,
            "entry_mode": signal.get("entry_mode") or "",
            "entry_mode_text": signal.get("entry_mode_text") or "等待分类",
            "signal_status_text": signal.get("signal_status_text") or "观察",
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
            "candidate_reasons": candidate.get("reasons") or [],
        }

    @staticmethod
    def _dedupe_candidates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        seen = set()
        for row in rows:
            code = normalize_stock_code(row.get("code") or row.get("stock_code") or "", add_suffix=False)
            if not code or code in seen:
                continue
            seen.add(code)
            item = dict(row)
            item["code"] = code
            out.append(item)
        return out
