"""Versioned observational evidence. Never grants strategy or fill eligibility."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version

from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now

EVIDENCE_KINDS = frozenset(
    {"auction_series", "opening_match", "trade_batch", "depth", "evidence_quality", "evidence_publication"}
)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def number(value):
    """Missing/non-finite numbers remain unknown rather than becoming zero."""
    try:
        result = float(value) if value is not None else None
        return result if result is not None and math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("Evidence timestamp requires timezone")
    return shanghai_now(stamp)


@dataclass
class EvidenceResult:
    kind: str
    code: str
    market_date: str
    status: str = "ok"
    rows: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    received_at: str = field(default_factory=lambda: shanghai_now().isoformat(timespec="milliseconds"))

    def envelope(self, *, plan_id="", capture_mode="live"):
        datetime.strptime(self.market_date, "%Y%m%d")
        if self.kind not in EVIDENCE_KINDS or capture_mode not in {"live", "backfill"}:
            raise ValueError("Invalid evidence kind or capture mode")
        code = normalize_stock_code(self.code)
        if len(code) != 6 or not code.isdigit():
            raise ValueError("Invalid stock code")
        if self.status not in {
            "ok",
            "empty",
            "partial",
            "unsupported",
            "network_error",
            "parse_error",
            "request_error",
        }:
            raise ValueError("Invalid evidence quality status")
        timestamp(self.received_at)
        if capture_mode == "live" and timestamp(self.received_at).strftime("%Y%m%d") != self.market_date:
            raise ValueError("Live evidence date differs from receipt date")
        try:
            sdk = version("eltdx")
        except Exception:
            sdk = "unavailable"
        summary = (
            trade_summary(self.rows)
            if self.kind == "trade_batch" and self.coverage.get("sequence_verified")
            else auction_summary(self.rows)
            if self.kind == "auction_series" and self.status == "ok"
            else {}
        )
        return dict(
            schema_version=1,
            source="eltdx",
            sdk_version=sdk,
            kind=self.kind,
            code=code,
            market_date=self.market_date,
            event_time=None,
            time_precision="unknown",
            received_at=self.received_at,
            published_at=None,
            collection_plan_id=plan_id,
            capture_mode=capture_mode,
            quality_status=self.status,
            quality_reasons=self.reasons,
            coverage=self.coverage,
            rows=self.rows,
            summary=summary,
            summary_scope="response_batch",
            formal_eligible=False,
        )


def normalize_depth(snapshot):
    def levels(name):
        out = []
        for index, item in enumerate(getattr(snapshot, name, ()) or ()):
            if index == 5:
                break
            get = item.get if isinstance(item, dict) else lambda key, item=item: getattr(item, key, None)
            price, volume = number(get("price")), number(get("volume"))
            out.append(
                dict(
                    level=index + 1,
                    price=price,
                    volume_hand=volume,
                    volume_shares=volume * 100 if volume is not None else None,
                )
            )
        return out

    bids, asks = levels("buy_levels"), levels("sell_levels")
    reasons = []
    if len(bids) != 5 or len(asks) != 5:
        reasons.append("insufficient_levels")
    if any(
        x["price"] is None or x["volume_hand"] is None or x["price"] < 0 or x["volume_hand"] < 0 for x in bids + asks
    ):
        reasons.append("invalid_level")
    active_bids = [x["price"] for x in bids if x["price"] is not None and x["price"] > 0]
    active_asks = [x["price"] for x in asks if x["price"] is not None and x["price"] > 0]
    if active_bids != sorted(active_bids, reverse=True) or active_asks != sorted(active_asks):
        reasons.append("unordered_levels")
    spread = active_asks[0] - active_bids[0] if active_bids and active_asks else None
    if spread is not None and spread < 0:
        reasons.append("crossed_book")
    bv = sum(x["volume_hand"] for x in bids) if bids and all(x["volume_hand"] is not None for x in bids) else None
    av = sum(x["volume_hand"] for x in asks) if asks and all(x["volume_hand"] is not None for x in asks) else None
    return dict(
        buy_levels=bids,
        sell_levels=asks,
        spread_yuan=spread,
        bid_depth_hand=bv,
        ask_depth_hand=av,
        imbalance=(bv - av) / (bv + av) if bv is not None and av is not None and bv + av > 0 else None,
        book_state="two_sided"
        if active_bids and active_asks
        else "one_sided"
        if active_bids or active_asks
        else "empty",
        quality_status="partial" if reasons else "ok",
        quality_reasons=reasons,
        exchange_timestamp_verified=False,
    )


def trade_summary(rows):
    """Descriptive trade statistics; side is the vendor's classification."""
    actual = [row for row in rows if row.get("event_kind") == "trade"]
    amounts = [row["amount_yuan"] for row in actual if row.get("amount_yuan") is not None]
    total = sum(amounts)
    by_side = {
        side: sum(row.get("amount_yuan") or 0 for row in actual if row.get("side") == side)
        for side in ("buy", "sell", "unknown")
    }
    return dict(
        count=len(actual),
        volume_hand=sum(row.get("volume_hand") or 0 for row in actual),
        amount_estimated_yuan=total,
        amount_by_side_yuan=by_side,
        buy_amount_share=by_side["buy"] / total if total > 0 else None,
        top5_amount_share=sum(sorted(amounts, reverse=True)[:5]) / total if total > 0 else None,
        source_direction_is_inference=True,
        time_precision="minute",
    )


def auction_summary(rows):
    points = sorted((row for row in rows if row.get("event_time")), key=lambda row: row["event_time"])
    if not points:
        return dict(point_count=0, tail_price_change_pct=None, matched_volume_change_hand=None)
    last = points[-1]
    tail = [
        row for row in points if (timestamp(last["event_time"]) - timestamp(row["event_time"])).total_seconds() <= 60
    ]
    first = tail[0]
    price, last_price = number(first.get("price")), number(last.get("price"))
    first_volume, last_volume = number(first.get("matched_volume_hand")), number(last.get("matched_volume_hand"))
    return dict(
        point_count=len(points),
        last_event_time=last["event_time"],
        tail_window_seconds=60,
        tail_price_change_pct=(last_price / price - 1) * 100
        if price and price > 0 and last_price is not None
        else None,
        matched_volume_change_hand=last_volume - first_volume
        if last_volume is not None and first_volume is not None
        else None,
        matched_amount_is_estimated=True,
        actual_executed_volume=False,
        actual_cancellation_volume_known=False,
    )
