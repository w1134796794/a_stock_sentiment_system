"""A-share price-limit helpers shared by factors, screening and trading views."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional


def normalize_code(code: Any) -> str:
    text = str(code or "").strip().upper()
    if "." in text:
        text = text.split(".")[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def is_st_stock(name: Any) -> bool:
    label = str(name or "").upper().replace(" ", "")
    return "ST" in label or "退" in str(name or "")


def get_price_limit_pct(code: Any, name: Any = "", pre_close: Optional[float] = None) -> Optional[float]:
    """Return daily price-limit ratio, e.g. 0.20 for ChiNext/STAR."""
    try:
        if pre_close is not None and float(pre_close) <= 0:
            return None
    except Exception:
        pass

    code6 = normalize_code(code)
    if not code6:
        return 0.10

    # STAR Market / ChiNext: +/-20%.
    if code6.startswith(("688", "689", "300", "301")):
        return 0.20

    # Beijing Stock Exchange: +/-30%.
    if code6.startswith(("43", "83", "87", "88", "920")):
        return 0.30

    # Main board ST: +/-5%; ordinary main board: +/-10%.
    return 0.05 if is_st_stock(name) else 0.10


def get_price_limit_pct_points(code: Any, name: Any = "", pre_close: Optional[float] = None) -> Optional[float]:
    pct = get_price_limit_pct(code, name, pre_close)
    return None if pct is None else pct * 100.0


def limit_progress(pct_chg: Any, code: Any, name: Any = "", pre_close: Optional[float] = None) -> float:
    limit_pct = get_price_limit_pct_points(code, name, pre_close)
    if not limit_pct:
        return 0.0
    try:
        return float(pct_chg) / limit_pct
    except Exception:
        return 0.0


def round_price(price: float) -> float:
    try:
        return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return round(float(price), 2)


def limit_up_price(pre_close: float, code: Any, name: Any = "") -> Optional[float]:
    pct = get_price_limit_pct(code, name, pre_close)
    if pct is None or pre_close <= 0:
        return None
    return round_price(float(pre_close) * (1.0 + pct))


def limit_down_price(pre_close: float, code: Any, name: Any = "") -> Optional[float]:
    pct = get_price_limit_pct(code, name, pre_close)
    if pct is None or pre_close <= 0:
        return None
    return round_price(float(pre_close) * (1.0 - pct))
