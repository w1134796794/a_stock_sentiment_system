"""盘后短线增强数据 Mixin。

所有接口按交易日先读磁盘缓存，未命中时才访问 Tushare。页面、筛选和回测
不直接使用这些方法，而是消费 DataPrep 生成的银层数据。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd
from loguru import logger


class ShortSignalDataManager:
    """资金流、热榜、开盘啦、融资和事件数据。"""

    _SIGNAL_APIS: Dict[str, Dict[str, Any]] = {
        "moneyflow_ths": {"method": "moneyflow_ths"},
        "moneyflow_dc": {"method": "moneyflow_dc"},
        "sector_moneyflow_ths": {"method": "moneyflow_cnt_ths"},
        "ths_hot": {"method": "ths_hot", "kwargs": {"market": "热股"}},
        "dc_hot": {"method": "dc_hot", "kwargs": {"market": "A股市场"}},
        "kpl_list": {"method": "kpl_list"},
        "margin_detail": {"method": "margin_detail"},
        "block_trade": {"method": "block_trade"},
    }

    def _get_daily_signal(self, signal: str, trade_date: str) -> pd.DataFrame:
        cfg = self._SIGNAL_APIS[signal]
        date = str(trade_date)
        cache_dir = Path(self.cache_dir) / "signals" / signal
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{date}.csv"
        if cache_file.exists():
            try:
                return pd.read_csv(cache_file, dtype={"ts_code": str, "trade_date": str})
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{signal}] 缓存读取失败，将重新获取 {date}: {exc}")
        if self.ts_pro is None:
            return pd.DataFrame()
        try:
            method = getattr(self.ts_pro, str(cfg["method"]))
            kwargs = dict(cfg.get("kwargs") or {})
            kwargs["trade_date"] = date
            frame = method(**kwargs)
            frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
            if not frame.empty:
                frame.to_csv(cache_file, index=False, encoding="utf-8-sig")
            logger.info(f"[{signal}] {date} 获取 {len(frame)} 条")
            return frame
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{signal}] {date} 获取失败: {exc}")
            return pd.DataFrame()

    def get_moneyflow_ths(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("moneyflow_ths", trade_date)

    def get_moneyflow_dc(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("moneyflow_dc", trade_date)

    def get_sector_moneyflow_ths(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("sector_moneyflow_ths", trade_date)

    def get_ths_hot(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("ths_hot", trade_date)

    def get_dc_hot(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("dc_hot", trade_date)

    def get_kpl_list(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("kpl_list", trade_date)

    def get_margin_detail(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("margin_detail", trade_date)

    def get_block_trade(self, trade_date: str) -> pd.DataFrame:
        return self._get_daily_signal("block_trade", trade_date)
