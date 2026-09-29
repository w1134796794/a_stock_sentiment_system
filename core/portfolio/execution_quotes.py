"""Conservative quote gate shared by simulated exits and rotations."""
import math
from datetime import datetime

from core.utils.price_limit import limit_down_price, limit_up_price, round_price


def quote_error(quote, trade_date, *, side="sell", reference_time="", max_age=90):
    if not quote or quote.get("is_stale") is not False:
        return "行情缺失或时效未验证"
    if str(quote.get("date") or "").replace("-", "") != str(trade_date):
        return "行情日期不匹配"
    try:
        price = float(quote.get("last_price") or 0)
        stamp = datetime.strptime(str(quote.get("time") or "")[:8], "%H:%M:%S")
        if not math.isfinite(price) or price <= 0:
            return "行情价格无效"
        if reference_time:
            now = datetime.strptime(str(reference_time)[:8], "%H:%M:%S")
            if abs((now-stamp).total_seconds()) > max_age:
                return "新旧行情不在同一时点"
    except (ValueError, TypeError):
        return "缺少可信行情时间"
    if quote.get("suspended") or quote.get("is_suspended"):
        return "停牌不能模拟成交"
    try:
        previous = float(quote.get("pre_close") or 0)
    except (ValueError, TypeError):
        return "昨收价格无效"
    if not math.isfinite(previous) or previous <= 0:
        return "缺少昨收，无法校验涨跌停"
    limit = (limit_down_price if side == "sell" else limit_up_price)(previous, quote.get("code", ""), quote.get("name", ""))
    if limit and ((side == "sell" and price <= float(limit) * 1.002) or (side == "buy" and price >= float(limit) * 0.998)):
        return "接近涨跌停，缺少可成交证据"
    return ""


def execution_price(quote, *, side, reference_time, slippage):
    """Return a conservative paper price only after the intended execution time."""
    stamp = datetime.strptime(str(quote["time"])[:8], "%H:%M:%S")
    intended = datetime.strptime(str(reference_time)[:8], "%H:%M:%S")
    if stamp < intended:
        raise ValueError("成交报价早于预定成交时间")
    last = float(quote["last_price"])
    price = round_price(last * (1 + slippage if side == "buy" else 1 - slippage))
    previous = float(quote.get("pre_close") or 0)
    limit = (limit_up_price if side == "buy" else limit_down_price)(
        previous, quote.get("code", ""), quote.get("name", "")
    )
    if limit and ((side == "buy" and price >= limit) or (side == "sell" and price <= limit)):
        raise ValueError("滑点后价格触及涨跌停，无法模拟成交")
    return price
