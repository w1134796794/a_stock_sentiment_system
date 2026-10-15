"""Opening-price semantics shared by live quotes, diagnostics and replay."""
from __future__ import annotations

import math


def positive_price(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def opening_gap(open_price, pre_close):
    opening, reference = positive_price(open_price), positive_price(pre_close)
    if opening is None or reference is None:
        return None
    return round(opening / reference - 1, 10)


def classify_opening_gap(gap, *, weak_max_gap, continuation_max_gap):
    if gap is None or not math.isfinite(gap):
        return ""
    if gap <= weak_max_gap:
        return "weak_only"
    if gap <= continuation_max_gap:
        return "continuation_only"
    return "acceleration_only"


def opening_context(quote, previous=None):
    """A minute point's open/close is never a session opening-price source."""
    previous = previous or {}
    opening = positive_price(quote.get("open_price"))
    quoted_reference = positive_price(quote.get("pre_close"))
    reference = quoted_reference or positive_price(previous.get("close"))
    return {"open_price": opening, "pre_close": reference,
            "open_gap": opening_gap(opening, reference),
            "opening_source": quote.get("opening_source") or quote.get("source") or "",
            "reference_source": "session_pre_close" if quoted_reference else "previous_session_close" if reference else "missing",
            "received_at": quote.get("opening_received_at") or quote.get("received_at") or ""}


def sector_reference_rows(previous, opening_evidence):
    return {code: {**previous.get(code, {}),
                   "close": positive_price(opening_evidence.get(code, {}).get("pre_close"))
                            or previous.get(code, {}).get("close")}
            for code in previous.keys() | opening_evidence.keys()}
