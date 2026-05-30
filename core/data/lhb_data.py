"""Sprint F-1：游资 / 龙虎榜数据 Provider

定位
====
现有 ``MoneyflowDataManager`` 已经有 ``get_top_list`` / ``get_top_inst``（龙虎榜
明细 + 机构席位）。本模块**补齐"游资视角"**所需的两个 Tushare 接口：

* ``hm_list``   游资分类名录（name / desc / orgs）   —— 5000 积分
* ``hm_detail`` 每日游资交易明细（含 hm_name / hm_orgs / net_amount） —— 10000 积分

采用**组合而非继承**：``HotMoneyDataProvider(dm)`` 复用 ``dm.ts_pro`` /
``dm.cache_dir``，不改动 DataManager 的 MRO，落地零侵入。

⚠ 积分风险（头号）
==================
``hm_detail`` 需要 10000 积分。账户积分不足时 Tushare 抛权限异常 → 本 Provider
统一吞掉并返回**空 DataFrame**，上层 ``lhb_analyzer`` 必须能在"无游资明细"时
降级到「仅 YAML 名单 + top_list 席位名」模式，保证 MVP 在低积分账户也能跑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
import loguru

logger = loguru.logger


class HotMoneyDataProvider:
    """游资数据获取器（组合 DataManager）。

    用法::

        provider = HotMoneyDataProvider(dm)
        df_list = provider.get_hm_list()              # 游资名录（基本不变，缓存长期有效）
        df_detail = provider.get_hm_detail("20260526") # 当日游资明细
    """

    def __init__(self, dm):
        self.dm = dm
        # 复用 DataManager 的缓存根目录；游资数据单独放 cache/hot_money/
        self.cache_dir: Path = Path(getattr(dm, "cache_dir", "data/cache")) / "hot_money"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -------------------- 游资名录 hm_list --------------------

    def get_hm_list(self, *, force: bool = False) -> pd.DataFrame:
        """获取游资分类名录 (Tushare ``hm_list``)。

        名录变化极慢 → 缓存为单文件 ``hm_list.csv``，默认长期复用。

        Returns:
            columns = [name, desc, orgs]；无权限 / 无 token 时返回空 DataFrame。
        """
        cache_file = self.cache_dir / "hm_list.csv"
        if cache_file.exists() and not force:
            try:
                return pd.read_csv(cache_file)
            except Exception:
                pass

        ts_pro = getattr(self.dm, "ts_pro", None)
        if ts_pro is None:
            return pd.DataFrame()

        try:
            df = ts_pro.hm_list()
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_hm_list] 游资名录: {len(df)} 个")
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            # 5000 积分不足 / 接口下线等
            logger.warning(f"[get_hm_list] 获取失败（可能积分不足 5000）: {e}")
            return pd.DataFrame()

    # -------------------- 每日游资明细 hm_detail --------------------

    def get_hm_detail(self, trade_date: str, *, force: bool = False) -> pd.DataFrame:
        """获取某交易日全市场游资交易明细 (Tushare ``hm_detail``)。

        Args:
            trade_date: ``YYYYMMDD``

        Returns:
            columns = [trade_date, ts_code, ts_name, buy_amount, sell_amount,
                       net_amount, hm_name, hm_orgs, tag]；
            无权限（积分 < 10000）/ 无 token 时返回**空 DataFrame**（上层须降级）。
        """
        cache_file = self.cache_dir / f"hm_detail_{trade_date}.csv"
        if cache_file.exists() and not force:
            try:
                return pd.read_csv(cache_file)
            except Exception:
                pass

        ts_pro = getattr(self.dm, "ts_pro", None)
        if ts_pro is None:
            return pd.DataFrame()

        try:
            df = ts_pro.hm_detail(trade_date=trade_date)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_hm_detail] {trade_date} 游资明细: {len(df)} 条")
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"[get_hm_detail] {trade_date} 获取失败（可能积分不足 10000）: {e}")
            return pd.DataFrame()

    # -------------------- 便捷：判断游资数据是否可用 --------------------

    def is_hm_available(self, probe_date: Optional[str] = None) -> bool:
        """探测当前账户是否有游资明细权限（缓存命中或一次试拉非空）。"""
        if probe_date is None:
            return (self.cache_dir / "hm_list.csv").exists()
        df = self.get_hm_detail(probe_date)
        return not df.empty


__all__ = ["HotMoneyDataProvider"]