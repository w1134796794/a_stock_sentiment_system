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
# 微信公众号配置
# ============================================
WECHAT_CONFIG = {
    "enabled": True,  # 是否启用公众号发布
    "app_id": os.getenv("WECHAT_APP_ID", ""),  # 公众号AppID
    "app_secret": os.getenv("WECHAT_APP_SECRET", ""),  # 公众号AppSecret
    "author": "A股情绪系统",  # 文章作者
    "preview_wx": "gh_f1c18d75c665",  # 预览微信号（测试用）
    "auto_publish": False,  # 是否自动发布（False则只生成预览）
    
    # LLM配置（用于生成描述性报告）
    "use_llm": True,  # 是否使用LLM生成报告
    "llm_api_key": os.getenv("DASHSCOPE_API_KEY", "your-api-key-here"),  # LLM API密钥
    # 支持的模型：
    # - OpenAI: "gpt-3.5-turbo", "gpt-4"
    # - 通义千问: "qwen-turbo", "qwen-plus", "qwen-max"
    "llm_model": "qwen-turbo",  # LLM模型名称
}

# ============================================
# 同花顺板块追踪器配置 (THSSectorTracker)
# ============================================
THS_SECTOR_CONFIG = {
    # 板块分析参数
    "analyze_sectors": {
        "top_n": 20,  # 默认返回前N个板块
        "use_limit_cpt": True,  # 是否使用limit_cpt_list数据
        "min_member_count": 10,  # 最小成分股数量（过滤小板块）
    },
    
    # 概念/行业差异化参数
    "sector_params": {
        "概念": {
            "min_pct_change": 5.0,      # 概念涨幅阈值更高
            "price_weight": 0.5,         # 概念价格权重更高（追热点）
            "amount_weight": 0.2,        # 概念资金权重更低
            "limit_weight": 0.3,         # 概念涨停权重
            "hot_threshold_pct": 0.15,   # 概念前15%算热点（更严格）
        },
        "行业": {
            "min_pct_change": 3.0,      # 行业涨幅阈值更低
            "price_weight": 0.35,        # 行业价格权重更低
            "amount_weight": 0.35,       # 行业资金权重更高（看资金）
            "limit_weight": 0.3,         # 行业涨停权重
            "hot_threshold_pct": 0.2,    # 行业前20%算热点
        }
    },
    
    # 板块关联分析参数
    "sector_relation": {
        "min_overlap": 0.05,  # 最小重叠度阈值（查找关联板块）
        "default_overlap": 0.1,  # 默认重叠度阈值
    },
    
    # 板块共振分析参数
    "resonance": {
        "top_n": 20,  # 分析前N个板块
        "min_overlap": 0.1,  # 最小重叠度
        "strong_resonance_threshold": 0.3,  # 强共振重叠度阈值
        "medium_resonance_threshold": 0.1,  # 中共振重叠度阈值
    },
    
    # 板块持续性分析参数
    "persistence": {
        "lookback_days": 10,  # 回溯交易日数量（增加历史数据分析天数，更准确判断持续性）
        "hot_threshold_days": 3,  # 判定为持续热门的最少天数
        "top_n": 10,  # 每日热点板块排名阈值
    },
    
    # 板块内部结构分析参数
    "internal_structure": {
        # 梯队完整性评分权重
        "hierarchy_weights": {
            "has_leader": 20,  # 有最高板
            "has_second_board": 20,  # 有2板
            "multiple_second_board": 10,  # 多个2板
            "has_third_plus": 20,  # 有3板及以上
            "first_board_count_3": 20,  # 首板>=3
            "first_board_count_5": 10,  # 首板>=5
        },
        # 龙头股评分
        "leader_score": {
            "space_leader": 10,  # 空间龙头
            "strength_leader": 10,  # 强度龙头
            "time_leader": 10,  # 时间龙头
        },
        # 中军股封单金额阈值（元）
        "mid_cap_min_amount": 100000000,  # 1亿
    },
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
