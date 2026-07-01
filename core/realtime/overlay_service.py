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
        min_open_gap_pct: float = 0.0,
        min_intraday_pct: float = 0.0,
        cancel_intraday_pct: float = -2.0,
    ):
        from config.settings import SNAPSHOT_DIR, WEB_DATA_DIR
        from snapshot.reader import SnapshotReader

        self.quote_service = quote_service
        self.screening_dir = Path(screening_dir or WEB_DATA_DIR / "screening")
        self.snapshot_reader = snapshot_reader or SnapshotReader(SNAPSHOT_DIR)
        self.output_dir = Path(output_dir or WEB_DATA_DIR / "realtime")
        self.min_open_gap_pct = float(min_open_gap_pct)
        self.min_intraday_pct = float(min_intraday_pct)
        self.cancel_intraday_pct = float(cancel_intraday_pct)

    def build_overlay(
        self,
        trade_date: Optional[str] = None,
        *,
        candidates: Optional[Iterable[Dict[str, Any]]] = None,
        profile: str = "",
        limit: int = 20,
        persist: bool = False,
    ) -> Dict[str, Any]:
        trade_date = str(trade_date or self._latest_date() or "")
        rows = list(candidates) if candidates is not None else self._load_candidates(trade_date, profile=profile)
        rows = self._dedupe_candidates(rows)[: max(int(limit or 20), 1)]
        codes = [r["code"] for r in rows if r.get("code")]

        quotes = self._quote_map(codes)
        overlay_rows = []
        for cand in rows:
            quote = quotes.get(cand.get("code") or "", {})
            overlay_rows.append(self._build_row(trade_date, cand, quote))

        counts = {
            "confirmed": sum(1 for r in overlay_rows if r["confirm_status"] == "confirmed"),
            "cancelled": sum(1 for r in overlay_rows if r["confirm_status"] == "cancelled"),
            "observe": sum(1 for r in overlay_rows if r["confirm_status"] == "observe"),
        }
        screening_exists = self._screening_path(trade_date, profile).exists()
        payload = {
            "ok": bool(overlay_rows),
            "trade_date": trade_date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": "当日指标筛选" if screening_exists else "当日指标筛选未生成",
            "profile": profile or "",
            "thresholds": {
                "min_open_gap_pct": self.min_open_gap_pct,
                "min_intraday_pct": self.min_intraday_pct,
                "cancel_intraday_pct": self.cancel_intraday_pct,
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
        path = self.output_dir / f"overlay_{date}.json"
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
                    return list(final)
            except Exception:
                pass
        return []

    def _screening_path(self, trade_date: str, profile: str = "") -> Path:
        suffix = f"_{profile}" if profile else ""
        preferred = self.screening_dir / f"screening_{trade_date}{suffix}.json"
        if preferred.exists():
            return preferred
        return self.screening_dir / f"screening_{trade_date}.json"

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

    def _build_row(self, trade_date: str, candidate: Dict[str, Any], quote: Dict[str, Any]) -> Dict[str, Any]:
        open_price = _to_float(quote.get("open_price"))
        pre_close = _to_float(quote.get("pre_close"))
        last_price = _to_float(quote.get("last_price"))
        change_pct = _to_float(quote.get("change_pct"))
        gap_pct = (open_price / pre_close - 1.0) * 100.0 if open_price > 0 and pre_close > 0 else None
        status, reason = self._decide(quote, gap_pct, change_pct)
        return {
            "trade_date": trade_date,
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
            "candidate_reasons": candidate.get("reasons") or [],
        }

    def _decide(self, quote: Dict[str, Any], gap_pct: Optional[float], change_pct: float) -> tuple[str, str]:
        if not quote:
            return "observe", "未获取到实时行情，保持观察"
        if quote.get("is_stale"):
            return "observe", "行情可能过期，保持观察"
        if gap_pct is not None and gap_pct < 0:
            return "cancelled", f"竞价/开盘低开 {gap_pct:.2f}%，放弃"
        if change_pct <= self.cancel_intraday_pct:
            return "cancelled", f"盘中跌幅 {change_pct:.2f}% 触发取消线"
        if gap_pct is not None and gap_pct >= self.min_open_gap_pct and change_pct >= self.min_intraday_pct:
            return "confirmed", f"高开 {gap_pct:.2f}% 且实时涨幅 {change_pct:.2f}%，确认"
        return "observe", "未满足确认条件，继续观察"

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
