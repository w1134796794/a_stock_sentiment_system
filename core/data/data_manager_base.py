"""
数据管理器基础模块 - 缓存管理、目录结构、工具类初始化

所有子模块（market/stock/sector/concept）继承此基类，
共享缓存策略和目录结构。
"""
import time
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Optional
import loguru

from core.utils import (
    DateUtils,
    StockCodeUtils,
    TimeUtils,
    CalculationUtils,
    ValidationUtils,
)

logger = loguru.logger


class DataManagerBase:
    """数据管理器基类 - 提供缓存管理和目录结构"""

    def __init__(self, tushare_token: str, cache_dir: Path):
        self.ts_pro = None
        if tushare_token and tushare_token != "your_tushare_token_here":
            import tushare as ts
            self.ts_pro = ts.pro_api(tushare_token)

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._init_cache_structure()
        self._init_utils()
        self._init_memory_cache()

    def _init_utils(self):
        """初始化工具类（单例模式）"""
        self.date_utils = DateUtils()
        self.stock_code_utils = StockCodeUtils()
        self.time_utils = TimeUtils()
        self.calculation_utils = CalculationUtils()
        self.validation_utils = ValidationUtils()

    def _init_memory_cache(self):
        """初始化内存缓存"""
        self._memory_cache = {}
        self._cache_ttl = 300
        self._cache_timestamp = {}

        self._batch_cache = {}
        self._batch_cache_timestamp = {}
        self._batch_cache_ttl = 600

    def _init_cache_structure(self):
        """初始化缓存目录结构"""
        self.market_dir = self.cache_dir / "market"
        (self.market_dir / "daily_basic").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_up").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_down").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "rt_k").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_step").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_cpt").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "index_daily").mkdir(parents=True, exist_ok=True)

        self.stock_dir = self.cache_dir / "stock"
        (self.stock_dir / "daily").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "daily_price").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "daily_data").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "daily_basic").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "all_daily").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "tick").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "auction").mkdir(parents=True, exist_ok=True)

        self.sector_dir = self.cache_dir / "sector"
        (self.sector_dir / "ths_index").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "ths_member").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "stock_sectors").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "ths_daily").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "moneyflow").mkdir(parents=True, exist_ok=True)

        self.concept_dir = self.cache_dir / "concept"
        (self.concept_dir / "members").mkdir(parents=True, exist_ok=True)

        self.summary_dir = self.cache_dir / "summary"
        self.summary_dir.mkdir(parents=True, exist_ok=True)

    def _get_from_memory_cache(self, cache_key: str) -> Optional[pd.DataFrame]:
        """从内存缓存获取数据"""
        if cache_key in self._memory_cache:
            timestamp = self._cache_timestamp.get(cache_key, 0)
            if time.time() - timestamp < self._cache_ttl:
                return self._memory_cache[cache_key]
            else:
                del self._memory_cache[cache_key]
                del self._cache_timestamp[cache_key]
        return None

    def _set_memory_cache(self, cache_key: str, data: pd.DataFrame):
        """设置内存缓存"""
        self._memory_cache[cache_key] = data
        self._cache_timestamp[cache_key] = time.time()

        if len(self._memory_cache) > 100:
            oldest_key = min(self._cache_timestamp, key=self._cache_timestamp.get)
            del self._memory_cache[oldest_key]
            del self._cache_timestamp[oldest_key]

    def _get_from_batch_cache(self, cache_key: str) -> Optional[pd.DataFrame]:
        """从批量缓存获取数据"""
        if cache_key in self._batch_cache:
            timestamp = self._batch_cache_timestamp.get(cache_key, 0)
            if time.time() - timestamp < self._batch_cache_ttl:
                return self._batch_cache[cache_key]
            else:
                del self._batch_cache[cache_key]
                del self._batch_cache_timestamp[cache_key]
        return None

    def _set_batch_cache(self, cache_key: str, data: pd.DataFrame):
        """设置批量缓存"""
        self._batch_cache[cache_key] = data
        self._batch_cache_timestamp[cache_key] = time.time()

        if len(self._batch_cache) > 50:
            oldest_key = min(self._batch_cache_timestamp, key=self._batch_cache_timestamp.get)
            del self._batch_cache[oldest_key]
            del self._batch_cache_timestamp[oldest_key]

    def clear_memory_cache(self):
        """清理内存缓存"""
        self._memory_cache.clear()
        self._cache_timestamp.clear()
        self._batch_cache.clear()
        self._batch_cache_timestamp.clear()
        logger.info("[缓存管理] 内存缓存已清理")