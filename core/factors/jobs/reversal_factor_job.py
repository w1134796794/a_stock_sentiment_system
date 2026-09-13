"""Point-in-time limit-event structures, persisted once per trading day."""
from __future__ import annotations

import json
import math

import pandas as pd

from core.factors.jobs.gold_utils import read_recent_trade_dates, write_replace_partition
from core.utils.price_limit import is_st_stock, limit_down_price, limit_up_price


def build_reversal_factors(daily: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    columns = ["trade_date", "code", "stk_limit_pullback", "stk_limit_reversal",
               "stk_pullback_contraction", "stk_reversal_recovery", "reversal_structures"]
    records = []
    if daily.empty:
        return pd.DataFrame({col: pd.Series(dtype="string" if col in {"trade_date", "code", "reversal_structures"}
                                           else "float64") for col in columns})
    data = daily.copy()
    data["trade_date"] = data["trade_date"].astype(str)
    data = data[data["trade_date"] <= trade_date]
    for code, history in data.groupby("code", sort=False):
        h = history.sort_values("trade_date").drop_duplicates("trade_date").tail(80).reset_index(drop=True)
        if len(h) < 21 or str(h.iloc[-1]["trade_date"]) != trade_date:
            continue
        for col in ("open", "high", "low", "close", "pre_close", "vol_hand"):
            h[col] = pd.to_numeric(h[col], errors="coerce")
        if h[["open", "high", "low", "close", "pre_close", "vol_hand"]].isna().any().any():
            continue
        if (h[["open", "high", "low", "close", "pre_close"]] <= 0).any().any():
            continue
        today = h.iloc[-1]
        name = str(today.get("name") or "")
        if is_st_stock(name) or float(today["vol_hand"]) <= 0:
            continue
        # Do not compare unadjusted supports across a corporate-action discontinuity.
        discontinuity = (h["pre_close"] / h["close"].shift(1) - 1).abs()
        if (discontinuity.tail(20) > 0.005).any():
            continue
        tr = pd.concat([h.high - h.low, (h.high-h.pre_close).abs(),
                        (h.low-h.pre_close).abs()], axis=1).max(axis=1)
        atr = float(tr.tail(14).mean())
        if not math.isfinite(atr) or atr <= 0:
            continue
        up, down = {}, {}
        for index in range(max(0, len(h)-16), len(h)):
            r = h.iloc[index]
            up[index] = abs(float(r.close)-float(limit_up_price(r.pre_close, code, name))) < 0.0051
            down[index] = float(r.low) <= float(limit_down_price(r.pre_close, code, name))+0.0051
        structures = {}
        contraction = recovery = 0.0
        for index in reversed(range(max(20, len(h)-16), len(h)-1)):
            if not up[index]:
                continue
            event = h.iloc[index]
            before = h.iloc[index-20:index]
            platform = float(before.high.max())
            # The platform and event body are known when the limit-up event closes.
            support = platform if float(event.close) > platform and float(event.open) <= platform else float((event.open+event.close)/2)
            after = h.iloc[index+1:]
            contraction = float(after.vol_hand.mean()/max(float(event.vol_hand), 1))
            peak = float(h.iloc[index:].high.max())
            intact = float(after.close.min()) >= support - 0.5*atr
            if intact and float(today.close) < peak-0.3*atr and abs(float(today.close)-support) <= 1.5*atr:
                structures["limit_pullback"] = {
                    "event_date": str(event.trade_date), "as_of_date": trade_date,
                    "reference_close": float(today.close), "support": support,
                    "support_low": support-0.35*atr, "support_high": support+0.35*atr,
                    "protection": support-0.65*atr, "atr": atr, "resistance": peak,
                    "support_source": "突破平台" if support == platform else "涨停阳线中部",
                }
            break
        for index in reversed(range(max(20, len(h)-3), len(h))):
            if not down[index]:
                continue
            event = h.iloc[index]
            floor = float(event.low)
            target = float(event.open) if float(event.open-event.close) > atr*0.1 else float(event.pre_close)
            recovery = (float(today.close)-float(event.close))/max(float(event.pre_close-event.close), 0.01)
            consecutive = index > 0 and down.get(index-1, False)
            if not consecutive and float(h.iloc[index:].close.min()) >= floor and float(today.close) <= target+atr:
                structures["limit_reversal"] = {
                    "event_type": "收盘跌停" if abs(float(event.close)-float(limit_down_price(event.pre_close, code, name))) < 0.0051 else "盘中触及跌停",
                    "event_date": str(event.trade_date), "as_of_date": trade_date,
                    "reference_close": float(today.close), "target": target,
                    "midpoint": float((event.open+event.close)/2),
                    "protection": floor-0.2*atr, "atr": atr,
                    "event_low": floor, "event_pre_close": float(event.pre_close),
                    "target_label": "阴线实体上沿" if target == float(event.open) else "跌停前收盘价",
                }
            break
        records.append({"trade_date": trade_date, "code": str(code),
                        "stk_limit_pullback": 100.0 if "limit_pullback" in structures else 0.0,
                        "stk_limit_reversal": 100.0 if "limit_reversal" in structures else 0.0,
                        "stk_pullback_contraction": max(0.0, min(100.0, (1.5-contraction)*100)),
                        "stk_reversal_recovery": max(0.0, min(100.0, recovery*100)),
                        "reversal_structures": json.dumps(structures, ensure_ascii=False)})
    frame = pd.DataFrame(records, columns=columns)
    return frame.astype({col: "string" if col in {"trade_date", "code", "reversal_structures"}
                         else "float64" for col in columns})


def run_reversal_factors(con, trade_date: str) -> int:
    daily = read_recent_trade_dates(con, "stock_daily_silver", trade_date, days=80)
    frame = build_reversal_factors(daily, str(trade_date))
    return write_replace_partition(con, "factor_reversal_stock_wide", frame,
                                   where="trade_date = ?", params=[str(trade_date)])
