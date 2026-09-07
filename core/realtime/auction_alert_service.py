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

    @staticmethod
    def notification_content(payload: Dict[str, Any], *, max_rows: int = 20) -> str:
        """Build a readable auction summary with the stocks behind each count."""
        rows = [dict(row) for row in payload.get("rows") or [] if isinstance(row, dict)]
        category_order = (
            "高开加速观察",
            "强势延续观察",
            "弱转强观察",
            "大幅低开",
            "数据不足",
        )
        grouped: Dict[str, list[Dict[str, Any]]] = {}
        for row in rows:
            category = str(row.get("category") or "数据不足")
            grouped.setdefault(category, []).append(row)

        candidate_date = str(payload.get("candidate_date") or "--")
        lines = [f"观察{candidate_date}候选：共{len(rows)}只。", "竞价幅度均为开盘价相对昨日收盘价。"]
        shown = 0
        categories = [name for name in category_order if name in grouped]
        categories.extend(name for name in grouped if name not in category_order)
        for category in categories:
            category_rows = sorted(
                grouped[category],
                key=lambda row: (
                    -_number(row.get("open_gap_pct"), -999.0),
                    int(_number(row.get("rank"), 9999)),
                ),
            )
            lines.extend(("", f"【{category}】{len(category_rows)}只"))
            for row in category_rows:
                if shown >= max(int(max_rows), 1):
                    break
                name = str(row.get("name") or row.get("code") or "候选股")
                code = str(row.get("code") or "")
                identity = f"{name}（{code}）" if code else name
                gap = row.get("open_gap_pct")
                if gap is None:
                    quote_text = "竞价数据不足"
                else:
                    gap_value = _number(gap)
                    direction = "高开" if gap_value > 0 else "低开" if gap_value < 0 else "平开"
                    quote_text = f"{direction}{gap_value:+.2f}%"
                    open_price = _number(row.get("open_price"))
                    if open_price > 0:
                        quote_text += f"，开盘{open_price:.2f}"
                sectors = row.get("resonance_sectors") or ""
                if isinstance(sectors, (list, tuple, set)):
                    sectors = "、".join(str(item) for item in sectors if item)
                sector_text = f"，板块：{sectors}" if str(sectors).strip() else ""
                lines.append(f"{shown + 1}. {identity}：{quote_text}{sector_text}")
                shown += 1
            if shown >= max(int(max_rows), 1):
                break
        if shown < len(rows):
            lines.extend(("", f"其余{len(rows) - shown}只请在交易工作台查看。"))
        lines.extend(("", "动作：09:30后等待分钟条件确认，不以竞价结果直接买入。"))
        return "\n".join(lines)

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
