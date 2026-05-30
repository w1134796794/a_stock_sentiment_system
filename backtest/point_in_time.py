"""
Point-in-time 数据访问 + 未来函数审计（B-2a）

历史重演引擎最大的风险是"未来函数"（look-ahead bias）——用了回测当日之后才知道
的信息。本模块提供两类工具：

1. ``AsOfPriceProvider``：按交易日提供 OHLC。它只查询"某一交易日"的行情
   （``get_all_stocks_daily(date)`` / ``get_stock_daily_data(code, date)``），
   结构上就不可能返回该日之后的数据。

2. ``StaticPriceProvider``：内存价格源，用 ``{date: {code: ohlc}}`` 喂入，
   便于离线回测 / 单元测试，且天然 point-in-time。

3. ``assert_no_future_data`` / ``has_future_data``：审计断言，检查任意 DataFrame
   里不含晚于 ``as_of`` 的行——给 pipeline 历史重演做防护栏。

价格 dict 统一为 ``{open, high, low, close, pre_close, vol, amount}``。
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd
import loguru

from backtest.matching_rules import normalize_code

logger = loguru.logger


def _to_yyyymmdd(value) -> str:
    """把各种日期表示统一成 YYYYMMDD 字符串，无法解析返回空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value.strip().replace("-", "").replace("/", "")
        return s[:8] if len(s) >= 8 else s
    try:
        return pd.Timestamp(value).strftime("%Y%m%d")
    except Exception:
        return str(value)


def has_future_data(df: pd.DataFrame, as_of: str, date_col: str = "trade_date") -> bool:
    """DataFrame 是否包含晚于 as_of 的行（未来数据泄漏）。"""
    if df is None or df.empty or date_col not in df.columns:
        return False
    as_of_norm = _to_yyyymmdd(as_of)
    dates = df[date_col].map(_to_yyyymmdd)
    return bool((dates > as_of_norm).any())


def assert_no_future_data(df: pd.DataFrame, as_of: str, date_col: str = "trade_date") -> None:
    """断言无未来数据；若发现晚于 as_of 的行则抛 AssertionError（审计用）。"""
    if has_future_data(df, as_of, date_col):
        bad = df[df[date_col].map(_to_yyyymmdd) > _to_yyyymmdd(as_of)]
        raise AssertionError(
            f"检测到未来函数：{len(bad)} 行数据晚于 as_of={as_of}（列 {date_col}）"
        )


class _BasePriceProvider:
    """价格源接口：day_prices(date) + ohlc(code, date)。"""

    def day_prices(self, date: str) -> Dict[str, Dict]:  # pragma: no cover - 接口
        raise NotImplementedError

    def ohlc(self, code: str, date: str) -> Optional[Dict]:
        return self.day_prices(date).get(normalize_code(code))


class AsOfPriceProvider(_BasePriceProvider):
    """基于 DataManager 的 point-in-time 价格源（按日缓存）。"""

    _FIELDS = ("open", "high", "low", "close", "pre_close", "vol", "amount", "pct_chg")

    def __init__(self, data_manager):
        self.dm = data_manager
        self._cache: Dict[str, Dict[str, Dict]] = {}

    def day_prices(self, date: str) -> Dict[str, Dict]:
        if date in self._cache:
            return self._cache[date]
        result: Dict[str, Dict] = {}
        try:
            df = self.dm.get_all_stocks_daily(trade_date=date)
        except TypeError:
            df = self.dm.get_all_stocks_daily(date)
        except Exception as e:
            logger.debug(f"[AsOfPriceProvider] {date} 取全市场行情失败: {e}")
            df = pd.DataFrame()

        if df is not None and not df.empty:
            # 审计防护：全市场日线必须全部属于该交易日
            try:
                assert_no_future_data(df, date, "trade_date")
            except AssertionError as e:
                logger.warning(f"[AsOfPriceProvider] {e}")
            for _, row in df.iterrows():
                code = normalize_code(row.get("ts_code", ""))
                if not code:
                    continue
                result[code] = {f: float(row.get(f, 0) or 0) for f in self._FIELDS}
        self._cache[date] = result
        return result

    def ohlc(self, code: str, date: str) -> Optional[Dict]:
        day = self.day_prices(date)
        c = normalize_code(code)
        if c in day:
            return day[c]
        # 单票兜底
        try:
            std = code if "." in str(code) else None
            if std is None:
                return None
            d = self.dm.get_stock_daily_data(std, date)
            if d and d.get("close", 0) > 0:
                return {f: float(d.get(f, 0) or 0) for f in self._FIELDS}
        except Exception:
            pass
        return None


class StaticPriceProvider(_BasePriceProvider):
    """内存价格源：``{date: {code: ohlc}}``，离线回测 / 测试友好。"""

    def __init__(self, data: Dict[str, Dict[str, Dict]]):
        self._data: Dict[str, Dict[str, Dict]] = {}
        for date, day in (data or {}).items():
            self._data[date] = {normalize_code(c): o for c, o in day.items()}

    def day_prices(self, date: str) -> Dict[str, Dict]:
        return self._data.get(date, {})


__all__ = [
    "AsOfPriceProvider",
    "StaticPriceProvider",
    "assert_no_future_data",
    "has_future_data",
]

