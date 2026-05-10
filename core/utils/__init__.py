"""
工具模块 - 提供股票分析系统的通用工具函数和类

使用示例:
    # 方式1: 直接导入工具类（推荐）
    from core.utils import DateUtils, StockCodeUtils, TimeUtils, CalculationUtils, ValidationUtils

    # 日期处理
    date_utils = DateUtils()
    prev_date = date_utils.get_prev_trade_date('20260418')

    # 股票代码处理
    code = StockCodeUtils.standardize_code('000001')  # 返回 '000001.SZ'

    # 时间处理
    minutes = TimeUtils.time_to_minutes('09:30:00')  # 返回 570

    # 数据验证
    if ValidationUtils.is_limit_up('U'):
        print("涨停!")

    # 方式2: 向后兼容 - 直接导入函数
    from core.utils import standardize_code, time_to_minutes, is_limit_up
"""

# 导出工具类
from .date_utils import DateUtils
from .stock_code_utils import StockCodeUtils, FieldNames, DataFrameFieldMapper
from .time_utils import TimeUtils
from .calculation_utils import CalculationUtils
from .validation_utils import ValidationUtils

# 向后兼容 - 日期工具函数
from .date_utils import (
    parse_date,
    format_date,
    get_today_str,
    get_date_range,
    is_weekend,
    get_last_n_trade_dates,
    is_trade_date,
    get_nearest_trade_date,
    get_prev_trade_date,
    get_next_trade_date,
)

# 向后兼容 - 股票代码工具函数
from .stock_code_utils import (
    standardize_code,
    remove_suffix,
    get_exchange,
    is_shanghai_stock,
    is_shenzhen_stock,
    is_beijing_stock,
    is_chuangyeban,
    is_kechuangban,
    is_zhongxiaoban,
    is_zhuban,
    batch_standardize,
    to_akshare_symbol,
    from_akshare_symbol,
    is_valid_code,
    extract_code_from_text,
)

# 向后兼容 - 时间工具函数
from .time_utils import (
    time_to_minutes,
    minutes_to_time,
    format_time,
    minutes_from_market_open,
    minutes_to_market_close,
    is_in_trading_hours,
    is_in_auction,
    is_morning_session,
    is_afternoon_session,
    compare_time,
    is_time_before,
    is_time_after,
    is_time_between,
    get_time_period,
    parse_time_range,
)

# 向后兼容 - 计算工具函数
from .calculation_utils import (
    calculate_gap,
    calculate_drawdown,
    calculate_volume_ratio,
    calculate_confidence_score,
    calculate_moving_average,
    calculate_rsi,
    calculate_score,
    normalize_value,
    calculate_position_size,
    calculate_stop_loss,
    calculate_take_profit,
    calculate_risk_reward_ratio,
)

# 向后兼容 - 验证工具函数
from .validation_utils import (
    is_limit_up,
    is_limit_down,
    is_broken_limit,
    is_yizi_board,
    is_miaoban,
    is_late_board,
    is_broken_board,
    is_lanban,
    is_high_turnover,
    is_low_turnover,
    is_gap_up,
    is_gap_down,
    is_strong_auction,
    is_sector_leader,
    is_market_leader,
    classify_board_height,
    is_valid_weak_quality,
    is_valid_turnover,
    is_trade_date as validate_trade_date,
    validate_price_data,
    is_data_fresh,
)

__all__ = [
    # 工具类（推荐）
    'DateUtils',
    'StockCodeUtils',
    'TimeUtils',
    'CalculationUtils',
    'ValidationUtils',

    # 日期工具函数
    'parse_date',
    'format_date',
    'get_today_str',
    'get_date_range',
    'is_weekend',
    'get_last_n_trade_dates',
    'is_trade_date',
    'get_nearest_trade_date',
    'get_prev_trade_date',
    'get_next_trade_date',

    # 股票代码工具函数
    'standardize_code',
    'remove_suffix',
    'get_exchange',
    'is_shanghai_stock',
    'is_shenzhen_stock',
    'is_beijing_stock',
    'is_chuangyeban',
    'is_kechuangban',
    'is_zhongxiaoban',
    'is_zhuban',
    'batch_standardize',
    'to_akshare_symbol',
    'from_akshare_symbol',
    'is_valid_code',
    'extract_code_from_text',

    # 时间工具函数
    'time_to_minutes',
    'minutes_to_time',
    'format_time',
    'minutes_from_market_open',
    'minutes_to_market_close',
    'is_in_trading_hours',
    'is_in_auction',
    'is_morning_session',
    'is_afternoon_session',
    'compare_time',
    'is_time_before',
    'is_time_after',
    'is_time_between',
    'get_time_period',
    'parse_time_range',

    # 计算工具函数
    'calculate_gap',
    'calculate_drawdown',
    'calculate_volume_ratio',
    'calculate_confidence_score',
    'calculate_moving_average',
    'calculate_rsi',
    'calculate_score',
    'normalize_value',
    'calculate_position_size',
    'calculate_stop_loss',
    'calculate_take_profit',
    'calculate_risk_reward_ratio',

    # 验证工具函数
    'is_limit_up',
    'is_limit_down',
    'is_broken_limit',
    'is_yizi_board',
    'is_miaoban',
    'is_late_board',
    'is_broken_board',
    'is_lanban',
    'is_high_turnover',
    'is_low_turnover',
    'is_gap_up',
    'is_gap_down',
    'is_strong_auction',
    'is_sector_leader',
    'is_market_leader',
    'classify_board_height',
    'is_valid_weak_quality',
    'is_valid_turnover',
    'validate_trade_date',
    'validate_price_data',
    'is_data_fresh',
]
