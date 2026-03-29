"""
A股短线情绪量化系统 - 主配置文件
所有配置参数集中管理
"""
import os
from pathlib import Path

# ============================================
# 基础路径配置
# ============================================
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = BASE_DIR / "output"

# ============================================
# API配置
# ============================================
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")  # 请替换为您的token
AKSHARE_ENABLE = True

# ============================================
# 数据获取配置
# ============================================
TRADE_HOUR = 15
TRADE_MINUTE = 35
HISTORY_DAYS = 60  # 回溯天数
LIMIT_UP_THRESHOLD = 0.095  # 涨停阈值（9.5%）

# ============================================
# 行业映射配置
# ============================================
INDUSTRY_MAPPING_FILE = DATA_DIR / "Industry_Mapping.csv"

# ============================================
# 核心标的筛选条件
# ============================================
CORE_STOCK_FILTER = {
    "limit_up_lookback": 20,  # 近20个交易日
    "volume_ratio_threshold": 2.0,  # 成交量放大倍数
    "ma20_trend_days": 5,  # MA20趋势判断天数
    "core_stock_time_limit": "10:30:00",  # 核心标的封板时间限制
}

# ============================================
# 情绪计算权重
# ============================================
SENTIMENT_WEIGHTS = {
    "limit_up_count": 0.6,  # 涨停家数权重
    "continuing_board_height": 0.4,  # 连板高度权重
}

# ============================================
# 板块热度计算参数 (V2)
# ============================================
SECTOR_HEAT_WEIGHTS = {
    'today_weight': 0.35,        # 当日权重最高（T+0敏感）
    'weight_3d': 0.30,
    'weight_5d': 0.20,
    'weight_20d': 0.15,
}

SECTOR_HEAT_THRESHOLDS = {
    'explosion_threshold': 1.5,     # 当日爆发阈值（1.5倍，降低门槛）
    'acceleration_threshold': 0.25,  # 短期加速阈值（25%，降低门槛）
    'decline_3d_threshold': -0.15,   # 3日退潮阈值（-20%即预警，更灵敏）
    'decline_5d_threshold': -0.20,   # 5日退潮阈值
    'min_today_count': 2,           # 当日至少2只涨停才关注
    'explosion_min_today': 4,       # 爆发期最小今日涨停数（原来是3）
    'acceleration_min_3d': 3,       # 加速期最小3日涨停数（原来是5）
    'confirmed_min_today': 2,       # 确认期最小今日涨停数（原来是3）
    'confirmed_min_3d': 5,          # 确认期最小3日涨停数（原来是8）
    'watch_min_yesterday': 1,       # 观察期最小昨日涨停数（原来是2）
}

# ============================================
# 模式识别参数
# ============================================

# 弱转强参数
WEAK_TO_STRONG = {
    "yesterday_zt_board": True,  # 昨日烂板/炸板
    "today_gap_up": 0.02,  # 今日跳空高开2%
    "volume_increase": 1.5,  # 成交量放大1.5倍
}

# 二板定龙参数
SECOND_BOARD_DRAGON = {
    "min_first_board_time": "09:35:00",  # 首板封板时间早于9:35
    "max_open_times": 0,  # 首板无炸板
    "second_board_time": "09:40:00",  # 二板封板时间早于9:40
}

# 炸板回封参数
BLAST_RESEAL = {
    "max_open_times": 3,  # 最大炸板次数
    "min_reseal_strength": 0.7,  # 最小回封强度
}

# 龙二波参数
DRAGON_SECOND_WAVE = {
    "min_historical_boards": 3,  # 历史至少3连板
    "adjustment_days": 10,  # 调整期天数
    "volume_shrink_ratio": 0.5,  # 成交量萎缩比例
    "breakout_volume_ratio": 1.2,  # 突破放量比例
}

# 卡位板参数
POSITION_BATTLE = {
    "time_advantage_minutes": 5,  # 时间优势（分钟）
    "height_advantage": 1,  # 高度优势（板数）
}

# 龙回头参数（兼容旧配置）
DRAGON_PULLBACK = {
    "min_boards": 3,  # 最少连板数
    "pullback_ma": ["MA10", "MA20"],  # 回落均线支撑
    "volume_shrink": 0.6,  # 成交量萎缩至前期60%以下
    "rebound_volume": 1.2,  # 反弹放量1.2倍
}

# ============================================
# 散户特供参数
# ============================================
RETAIL_TRADER = {
    "min_float_market_cap": 20,  # 最小流通市值（亿）
    "max_float_market_cap": 80,  # 最大流通市值（亿）
    "max_open_times": 1,  # 最大炸板次数
    "latest_limit_up_time": "10:00:00",  # 最晚封板时间
    "min_sector_limit_up": 2,  # 板块最少涨停数
}

# ============================================
# 日志配置
# ============================================
LOG_CONFIG = {
    "level": "INFO",
    "format": "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    "rotation": "1 day",
    "retention": "30 days",
}
