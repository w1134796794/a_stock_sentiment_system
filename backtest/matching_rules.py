"""
A 股撮合规则（B-1b）

把回测撮合里的 A 股特有规则收敛成纯函数，供 ``ReplayEngine`` / ``BacktestEngine`` 复用：

- 分板块涨跌停幅度：主板 ±10%、科创/创业 ±20%、北交所 ±30%、主板 ST ±5%、新股首日无限制。
- 涨跌停价：``round(pre_close * (1±pct), 2)``（四舍五入到分）。
- 一字板：开=高=低=收=涨停 → 无法买入；开=高=低=收=跌停 → 无法卖出。
- 停牌：无成交量 / 无行情 → 当日不可成交。
- 撮合 + 滑点：买入价上不破涨停、卖出价下不破跌停。

全部纯函数，离线可测，不依赖网络。``ohlc`` 统一为 dict：
``{open, high, low, close, pre_close, vol}``（缺字段按 0 处理）。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Tuple

# 价格容差：涨跌停判定时允许的相对误差
_LIMIT_TOL = 0.003


def normalize_code(code) -> str:
    """取 6 位纯数字代码（去掉 .SH/.SZ/.BJ 后缀）。"""
    s = str(code).strip()
    if "." in s:
        s = s.split(".")[0]
    return s.zfill(6)


def is_st(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return "ST" in n or "退" in (name or "")


def get_price_limit_pct(code, name: str = "", pre_close: Optional[float] = None) -> Optional[float]:
    """
    返回涨跌停幅度（如 0.10）；新股首日 / 无昨收返回 None（表示当日不设涨跌停）。
    """
    if pre_close is not None and pre_close <= 0:
        return None  # 新股首日 / 无昨收

    code6 = normalize_code(code)

    # 科创板(688) / 创业板(300,301)：±20%（含其风险警示股）
    if code6.startswith("688") or code6.startswith(("300", "301")):
        return 0.20
    # 北交所：±30%
    if code6.startswith(("43", "83", "87", "88", "920")):
        return 0.30
    # 主板：ST ±5%，普通 ±10%
    return 0.05 if is_st(name) else 0.10


def round_price(price: float) -> float:
    """四舍五入到分（A 股最小价格变动 0.01）。"""
    try:
        return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return round(float(price), 2)


def limit_up_price(pre_close: float, code, name: str = "") -> Optional[float]:
    pct = get_price_limit_pct(code, name, pre_close)
    if pct is None or pre_close <= 0:
        return None
    return round_price(pre_close * (1 + pct))


def limit_down_price(pre_close: float, code, name: str = "") -> Optional[float]:
    pct = get_price_limit_pct(code, name, pre_close)
    if pct is None or pre_close <= 0:
        return None
    return round_price(pre_close * (1 - pct))


def _get(ohlc: Dict, key: str) -> float:
    try:
        v = ohlc.get(key, 0) if ohlc else 0
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def open_gap_pct(ohlc: Dict, pre_close: Optional[float] = None) -> Optional[float]:
    """开盘相对昨收涨跌幅；缺少开盘价/昨收价时返回 None。"""
    open_price = _get(ohlc, "open")
    try:
        prev = _get(ohlc, "pre_close") if pre_close is None else float(pre_close or 0)
    except Exception:
        prev = 0.0
    if open_price <= 0 or prev <= 0:
        return None
    return (open_price - prev) / prev


def is_positive_open_gap(ohlc: Dict, pre_close: Optional[float] = None) -> bool:
    """是否满足“早盘竞价高开才买入”：开盘价必须严格高于昨收。"""
    gap = open_gap_pct(ohlc, pre_close)
    return gap is not None and gap > 0


def is_suspended(ohlc: Optional[Dict]) -> bool:
    """停牌 / 无行情：无 OHLC 或成交量为 0。"""
    if not ohlc:
        return True
    if _get(ohlc, "vol") <= 0 and _get(ohlc, "amount") <= 0:
        return True
    if _get(ohlc, "close") <= 0 and _get(ohlc, "open") <= 0:
        return True
    return False


def is_limit_up(close: float, pre_close: float, code, name: str = "") -> bool:
    lu = limit_up_price(pre_close, code, name)
    if lu is None or close <= 0:
        return False
    return close >= lu * (1 - _LIMIT_TOL)


def is_limit_down(close: float, pre_close: float, code, name: str = "") -> bool:
    ld = limit_down_price(pre_close, code, name)
    if ld is None or close <= 0:
        return False
    return close <= ld * (1 + _LIMIT_TOL)


def _is_one_word(ohlc: Dict) -> bool:
    o, h, l, c = _get(ohlc, "open"), _get(ohlc, "high"), _get(ohlc, "low"), _get(ohlc, "close")
    if min(o, h, l, c) <= 0:
        return False
    return abs(h - l) <= max(0.01, h * 1e-4)


def is_one_word_limit_up(ohlc: Dict, pre_close: float, code, name: str = "") -> bool:
    """一字涨停：全天高=低且封在涨停 → 无法买入。"""
    if not _is_one_word(ohlc):
        return False
    return is_limit_up(_get(ohlc, "close"), pre_close, code, name)


def is_one_word_limit_down(ohlc: Dict, pre_close: float, code, name: str = "") -> bool:
    """一字跌停：全天高=低且封在跌停 → 无法卖出。"""
    if not _is_one_word(ohlc):
        return False
    return is_limit_down(_get(ohlc, "close"), pre_close, code, name)


def simulate_buy(target_price: float, ohlc: Dict, pre_close: float,
                 code, name: str = "", slippage: float = 0.0) -> Tuple[bool, float, str]:
    """
    模拟买入成交。

    Returns:
        (是否成交, 成交价, 说明)
    """
    if is_suspended(ohlc):
        return False, 0.0, "停牌无法买入"

    high, low = _get(ohlc, "high"), _get(ohlc, "low")
    if high <= 0 or low <= 0:
        return False, 0.0, "无有效行情"

    # 一字涨停 / 全天封板：买不进
    if pre_close > 0 and is_one_word_limit_up(ohlc, pre_close, code, name):
        return False, 0.0, "一字涨停无法买入"

    if target_price <= 0:
        # 未给目标价 → 以开盘价成交
        fill = _get(ohlc, "open") or low
    elif low <= target_price <= high:
        fill = target_price
    elif target_price < low:
        fill = low  # 目标价低于全天最低 → 以最低价成交（更易撮合）
    else:
        return False, 0.0, f"目标价{target_price:.2f}高于当日最高{high:.2f}，未成交"

    fill *= (1 + slippage)
    lu = limit_up_price(pre_close, code, name) if pre_close > 0 else None
    if lu is not None:
        fill = min(fill, lu)  # 买入价不破涨停
    return True, round_price(fill), "成交"


def simulate_sell(target_price: float, ohlc: Dict, pre_close: float,
                  code, name: str = "", slippage: float = 0.0) -> Tuple[bool, float, str]:
    """
    模拟卖出成交。

    Returns:
        (是否成交, 成交价, 说明)
    """
    if is_suspended(ohlc):
        return False, 0.0, "停牌无法卖出"

    high, low = _get(ohlc, "high"), _get(ohlc, "low")
    if high <= 0 or low <= 0:
        return False, 0.0, "无有效行情"

    # 一字跌停：卖不出
    if pre_close > 0 and is_one_word_limit_down(ohlc, pre_close, code, name):
        return False, 0.0, "一字跌停无法卖出"

    if target_price <= 0:
        fill = _get(ohlc, "open") or high
    else:
        fill = min(max(target_price, low), high)  # 夹在当日 [低, 高] 之间

    fill *= (1 - slippage)
    ld = limit_down_price(pre_close, code, name) if pre_close > 0 else None
    if ld is not None:
        fill = max(fill, ld)  # 卖出价不破跌停
    return True, round_price(fill), "成交"


__all__ = [
    "normalize_code", "is_st", "get_price_limit_pct", "round_price",
    "limit_up_price", "limit_down_price", "is_suspended",
    "open_gap_pct", "is_positive_open_gap",
    "is_limit_up", "is_limit_down",
    "is_one_word_limit_up", "is_one_word_limit_down",
    "simulate_buy", "simulate_sell",
]
