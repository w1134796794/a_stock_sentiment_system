"""
使用示例 - 展示如何调用各模块进行自定义分析
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import TUSHARE_TOKEN, CACHE_DIR, OUTPUT_DIR, INDUSTRY_MAPPING_FILE
from core.data.data_manager import DataManager
from core.data.industry_mapper import IndustryMapper
from core.analysis.pattern_recognition import PatternRecognition
from core.analysis.emotion_cycle_engine import EmotionCycleEngine
# 使用新版同花顺板块追踪器
from core.analysis.ths_sector_tracker import THSSectorTracker as SectorRotationTracker

# 初始化
dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
emotion_engine = EmotionCycleEngine()
sector_tracker = SectorRotationTracker(dm)

# 示例1: 获取今日涨停池并分析
date = "20260321"
print(f"=== 分析日期: {date} ===\n")

zt_pool = dm.get_limit_up_pool(date)
print(f"涨停股票数: {len(zt_pool)}")

if not zt_pool.empty:
    print(zt_pool[['代码', '名称', '涨跌幅']].head())
    
    # 构建层级映射
    hierarchy = mapper.build_hierarchy_dataframe(zt_pool)
    print("\n=== 概念板块分析Top5 ===")
    # 使用新的带交叉验证的分析方法
    mainline = sector_tracker.analyze_with_validation(date, top_n=10)
    if not mainline.empty:
        print(mainline[['板块名称', '涨停家数', '综合评分', '信号类型', '共振得分']].head())
