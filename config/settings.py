"""
A股短线情绪量化系统 - 配置文件
"""
import os
from pathlib import Path

# 基础路径
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = BASE_DIR / "output"

# API配置
TUSHARE_TOKEN = "9cb1d6a7b3a5cabf27023a45b0996e1f8e0c1e2676228278aed56cc7"  # 请替换为您的token
AKSHARE_ENABLE = True

# 数据获取配置
TRADE_HOUR = 15
TRADE_MINUTE = 35
HISTORY_DAYS = 60  # 回溯天数
LIMIT_UP_THRESHOLD = 0.095  # 涨停阈值（9.5%）

# 行业映射配置
INDUSTRY_MAPPING_FILE = DATA_DIR / "L2_L3_Mapping.xlsx"

# 核心标的筛选条件
CORE_STOCK_FILTER = {
    "limit_up_lookback": 20,  # 近20个交易日
    "volume_ratio_threshold": 2.0,  # 成交量放大倍数
    "ma20_trend_days": 5,  # MA20趋势判断天数
}

# 情绪计算权重
SENTIMENT_WEIGHTS = {
    "limit_up_count": 0.6,  # 涨停家数权重
    "continuing_board_height": 0.4,  # 连板高度权重
}

# 弱转强识别参数
WEAK_TO_STRONG = {
    "yesterday_zt_board": True,  # 昨日烂板/炸板
    "today_gap_up": 0.02,  # 今日跳空高开2%
    "volume_increase": 1.5,  # 成交量放大1.5倍
}

# 龙回头参数
DRAGON_PULLBACK = {
    "min_boards": 3,  # 最少连板数
    "pullback_ma": ["MA10", "MA20"],  # 回落均线支撑
    "volume_shrink": 0.6,  # 成交量萎缩至前期60%以下
    "rebound_volume": 1.2,  # 反弹放量1.2倍
}

# 日志配置
LOG_CONFIG = {
    "level": "INFO",
    "format": "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    "rotation": "1 day",
    "retention": "30 days",
}
