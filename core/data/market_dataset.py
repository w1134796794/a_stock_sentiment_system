"""当日只读数据集（Phase 1 数据解耦的载体）。

`MarketDataset` 是一次性预取好的「当日分析所需数据」的内存容器：分析开始前由
``DataPrep`` 用 ``DataManager`` 批量拉齐，之后业务层（策略 / 大盘 / 情绪 / 板块）
只通过 ``StockRepository`` 只读访问它，不再在计算过程中直连数据接口。

设计要点：
- 纯数据容器 + 少量切片访问器，不含取数逻辑（取数在 DataPrep）。
- 键的形态与上游调用方一致（个股代码沿用调用方传入的 ts_code 字符串），
  保证 Repository 能 drop-in 替换原 ``dm.xxx()`` 调用。
- 字段可增量填充：DataPrep 先填哪些，Repository 严格模式就只放行哪些。
- 可选落盘（``save_dir``/``load_dir``）以实现「同一份本地数据 → 同一结果」的离线复现。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


def _auction_key(code: str, date: str) -> str:
    return f"{code}|{date}"


def call_key(domain: str, **params: Any) -> str:
    """通用「调用键」：domain + 规范化后的命名参数（按键名排序，保证两端一致）。

    供 ``StockRepository``（读）与 ``DataPrep``（预取写）共用，避免键拼接漂移导致命不中。
    """
    inner = "|".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{domain}|{inner}" if inner else domain


@dataclass
class MarketDataset:
    """一日分析所需的全部预取数据（只读消费）。"""

    trade_date: str = ""
    prev_trade_date: str = ""

    # code -> 个股历史日线（含 'trade_date' 列，按日期升序），窗口=预取的最大回溯
    daily: Dict[str, pd.DataFrame] = field(default_factory=dict)
    # daily 预取覆盖的统一窗口 [daily_start, daily_end]（YYYYMMDD）；用于判断单次查询是否在窗内
    daily_start: str = ""
    daily_end: str = ""
    # date(YYYYMMDD) -> 全市场日线
    all_daily: Dict[str, pd.DataFrame] = field(default_factory=dict)
    # date(YYYYMMDD) -> 全市场每日基本面(daily_basic)
    daily_basic: Dict[str, pd.DataFrame] = field(default_factory=dict)
    # "code|date" -> 竞价 dict
    auction: Dict[str, dict] = field(default_factory=dict)
    # code -> 所属板块（行业/概念名）
    sector_map: Dict[str, str] = field(default_factory=dict)
    # date(YYYYMMDD) -> 涨停池 / 跌停池
    limit_up: Dict[str, pd.DataFrame] = field(default_factory=dict)
    limit_down: Dict[str, pd.DataFrame] = field(default_factory=dict)

    # 通用「调用键 -> 结果」缓存：用于板块/资金流等签名各异、按 (domain, 参数) 预取/记忆化的域
    # （如 ths_index/ths_daily/limit_cpt_list/moneyflow_summary/index_daily）。键由 ``call_key`` 生成。
    calls: Dict[str, Any] = field(default_factory=dict)

    # 哪些「数据域」已被 DataPrep 填充（供 Repository 判断严格模式是否放行）
    # 取值示例：{"daily", "auction", "all_daily", "sector_map", "limit_up", "ths_index", ...}
    prefetched: set = field(default_factory=set)
    meta: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 访问器（被 StockRepository 使用；返回 None/空表示「数据集未命中」）
    # ------------------------------------------------------------------
    def has_domain(self, domain: str) -> bool:
        return domain in self.prefetched

    # 通用调用缓存访问器（被 StockRepository 读、DataPrep/Repository 写）
    def has_call(self, key: str) -> bool:
        return key in self.calls

    def get_call(self, key: str) -> Any:
        return self.calls.get(key)

    def put_call(self, key: str, value: Any, domain: str = "") -> None:
        self.calls[key] = value
        if domain:
            self.prefetched.add(domain)

    def daily_covers(self, start_date: str, end_date: str) -> bool:
        """请求窗口 [start_date, end_date] 是否完全落在已预取的 daily 窗口内。

        只有完全覆盖时，数据集切片才与 ``dm.get_stock_daily`` 等价；否则应回退 dm，
        避免「截断切片」导致少行而与直连结果不一致。
        """
        if not self.daily_start or not self.daily_end:
            return False
        try:
            s = pd.to_datetime(str(start_date))
            e = pd.to_datetime(str(end_date))
            ws = pd.to_datetime(self.daily_start)
            we = pd.to_datetime(self.daily_end)
        except Exception:
            return False
        return ws <= s and e <= we

    def get_daily_window(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """切片返回 [start_date, end_date] 区间的个股日线；未预取该 code 返回 None。"""
        df = self.daily.get(code)
        if df is None:
            return None
        if "trade_date" not in df.columns or df.empty:
            return df.iloc[0:0].copy()
        td = pd.to_datetime(df["trade_date"])
        s = pd.to_datetime(str(start_date))
        e = pd.to_datetime(str(end_date))
        mask = (td >= s) & (td <= e)
        return df.loc[mask].copy()

    def get_daily_on(self, code: str, date: str) -> Optional[pd.DataFrame]:
        """单日个股日线（0 或 1 行）；未预取该 code 返回 None。"""
        return self.get_daily_window(code, date, date)

    def get_all_daily(self, date: str) -> Optional[pd.DataFrame]:
        return self.all_daily.get(str(date))

    def get_daily_basic(self, date: str) -> Optional[pd.DataFrame]:
        return self.daily_basic.get(str(date))

    def get_auction(self, code: str, date: str) -> Optional[dict]:
        return self.auction.get(_auction_key(code, str(date)))

    def get_sector(self, code: str) -> Optional[str]:
        return self.sector_map.get(code)

    def get_limit_up(self, date: str) -> Optional[pd.DataFrame]:
        return self.limit_up.get(str(date))

    def get_limit_down(self, date: str) -> Optional[pd.DataFrame]:
        return self.limit_down.get(str(date))

    # ------------------------------------------------------------------
    # 填充器（被 DataPrep 使用）
    # ------------------------------------------------------------------
    def put_daily(self, code: str, df: pd.DataFrame) -> None:
        self.daily[code] = df
        self.prefetched.add("daily")

    def put_auction(self, code: str, date: str, data: dict) -> None:
        self.auction[_auction_key(code, str(date))] = data
        self.prefetched.add("auction")

    def set_sector_map(self, mapping: Dict[str, str]) -> None:
        self.sector_map.update(mapping or {})
        self.prefetched.add("sector_map")

    def summary(self) -> str:
        return (f"MarketDataset({self.trade_date}) "
                f"daily={len(self.daily)} all_daily={len(self.all_daily)} "
                f"auction={len(self.auction)} sector_map={len(self.sector_map)} "
                f"limit_up={len(self.limit_up)} prefetched={sorted(self.prefetched)}")

    # ------------------------------------------------------------------
    # 可选落盘 / 加载（离线复现）。daily 等 DataFrame 走 parquet（无引擎则降级 csv）。
    # ------------------------------------------------------------------
    def save_dir(self, out_dir: Path) -> None:
        out_dir = Path(out_dir)
        (out_dir / "daily").mkdir(parents=True, exist_ok=True)
        for code, df in self.daily.items():
            self._write_frame(df, out_dir / "daily" / f"{code}")
        for date, df in self.all_daily.items():
            (out_dir / "all_daily").mkdir(parents=True, exist_ok=True)
            self._write_frame(df, out_dir / "all_daily" / f"{date}")
        for date, df in self.limit_up.items():
            (out_dir / "limit_up").mkdir(parents=True, exist_ok=True)
            self._write_frame(df, out_dir / "limit_up" / f"{date}")
        meta = {
            "trade_date": self.trade_date,
            "prev_trade_date": self.prev_trade_date,
            "prefetched": sorted(self.prefetched),
            "sector_map": self.sector_map,
            "auction": self.auction,
            "meta": self.meta,
        }
        (out_dir / "index.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_frame(df: pd.DataFrame, base: Path) -> None:
        try:
            df.to_parquet(base.with_suffix(".parquet"), index=False)
        except Exception:  # 无 pyarrow/fastparquet 时降级 csv
            df.to_csv(base.with_suffix(".csv"), index=False)