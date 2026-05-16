"""
数据管理器统一入口 - 组合所有子模块

模块划分：
  - DataManagerBase:     缓存管理、目录结构、工具类
  - MarketDataManager:   市场数据（daily_basic, limit_up/down, rt_k, limit_cpt, limit_step）
  - StockDataManager:    个股数据（daily, tick, auction, batch）
  - SectorDataManager:   板块数据（ths_index, ths_daily, ths_member, moneyflow）
  - ConceptDataManager:  概念数据（stock_concepts, concept_members）

使用方式：
  from core.data.data_manager_main import DataManager
  dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
  dm.get_limit_up_pool('20250101')  # 市场数据
  dm.get_stock_daily('000001.SZ', ...)  # 个股数据
  dm.get_ths_index()  # 板块数据
  dm.get_stock_concepts('000001.SZ')  # 概念数据
"""
from pathlib import Path
import loguru

from core.data.data_manager_market import MarketDataManager
from core.data.data_manager_stock import StockDataManager
from core.data.data_manager_sector import SectorDataManager
from core.data.data_manager_concept import ConceptDataManager

logger = loguru.logger


class DataManager(MarketDataManager, StockDataManager, SectorDataManager, ConceptDataManager):
    """
    数据管理器统一入口

    通过多重继承组合所有子模块，对外暴露统一接口。
    兼容原有 DataManager 的所有方法签名。
    """

    def __init__(self, tushare_token: str, cache_dir: Path):
        MarketDataManager.__init__(self, tushare_token, cache_dir)
        logger.info(f"[DataManager] 初始化完成，缓存目录: {cache_dir}")


if __name__ == "__main__":
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    print("数据管理器初始化成功")
    print(f"可用方法数: {len([m for m in dir(dm) if not m.startswith('_')])}")