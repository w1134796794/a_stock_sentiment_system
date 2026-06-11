"""只读数据仓库门面（Phase 1 数据解耦的访问入口）。

业务层（策略 / 大盘 / 情绪 / 板块）通过 ``StockRepository`` 取数，方法名与原
``DataManager`` 对齐，以便从 ``self.dm.xxx()`` 近乎 drop-in 迁移到 ``self.repo.xxx()``。

两种模式：
- 迁移期（``strict=False``，默认）：数据集命中就返回；未命中则**回退** ``DataManager``
  并打 WARNING。此模式下行为与改造前完全一致 —— 用于逐模块平滑迁移 + 回归对齐。
- 收口期（``strict=True``）：未预取的数据直接 ``raise DataNotPrefetchedError``，
  强制保证「业务层只读本地、不在计算中途打 API」，从而可复现、可单测。

构造方式：
    repo = StockRepository(dataset, dm=dm)                 # 数据集 + dm 回退
    repo = StockRepository.passthrough(dm)                 # 无数据集，纯透传（迁移第0步）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import loguru
import pandas as pd

from core.data.market_dataset import MarketDataset, call_key

logger = loguru.logger


class DataNotPrefetchedError(RuntimeError):
    """严格模式下访问了未预取的数据（说明 DataPrep 的 universe/窗口需要补全）。"""


class StockRepository:
    def __init__(self,
                 dataset: Optional[MarketDataset] = None,
                 *,
                 dm: Any = None,
                 strict: bool = False):
        self.dataset = dataset if dataset is not None else MarketDataset()
        self.dm = dm
        self.strict = strict
        self._fallback_warned: set = set()

    @classmethod
    def passthrough(cls, dm: Any) -> "StockRepository":
        """无数据集的纯透传仓库：所有调用直接落到 DataManager（行为零变化）。"""
        return cls(dataset=None, dm=dm, strict=False)

    # ------------------------------------------------------------------
    # 内部：未命中处理
    # ------------------------------------------------------------------
    def _miss(self, domain: str, fetch, what: str):
        """数据集未命中：严格模式报错；否则回退 dm 并告警（每种 domain 只告警一次）。"""
        if self.strict:
            raise DataNotPrefetchedError(
                f"[Repository] 严格模式下未预取 {domain}：{what}。"
                f"请在 DataPrep 中将其纳入 universe/窗口。")
        if self.dm is None:
            raise DataNotPrefetchedError(
                f"[Repository] 未预取 {domain}：{what}，且无 dm 回退。")
        if domain not in self._fallback_warned:
            logger.warning(f"[Repository] {domain} 未命中数据集，回退 DataManager（迁移期）。"
                           f" 首次示例：{what}")
            self._fallback_warned.add(domain)
        return fetch()

    def _cached(self, domain: str, key: str, fetch, what: str):
        """带「调用键」缓存的取数（用于签名各异的板块/资金流域）。

        - 数据集已（由 DataPrep 预取或本次记忆化）命中该 key → 直接返回，不告警、不回退；
        - 未命中 → 走 ``_miss``（严格模式报错 / 迁移期回退 dm 并按域首次告警），
          再把结果**记忆化**进数据集，使后续相同调用命中、避免重复打接口。
        """
        if self.dataset.has_call(key):
            return self.dataset.get_call(key)
        result = self._miss(domain, fetch, what)
        self.dataset.put_call(key, result)
        return result

    # ------------------------------------------------------------------
    # 个股日线
    # ------------------------------------------------------------------
    def _daily_hit(self, ts_code: str, start_date: str, end_date: str) -> bool:
        """数据集是否能等价服务该 daily 查询：域已预取 + 窗口完全覆盖 + 该 code 已预取。"""
        return (self.dataset.has_domain("daily")
                and self.dataset.daily_covers(start_date, end_date)
                and ts_code in self.dataset.daily)

    def get_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        if self._daily_hit(ts_code, start_date, end_date):
            df = self.dataset.get_daily_window(ts_code, start_date, end_date)
            if df is not None:
                return df
        return self._miss("daily", lambda: self.dm.get_stock_daily(ts_code, start_date, end_date),
                          f"{ts_code} {start_date}~{end_date}")

    # 与 dm 同名别名，便于调用点 self.dm.get_stock_daily(...) → self.repo.get_stock_daily(...)
    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self.get_daily(ts_code, start_date, end_date)

    def get_daily_price(self, ts_code: str, trade_date: str) -> Dict:
        """{open, close, pre_close}；语义与 dm.get_stock_daily_price 一致。"""
        if self._daily_hit(ts_code, trade_date, trade_date):
            df = self.dataset.get_daily_on(ts_code, trade_date)
            if df is not None:
                if df.empty:
                    return {}
                row = df.iloc[0]
                return {
                    "open": float(row.get("open", 0)),
                    "close": float(row.get("close", 0)),
                    "pre_close": float(row.get("pre_close", 0)),
                }
        return self._miss("daily", lambda: self.dm.get_stock_daily_price(ts_code, trade_date),
                          f"{ts_code}@{trade_date}")

    def get_stock_daily_price(self, ts_code: str, trade_date: str) -> Dict:
        return self.get_daily_price(ts_code, trade_date)

    def get_daily_basic(self, ts_code: str, trade_date: str) -> Dict:
        if self.dataset.has_domain("daily_basic"):
            df = self.dataset.get_daily_basic(trade_date)
            if df is not None and not df.empty and "ts_code" in df.columns:
                hit = df[df["ts_code"] == ts_code]
                if not hit.empty:
                    row = hit.iloc[0]
                    return {k: row.get(k) for k in row.index}
                return {}
        return self._miss("daily_basic", lambda: self.dm.get_stock_daily_basic(ts_code, trade_date),
                          f"{ts_code}@{trade_date}")

    def get_all_stocks_daily(self, trade_date: str) -> pd.DataFrame:
        if self.dataset.has_domain("all_daily"):
            df = self.dataset.get_all_daily(trade_date)
            if df is not None:
                return df
        return self._miss("all_daily", lambda: self.dm.get_all_stocks_daily(trade_date),
                          f"@{trade_date}")

    # ------------------------------------------------------------------
    # 竞价
    # ------------------------------------------------------------------
    def get_auction_data(self, ts_code: str, trade_date: str) -> Dict:
        if self.dataset.has_domain("auction"):
            data = self.dataset.get_auction(ts_code, trade_date)
            if data is not None:
                return data
        return self._miss("auction", lambda: self.dm.get_auction_data(ts_code, trade_date),
                          f"{ts_code}@{trade_date}")

    # ------------------------------------------------------------------
    # 涨停 / 跌停池
    # ------------------------------------------------------------------
    def get_limit_up_pool(self, trade_date: str) -> pd.DataFrame:
        if self.dataset.has_domain("limit_up"):
            df = self.dataset.get_limit_up(trade_date)
            if df is not None:
                return df
        return self._miss("limit_up", lambda: self.dm.get_limit_up_pool(trade_date),
                          f"@{trade_date}")

    def get_limit_down_pool(self, trade_date: str) -> pd.DataFrame:
        if self.dataset.has_domain("limit_down"):
            df = self.dataset.get_limit_down(trade_date)
            if df is not None:
                return df
        return self._miss("limit_down", lambda: self.dm.get_limit_down_pool(trade_date),
                          f"@{trade_date}")

    # ------------------------------------------------------------------
    # 板块归属
    # ------------------------------------------------------------------
    def sector_of(self, code: str) -> Optional[str]:
        if self.dataset.has_domain("sector_map"):
            return self.dataset.get_sector(code)
        if self.strict:
            raise DataNotPrefetchedError(f"[Repository] 严格模式下未预取 sector_map：{code}")
        return None

    # ------------------------------------------------------------------
    # 以下数据域暂未纳入预取：当前一律透传 dm（行为不变）。
    # 待对应调用点稳定后，可在 DataPrep 中补预取并在此读取数据集。
    # ------------------------------------------------------------------
    def get_stock_sectors_batch(self, codes: List[str]):
        return self._miss("stock_sectors_batch",
                          lambda: self.dm.get_stock_sectors_batch(codes),
                          f"{len(codes)} codes")

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str):
        key = call_key("index_daily", ts_code=ts_code, start_date=start_date, end_date=end_date)
        return self._cached("index_daily", key,
                            lambda: self.dm.get_index_daily(ts_code, start_date, end_date),
                            f"{ts_code} {start_date}~{end_date}")

    def get_moneyflow_data(self, ts_code: str, trade_date: str):
        return self._miss("moneyflow",
                          lambda: self.dm.get_moneyflow_data(ts_code, trade_date),
                          f"{ts_code}@{trade_date}")

    def get_sector_daily(self, sector_name: str, trade_date: str):
        return self._miss("sector_daily",
                          lambda: self.dm.get_sector_daily(sector_name, trade_date),
                          f"{sector_name}@{trade_date}")

    # 同花顺板块/概念数据域（Layer2）：DataPrep 预取「列表/按日」调用，按 (domain,参数) 命中；
    # 其余按需调用（如逐板块 ths_member）走 _cached 记忆化，避免重复打接口。
    def get_ths_index(self, index_type: str = None):
        key = call_key("ths_index", index_type=index_type)
        return self._cached("ths_index", key,
                            lambda: self.dm.get_ths_index(index_type=index_type),
                            f"index_type={index_type}")

    def get_ths_member(self, ts_code: str):
        key = call_key("ths_member", ts_code=ts_code)
        return self._cached("ths_member", key,
                            lambda: self.dm.get_ths_member(ts_code),
                            f"ts_code={ts_code}")

    def get_ths_daily(self, ts_code: str = None, trade_date: str = None,
                      start_date: str = None, end_date: str = None):
        key = call_key("ths_daily", ts_code=ts_code, trade_date=trade_date,
                       start_date=start_date, end_date=end_date)
        return self._cached("ths_daily", key,
                            lambda: self.dm.get_ths_daily(ts_code=ts_code, trade_date=trade_date,
                                                          start_date=start_date, end_date=end_date),
                            key)

    def get_limit_cpt_list(self, trade_date: str):
        key = call_key("limit_cpt_list", trade_date=trade_date)
        return self._cached("limit_cpt_list", key,
                            lambda: self.dm.get_limit_cpt_list(trade_date),
                            f"@{trade_date}")

    # 与 dm 同名别名：完整日行情字典 / 每日基本面
    def get_stock_daily_data(self, *args, **kwargs):
        return self._miss("stock_daily_data", lambda: self.dm.get_stock_daily_data(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    def get_stock_daily_basic(self, ts_code: str, trade_date: str):
        return self.get_daily_basic(ts_code, trade_date)

    # 批量日线：当前透传 dm（dm 内部已有并发与缓存）。后续可基于 daily 域 + 覆盖判断改为数据集服务。
    def get_stocks_daily_batch(self, *args, **kwargs):
        return self._miss("stocks_daily_batch", lambda: self.dm.get_stocks_daily_batch(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    # 资金流 / 行情聚合 / 龙虎榜 / 北向 / 两融 / 筹码：当前透传 dm。
    def get_moneyflow_summary(self, trade_date: str):
        key = call_key("moneyflow_summary", trade_date=trade_date)
        return self._cached("moneyflow_summary", key,
                            lambda: self.dm.get_moneyflow_summary(trade_date),
                            f"@{trade_date}")

    def get_all_rt_k_data(self, *args, **kwargs):
        return self._miss("all_rt_k_data", lambda: self.dm.get_all_rt_k_data(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    def get_hot_sectors(self, *args, **kwargs):
        return self._miss("hot_sectors", lambda: self.dm.get_hot_sectors(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    def get_top_inst(self, *args, **kwargs):
        return self._miss("top_inst", lambda: self.dm.get_top_inst(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    def get_moneyflow_hsgt(self, *args, **kwargs):
        return self._miss("moneyflow_hsgt", lambda: self.dm.get_moneyflow_hsgt(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    def get_margin(self, *args, **kwargs):
        return self._miss("margin", lambda: self.dm.get_margin(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    def get_chip_data(self, *args, **kwargs):
        return self._miss("chip_data", lambda: self.dm.get_chip_data(*args, **kwargs),
                          f"args={args} kwargs={kwargs}")

    @property
    def date_utils(self):
        """交易日历工具透传（非取数，仅为统一让业务层经 repo 访问）。"""
        return getattr(self.dm, "date_utils", None)