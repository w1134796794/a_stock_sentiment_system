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

        # P2-3：缓存上限提升 + 命中率统计
        self._memory_cache_max_size = 200
        self._batch_cache_max_size = 100
        self._cache_hits = 0
        self._cache_misses = 0

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

        self.moneyflow_dir = self.cache_dir / "moneyflow"
        (self.moneyflow_dir / "stock").mkdir(parents=True, exist_ok=True)
        (self.moneyflow_dir / "hsgt").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "top_list").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "cyq_perf").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "signals").mkdir(parents=True, exist_ok=True)

    def _get_from_memory_cache(self, cache_key: str) -> Optional[pd.DataFrame]:
        """从内存缓存获取数据"""
        if cache_key in self._memory_cache:
            timestamp = self._cache_timestamp.get(cache_key, 0)
            if time.time() - timestamp < self._cache_ttl:
                self._cache_hits += 1
                return self._memory_cache[cache_key]
            else:
                del self._memory_cache[cache_key]
                del self._cache_timestamp[cache_key]
        self._cache_misses += 1
        return None

    def _set_memory_cache(self, cache_key: str, data: pd.DataFrame):
        """设置内存缓存"""
        self._memory_cache[cache_key] = data
        self._cache_timestamp[cache_key] = time.time()

        if len(self._memory_cache) > self._memory_cache_max_size:
            oldest_key = min(self._cache_timestamp, key=self._cache_timestamp.get)
            del self._memory_cache[oldest_key]
            del self._cache_timestamp[oldest_key]

    def _get_from_batch_cache(self, cache_key: str) -> Optional[pd.DataFrame]:
        """从批量缓存获取数据"""
        if cache_key in self._batch_cache:
            timestamp = self._batch_cache_timestamp.get(cache_key, 0)
            if time.time() - timestamp < self._batch_cache_ttl:
                self._cache_hits += 1
                return self._batch_cache[cache_key]
            else:
                del self._batch_cache[cache_key]
                del self._batch_cache_timestamp[cache_key]
        self._cache_misses += 1
        return None

    def _set_batch_cache(self, cache_key: str, data: pd.DataFrame):
        """设置批量缓存"""
        self._batch_cache[cache_key] = data
        self._batch_cache_timestamp[cache_key] = time.time()

        if len(self._batch_cache) > self._batch_cache_max_size:
            oldest_key = min(self._batch_cache_timestamp, key=self._batch_cache_timestamp.get)
            del self._batch_cache[oldest_key]
            del self._batch_cache_timestamp[oldest_key]

    # =========================================================================
    # P2-3：统一缓存框架
    # =========================================================================

    def get_or_fetch(self, cache_key: str, fetcher,
                     ttl: Optional[int] = None,
                     force_refresh: bool = False,
                     use_batch_cache: bool = False) -> pd.DataFrame:
        """
        统一缓存入口：命中缓存则返回，否则调用 fetcher() 并写回缓存。

        Args:
            cache_key:      唯一缓存键
            fetcher:        无参回调，返回 pd.DataFrame
            ttl:            自定义 TTL（秒）；为 None 时使用默认值
            force_refresh:  强制跳过缓存重新拉取
            use_batch_cache: 使用更长 TTL 的批量缓存而不是普通缓存

        Returns:
            pd.DataFrame
        """
        cache_get = self._get_from_batch_cache if use_batch_cache else self._get_from_memory_cache
        cache_set = self._set_batch_cache if use_batch_cache else self._set_memory_cache

        if not force_refresh:
            cached = cache_get(cache_key)
            if cached is not None:
                if ttl is None:
                    return cached
                ts = (self._batch_cache_timestamp if use_batch_cache else self._cache_timestamp).get(cache_key, 0)
                if time.time() - ts < ttl:
                    return cached

        data = fetcher()
        if data is not None and not (isinstance(data, pd.DataFrame) and data.empty):
            cache_set(cache_key, data)
        return data if data is not None else pd.DataFrame()

    def cache_stats(self) -> dict:
        """返回缓存命中率统计"""
        total = self._cache_hits + self._cache_misses
        return {
            'memory_cache_size': len(self._memory_cache),
            'batch_cache_size': len(self._batch_cache),
            'memory_cache_max': self._memory_cache_max_size,
            'batch_cache_max': self._batch_cache_max_size,
            'hits': self._cache_hits,
            'misses': self._cache_misses,
            'hit_rate': round(self._cache_hits / total * 100, 2) if total > 0 else 0.0,
        }

    def invalidate_cache(self, key_prefix: Optional[str] = None) -> int:
        """
        失效内存缓存。

        Args:
            key_prefix: 可选前缀；为 None 时清空所有内存缓存

        Returns:
            int: 被清理的键数
        """
        cleared = 0
        if key_prefix is None:
            cleared = len(self._memory_cache) + len(self._batch_cache)
            self._memory_cache.clear()
            self._cache_timestamp.clear()
            self._batch_cache.clear()
            self._batch_cache_timestamp.clear()
            return cleared

        for store, ts_store in ((self._memory_cache, self._cache_timestamp),
                                (self._batch_cache, self._batch_cache_timestamp)):
            keys = [k for k in store if k.startswith(key_prefix)]
            for k in keys:
                store.pop(k, None)
                ts_store.pop(k, None)
                cleared += 1
        return cleared

    def invalidate_disk_cache(self, pattern: str) -> int:
        """
        按 glob 模式失效磁盘缓存文件。

        Args:
            pattern: 相对于 cache_dir 的 glob 模式，如 "stock/daily/000001*.csv"

        Returns:
            int: 被删除的文件数
        """
        deleted = 0
        for f in self.cache_dir.glob(pattern):
            try:
                if f.is_file():
                    f.unlink()
                    deleted += 1
            except Exception as e:
                logger.warning(f"[invalidate_disk_cache] 删除 {f} 失败: {e}")
        if deleted:
            logger.info(f"[invalidate_disk_cache] 已删除 {deleted} 个文件 (pattern={pattern})")
        return deleted

    def clear_memory_cache(self):
        """清理内存缓存（保留以向后兼容；新代码请使用 invalidate_cache()）"""
        self.invalidate_cache()
        logger.info("[缓存管理] 内存缓存已清理")
