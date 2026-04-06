"""
使用示例 - 展示如何调用各模块进行自定义分析
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import TUSHARE_TOKEN, CACHE_DIR, OUTPUT_DIR, INDUSTRY_MAPPING_FILE
from core.data.data_manager import DataManager
from core.data.industry_mapper import IndustryMapper
from core.analysis.sentiment_engine import SentimentEngine
from core.analysis.pattern_recognition import PatternRecognition

# 初始化
dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
engine = SentimentEngine()

# 示例1: 获取今日涨停池并分析
date = "20260321"
print(f"=== 分析日期: {date} ===\n")

zt_pool = dm.get_limit_up_pool(date)
print(f"涨停股票数: {len(zt_pool)}")

if not zt_pool.empty:
    print(zt_pool[['代码', '名称', '涨跌幅']].head())
    
    # 构建层级映射
    hierarchy = mapper.build_hierarchy_dataframe(zt_pool)
    print("\n=== 主线板块Top5 ===")
    mainline = engine.calculate_mainline_strength(hierarchy)
    if not mainline.empty:
        print(mainline[['L3_Industry', 'LimitUp_Count', 'Strength_Score']].head())
