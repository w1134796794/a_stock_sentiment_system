"""
情绪周期引擎使用示例

展示如何从DataManager获取数据，并调用EmotionCycleEngine分析情绪周期
"""
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.data_manager_main import DataManager
from core.analysis.emotion_cycle_engine import EmotionCycleEngine
from config.settings import TUSHARE_TOKEN, CACHE_DIR
import loguru

logger = loguru.logger


def analyze_today_emotion(trade_date: str = None):
    """
    分析指定日期的情绪周期
    
    Args:
        trade_date: 交易日期，格式YYYYMMDD，默认为最近交易日
    """
    # 1. 初始化数据管理器
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    
    # 2. 初始化情绪周期引擎
    engine = EmotionCycleEngine()
    
    # 3. 获取最近交易日
    if not trade_date:
        trade_date = dm.date_utils.get_nearest_trade_date(dm.today_str)
    
    logger.info(f"开始分析 {trade_date} 的情绪周期")
    
    # 4. 获取当日涨停数据
    limit_up_df = dm.get_limit_up_pool(trade_date)
    if limit_up_df.empty:
        logger.warning(f"未获取到 {trade_date} 的涨停数据")
        return None
    
    # 5. 获取当日跌停数据（核按钮）
    limit_down_df = dm.get_limit_down_pool(trade_date)
    
    # 6. 获取前天涨停数据（用于T+1溢价计算：前天涨停→昨日开盘买→今日开盘卖）
    prev_trade_date = dm.date_utils.get_prev_trade_date(trade_date)
    day_before_prev = dm.date_utils.get_prev_trade_date(prev_trade_date)
    prev_limit_up_df = dm.get_limit_up_pool(day_before_prev)
    
    # 7. 调用情绪周期分析
    result = engine.analyze_market_data(
        limit_up_df=limit_up_df,
        limit_down_df=limit_down_df,
        prev_limit_up_df=prev_limit_up_df
    )
    
    # 8. 输出分析结果
    print("\n" + "="*60)
    print(f"【{trade_date} 情绪周期分析报告】")
    print("="*60)
    
    print(f"\n📊 识别到的情绪周期: {result['cycle_name']}")
    
    strategy = result['strategy']
    print(f"\n📝 周期特征: {strategy.description}")
    print(f"🎯 策略建议: {strategy.strategy}")
    print(f"💰 仓位控制: {strategy.position}")
    print(f"⚠️  风险等级: {strategy.risk_level}")
    
    print(f"\n✅ 允许操作: {', '.join(strategy.allowed_actions)}")
    print(f"❌ 禁止操作: {', '.join(strategy.forbidden_actions)}")
    
    # 9. 输出详细指标
    metrics = result['metrics']
    print(f"\n📈 详细指标:")
    print(f"  - 涨停家数: {metrics['limit_up_count']}")
    print(f"  - 最高连板: {metrics['max_board_height']}板")
    print(f"  - 炸板率: {metrics['broken_rate']}%")
    print(f"  - 核按钮数: {metrics['nuclear_button_count']}")
    
    if metrics['prev_limit_up_premium']:
        print(f"  - 昨日涨停溢价: {metrics['prev_limit_up_premium']}%")
    
    if metrics['board_distribution']:
        print(f"\n🔥 连板分布:")
        for height in sorted(metrics['board_distribution'].keys()):
            count = metrics['board_distribution'][height]
            print(f"    {height}板: {count}只")
    
    # 10. 输出评分详情
    if result.get('scores'):
        print(f"\n📊 周期评分:")
        for cycle, score in result['scores'].items():
            cycle_name = {
                'boom': '高潮期',
                'rise': '上升期',
                'shake': '震荡期',
                'decline': '退潮期',
                'freeze': '冰点期'
            }.get(cycle, cycle)
            print(f"    {cycle_name}: {score}分")
    
    print("\n" + "="*60)
    
    return result


def analyze_emotion_with_limit_step(trade_date: str = None):
    """
    结合涨停池和连板天梯数据分析情绪周期
    
    连板天梯只包含连续涨停的股票（2板及以上），
    需要结合涨停池数据获取完整的涨停家数和首板数据
    
    Args:
        trade_date: 交易日期，格式YYYYMMDD
    """
    # 1. 初始化
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    engine = EmotionCycleEngine()
    
    # 2. 获取交易日
    if not trade_date:
        trade_date = dm.date_utils.get_nearest_trade_date(dm.today_str)
    
    logger.info(f"结合连板天梯和涨停池数据分析 {trade_date} 情绪周期")
    
    # 3. 获取涨停池数据（包含所有涨停股票）
    limit_up_df = dm.get_limit_up_pool(trade_date)
    if limit_up_df.empty:
        logger.warning(f"未获取到 {trade_date} 的涨停数据")
        return None
    
    # 4. 获取连板天梯数据（获取更精确的连板信息）
    limit_step_df = dm.get_limit_step(trade_date)
    
    # 5. 计算指标
    # 涨停家数以涨停池为准（包含首板）
    limit_up_count = len(limit_up_df)
    
    # 最高连板和分布以连板天梯为准（更精确）
    # 连板天梯字段名是'nums'不是'limit_up_nums'
    limit_nums_col = 'nums' if 'nums' in limit_step_df.columns else 'limit_up_nums'
    
    if limit_step_df.empty:
        max_board_height = 1
        board_distribution = {1: limit_up_count}  # 只有首板
    else:
        max_board_height = limit_step_df[limit_nums_col].max() if limit_nums_col in limit_step_df.columns else 1
        # 计算连板分布（2板及以上）
        board_distribution = {}
        if limit_nums_col in limit_step_df.columns:
            board_distribution = limit_step_df[limit_nums_col].value_counts().to_dict()
        # 计算首板数量 = 总涨停数 - 连板数
        continuous_count = len(limit_step_df)
        first_board_count = limit_up_count - continuous_count
        if first_board_count > 0:
            board_distribution[1] = first_board_count
    
    # 6. 计算炸板率（从涨停池数据）
    open_times_col = 'open_times' if 'open_times' in limit_up_df.columns else '炸板次数'
    if open_times_col in limit_up_df.columns:
        broken_count = len(limit_up_df[limit_up_df[open_times_col] > 0])
        broken_rate = (broken_count / limit_up_count) * 100 if limit_up_count > 0 else 0
    else:
        broken_rate = 0
    
    # 7. 获取跌停数据
    limit_down_df = dm.get_limit_down_pool(trade_date)
    nuclear_button_count = len(limit_down_df) if limit_down_df is not None else 0
    
    # 8. 识别情绪周期
    cycle = engine.detect_cycle(
        limit_up_count=limit_up_count,
        max_board_height=int(max_board_height),
        broken_rate=broken_rate,
        nuclear_button_count=nuclear_button_count,
        board_distribution=board_distribution
    )
    
    # 9. 获取策略
    strategy = engine.get_strategy(cycle)
    
    print("\n" + "="*60)
    print(f"【{trade_date} 情绪周期分析（结合连板天梯+涨停池）】")
    print("="*60)
    print(f"\n📊 情绪周期: {cycle.value}")
    print(f"🎯 策略: {strategy.strategy}")
    print(f"💰 仓位: {strategy.position}")
    print(f"\n📈 指标:")
    print(f"  - 涨停家数: {limit_up_count}")
    print(f"  - 最高连板: {max_board_height}板")
    print(f"  - 炸板率: {broken_rate:.2f}%")
    print(f"  - 核按钮: {nuclear_button_count}")
    
    if board_distribution:
        print(f"\n🔥 连板分布:")
        for height in sorted(board_distribution.keys(), reverse=True):
            count = board_distribution[height]
            bar = "█" * int(count / max(board_distribution.values()) * 20)
            print(f"    {height:2d}板: {bar} {count}只")
    
    print("="*60)
    
    return {
        'cycle': cycle,
        'strategy': strategy,
        'metrics': {
            'limit_up_count': limit_up_count,
            'max_board_height': int(max_board_height),
            'broken_rate': broken_rate,
            'nuclear_button_count': nuclear_button_count,
            'board_distribution': board_distribution
        }
    }


def batch_analyze_emotion(start_date: str, end_date: str):
    """
    批量分析一段时间内的情绪周期变化
    
    Args:
        start_date: 开始日期，格式YYYYMMDD
        end_date: 结束日期，格式YYYYMMDD
    """
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    engine = EmotionCycleEngine()
    
    # 获取交易日列表
    trade_dates = dm.date_utils.get_trade_dates_between(start_date, end_date)
    
    print("\n" + "="*80)
    print(f"【情绪周期变化趋势】{start_date} 至 {end_date}")
    print("="*80)
    print(f"{'日期':<12}{'周期':<10}{'涨停':<8}{'高度':<8}{'策略':<30}")
    print("-"*80)
    
    results = []
    for date in trade_dates:
        limit_up_df = dm.get_limit_up_pool(date)
        if limit_up_df.empty:
            continue
        
        limit_down_df = dm.get_limit_down_pool(date)
        
        result = engine.analyze_market_data(limit_up_df, limit_down_df)
        cycle = result['cycle']
        strategy = result['strategy']
        metrics = result['metrics']
        
        results.append({
            'date': date,
            'cycle': cycle.value,
            'limit_up': metrics['limit_up_count'],
            'height': metrics['max_board_height'],
            'strategy': strategy.strategy[:28]
        })
        
        print(f"{date:<12}{cycle.value:<10}{metrics['limit_up_count']:<8}{metrics['max_board_height']:<8}{strategy.strategy[:28]:<30}")
    
    print("="*80)
    
    return results


if __name__ == "__main__":
    # 示例1: 分析今天的情绪周期
    print("\n示例1: 分析最近交易日的情绪周期")
    result = analyze_today_emotion()
    
    # 示例2: 使用连板天梯数据分析
    print("\n\n示例2: 使用连板天梯数据分析")
    result2 = analyze_emotion_with_limit_step()
    
    # 示例3: 批量分析（需要设置日期范围）
    # print("\n\n示例3: 批量分析情绪周期变化")
    # batch_analyze_emotion("20260101", "20260418")
