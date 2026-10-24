"""SDK 3.3 evidence adapters, isolated from legacy strategy-shaped adapters."""

from __future__ import annotations

from datetime import datetime

from core.realtime.market_evidence import EvidenceResult, fingerprint, normalize_depth, number
from core.realtime.trading_session import shanghai_now


def failure(exc):
    if isinstance(exc, (AttributeError, NotImplementedError)):
        return "unsupported"
    if isinstance(exc, TimeoutError) or "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
        return "network_error"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "parse_error"
    return "request_error"


def trade_row(tick, date):
    get = tick.get if isinstance(tick, dict) else lambda key: getattr(tick, key, None)
    label = str(get("time_label") or "")
    # TDX trade records have minute precision even when a datetime has :00 seconds.
    try:
        at = (
            datetime.strptime(date + label[:5], "%Y%m%d%H:%M")
            .replace(tzinfo=shanghai_now().tzinfo)
            .isoformat(timespec="minutes")
        )
    except ValueError:
        at = None
    price, volume = number(get("price")), number(get("volume"))
    kind = get("event_kind") or "unknown"
    row = dict(
        event_time=at,
        time_precision="minute" if at else "unknown",
        time=label,
        price=price,
        volume_hand=volume,
        volume_shares=volume * 100 if volume is not None else None,
        amount_yuan=price * volume * 100
        if price is not None and volume is not None and kind != "auction_snapshot"
        else None,
        amount_is_estimated=True,
        side=get("side") if get("side") in {"buy", "sell"} else "unknown",
        event_kind=kind,
        status_raw=get("status_raw"),
        order_count=get("order_count"),
    )
    # Position/index fields are page offsets, not guaranteed exchange identities.
    row["fingerprint"] = fingerprint(row)
    if at is None or price is None or price <= 0 or volume is None or volume < 0:
        raise ValueError("Invalid trade time, price or volume")
    return row


class EltdxEvidenceMixin:
    def get_auction_evidence(self, code, date):
        result = EvidenceResult("auction_series", code, date)
        try:
            with self._client() as client:
                series = client.auctions.series(self._to_tdx_code(code), self._to_dash_date(date))
                self._validate_evidence_identity(series, code, date)
                for point in series.points:
                    price, volume = number(point.price), number(point.matched_volume)
                    seconds = int(point.time_seconds)
                    if not 0 <= seconds < 86400 or price is None or price <= 0 or volume is None or volume < 0:
                        raise ValueError("Invalid auction point")
                    at = datetime.strptime(date, "%Y%m%d").replace(
                        hour=seconds // 3600,
                        minute=seconds // 60 % 60,
                        second=seconds % 60,
                        tzinfo=shanghai_now().tzinfo,
                    )
                    result.rows.append(
                        dict(
                            event_time=at.isoformat(),
                            time_precision="second",
                            price=price,
                            matched_volume_hand=volume,
                            matched_amount_estimated_yuan=price * volume * 100,
                            unmatched_volume_hand=number(point.unmatched_volume),
                            unmatched_direction_raw=point.unmatched_direction_raw,
                            event_kind="auction_snapshot",
                        )
                    )
                result.status = "ok" if result.rows else "empty"
        except Exception as exc:
            result.status, result.reasons = failure(exc), [str(exc)]
            result.rows = []
        result.received_at = shanghai_now().isoformat(timespec="milliseconds")
        return result

    def get_opening_match_evidence(self, code, date, *, max_pages=3):
        result = EvidenceResult("opening_match", code, date)
        try:
            with self._client() as client:
                api, full = client.trades, self._to_tdx_code(code)
                tick = (
                    api.opening_match_today(full, max_pages=max_pages)
                    if self._is_today(date)
                    else api.opening_match_history(full, self._to_dash_date(date), max_pages=max_pages)
                )
                if tick is not None:
                    row = trade_row(tick, date)
                    if row["event_kind"] != "opening_match" or row["time"][:5] != "09:25":
                        raise ValueError("Not a formal 09:25 opening match")
                    result.rows = [row]
                result.status = "ok" if result.rows else "empty"
        except Exception as exc:
            result.status, result.reasons = failure(exc), [str(exc)]
        result.received_at = shanghai_now().isoformat(timespec="milliseconds")
        return result

    @staticmethod
    def _validate_evidence_identity(response, code, date):
        from core.realtime.models import normalize_stock_code

        identity = getattr(response, "code", None)
        if identity is not None and normalize_stock_code(identity) != normalize_stock_code(code):
            raise ValueError("Evidence symbol mismatch")
        day = getattr(response, "trading_date", None)
        if day is not None and day.strftime("%Y%m%d") != date:
            raise ValueError("Evidence trading date mismatch")

    def get_trade_evidence(self, code, date, *, page_size=1000, max_pages=3, anchor=None, next_sequence=0):
        """Newest pages first, stable chronological sequence only after verified overlap.

        Bootstrap requires an empty terminal page. Subsequent reads require a unique
        contiguous prior tail; ambiguous/missing anchors remain partial and do not
        advance the cursor. Raw observations remain available for diagnosis.
        """
        if not 1 <= page_size <= 1800 or not 1 <= max_pages <= 100:
            raise ValueError("Invalid pagination budget")
        result = EvidenceResult("trade_batch", code, date)
        pages, groups, terminal, overlap = 0, [], False, None
        try:
            with self._client() as client:
                for _ in range(max_pages):
                    api = client.trades
                    kwargs = dict(start=sum(len(g) for g in groups), count=page_size)
                    page = (
                        api.today(self._to_tdx_code(code), **kwargs)
                        if self._is_today(date)
                        else api.history(self._to_tdx_code(code), self._to_dash_date(date), **kwargs)
                    )
                    pages += 1
                    self._validate_evidence_identity(page, code, date)
                    group = [trade_row(tick, date) for tick in page.ticks]
                    groups.append(group)
                    rows = [row for g in reversed(groups) for row in g]
                    if anchor:
                        keys = [row["fingerprint"] for row in rows]
                        hits = [i for i in range(len(keys) - len(anchor) + 1) if keys[i : i + len(anchor)] == anchor]
                        if len(hits) == 1:
                            overlap = hits[0] + len(anchor)
                            break
                        if len(hits) > 1:
                            result.reasons = ["ambiguous_overlap"]
                            break
                    if not group:
                        terminal = True
                        break
                rows = [row for g in reversed(groups) for row in g]
                complete = overlap is not None if anchor else terminal
                # Offset pages can shift while new prints arrive. Re-read the head
                # before certifying a multi-page live response as contiguous.
                if complete and pages > 1 and self._is_today(date):
                    head = client.trades.today(self._to_tdx_code(code), start=0, count=page_size)
                    self._validate_evidence_identity(head, code, date)
                    keys = [trade_row(tick, date)["fingerprint"] for tick in head.ticks]
                    if keys != [row["fingerprint"] for row in groups[0]]:
                        complete = False
                        result.reasons = ["pagination_shifted_during_read"]
                fresh = (rows[overlap:] if overlap is not None else rows) if complete else []
                for index, row in enumerate(fresh, next_sequence):
                    row["sequence"] = index
                    row["trade_id"] = fingerprint([date, self._to_tdx_code(code), index, row["fingerprint"]])
                result.rows = fresh if complete else rows
                result.status = ("ok" if fresh else "empty") if complete else "partial"
                if not complete and not result.reasons:
                    result.reasons = ["overlap_missing" if anchor else "pagination_limit"]
                result.coverage = dict(
                    complete=complete,
                    pages=pages,
                    terminal_empty_page=terminal,
                    overlap_verified=overlap is not None,
                    incremental=bool(anchor),
                    anchor=([r["fingerprint"] for r in rows][-16:] if complete else anchor or []),
                    next_sequence=next_sequence + len(fresh) if complete else next_sequence,
                    sequence_verified=complete,
                    rows_observed=len(rows),
                )
        except Exception as exc:
            result.status, result.reasons = failure(exc), [str(exc)]
            result.rows = []
            result.coverage = dict(complete=False, pages=pages, sequence_verified=False)
        result.received_at = shanghai_now().isoformat(timespec="milliseconds")
        return result

    def get_depth_evidence(self, code, date):
        result = EvidenceResult("depth", code, date)
        try:
            if not self._is_today(date):
                result.status, result.reasons = "unsupported", ["historical_depth_unavailable"]
                return result
            with self._client() as client:
                snapshots = client.quotes.get_depth([self._to_tdx_code(code)])
                if len(snapshots) != 1:
                    result.status = "empty"
                else:
                    self._validate_evidence_identity(snapshots[0], code, date)
                    row = normalize_depth(snapshots[0])
                    result.rows = [row]
                    result.status, result.reasons = row["quality_status"], row["quality_reasons"]
        except Exception as exc:
            result.status, result.reasons = failure(exc), [str(exc)]
        result.received_at = shanghai_now().isoformat(timespec="milliseconds")
        return result
