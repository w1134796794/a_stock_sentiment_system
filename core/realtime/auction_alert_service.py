"""09:25 auction warnings for the previous trading day's candidates."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from core.realtime.models import normalize_stock_code


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class AuctionAlertService:
    def __init__(self, data_manager: Any, *, screening_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> None:
        from config.settings import WEB_DATA_DIR

        self.dm = data_manager
        self.screening_dir = Path(screening_dir or Path(WEB_DATA_DIR) / "screening")
        self.output_dir = Path(output_dir or Path(WEB_DATA_DIR) / "realtime")

    def build(self, candidate_date: str, market_date: str, *, limit: int = 20, persist: bool = True) -> Dict[str, Any]:
        candidates = self._candidates(candidate_date)[: max(int(limit), 1)]
        previous = self._previous_map(candidate_date)
        rows = []
        for candidate in candidates:
            code = normalize_stock_code(candidate.get("code") or "", add_suffix=False)
            if not code:
                continue
            ts_code = self._ts_code(code)
            try:
                auction = self.dm.get_auction_data(ts_code, market_date) or {}
            except Exception as exc:  # noqa: BLE001
                auction = {"error": str(exc)}
            open_price = _number(auction.get("开盘价"))
            pre_close = _number((previous.get(code) or {}).get("close"))
            gap_pct = (open_price / pre_close - 1.0) * 100.0 if open_price > 0 and pre_close > 0 else None
            category, conclusion = self._classify(gap_pct)
            rows.append({
                "code": code,
                "name": candidate.get("name") or "",
                "rank": candidate.get("rank"),
                "open_price": open_price or None,
                "pre_close": pre_close or None,
                "open_gap_pct": round(gap_pct, 2) if gap_pct is not None else None,
                "auction_amount": _number(auction.get("竞价成交额")) or None,
                "auction_source": auction.get("数据源") or "",
                "category": category,
                "conclusion": conclusion,
                "resonance_sectors": candidate.get("resonance_sectors") or "",
            })
        payload = {
            "ok": bool(rows),
            "candidate_date": str(candidate_date),
            "market_date": str(market_date),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "rows": rows,
        }
        if persist:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.output_dir / f"auction_alert_{market_date}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["output_path"] = str(path)
        return payload

    def _candidates(self, candidate_date: str) -> list[Dict[str, Any]]:
        path = self.screening_dir / f"screening_{candidate_date}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [row for row in (payload.get("final") or []) if isinstance(row, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def _previous_map(self, candidate_date: str) -> Dict[str, Dict[str, Any]]:
        try:
            frame = self.dm.get_all_stocks_daily(candidate_date)
        except Exception:
            frame = pd.DataFrame()
        if frame is None or frame.empty:
            return {}
        out = {}
        for row in frame.to_dict("records"):
            code = normalize_stock_code(row.get("ts_code") or row.get("code") or "", add_suffix=False)
            if code:
                out[code] = row
        return out

    @staticmethod
    def _classify(gap_pct: Optional[float]) -> tuple[str, str]:
        if gap_pct is None:
            return "数据不足", "竞价数据尚未取得，不做入场判断"
        if gap_pct < -3:
            return "大幅低开", "超出弱转强观察区间，优先放弃"
        if gap_pct <= 1:
            return "弱转强观察", "等待收复昨收、站上均价并突破前5分钟高点"
        if gap_pct <= 5:
            return "强势延续观察", "等待回踩均价不破或突破前5分钟高点"
        return "高开加速观察", "仅龙头或主线核心参与，必须证明真实可成交"

    @staticmethod
    def _ts_code(code: str) -> str:
        if code.startswith(("60", "68")):
            return f"{code}.SH"
        if code.startswith(("8", "4")):
            return f"{code}.BJ"
        return f"{code}.SZ"


__all__ = ["AuctionAlertService"]
