"""回测运行脚本。"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import TUSHARE_TOKEN, CACHE_DIR, OUTPUT_DIR, SNAPSHOT_DIR, WEB_DATA_DIR
from core.data.data_manager_main import DataManager
from backtest import BacktestEngine
from backtest.performance_analyzer import PerformanceAnalyzer
from backtest.plan_source import build_backtest_plan_dir
from risk import RiskManager, PositionSizer, RiskAnalyzer
import loguru

logger = loguru.logger


def run_backtest_demo():
    """运行快照驱动回测演示。"""
    logger.info("=" * 60)
    logger.info("开始运行快照驱动回测")
    logger.info("=" * 60)

    # 1. 初始化数据管理器
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)

    # 2. 设置回测参数
    # 回测最近30天的数据（使用示例数据）
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    logger.info(f"回测区间: {start_date} 至 {end_date}")

    # 3. 创建回测引擎
    from backtest.backtest_engine import BacktestConfig
    from risk.risk_config import RiskConfig

    config = BacktestConfig.from_risk_config(
        RiskConfig.load(), initial_capital=100000.0,
    )

    backtest = BacktestEngine(dm, config)

    # 4. 从 webdata/snapshots 或 webdata/screening 派生回测交易计划
    trade_plans_dir, _, _ = build_backtest_plan_dir(
        snapshot_dir=Path(SNAPSHOT_DIR),
        output_dir=Path(WEB_DATA_DIR),
        screening_dir=Path(WEB_DATA_DIR) / "screening",
        start_date=start_date,
        end_date=end_date,
    )
    result = backtest.run_backtest(
        start_date=start_date,
        end_date=end_date,
        trade_plans_dir=str(trade_plans_dir)
    )

    # 5. 生成性能报告
    analyzer = PerformanceAnalyzer()
    report = analyzer.generate_performance_report(result)

    print("\n" + report)

    # 6. 保存详细结果
    save_backtest_results(result, OUTPUT_DIR)

    logger.info("回测完成！")


def save_backtest_results(result: dict, output_dir: str, metadata: dict | None = None):
    """保存回测结果到文件"""
    import pandas as pd
    from pathlib import Path

    output_path = Path(output_dir) / "backtest_results"
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存交易记录
    if result.get('trade_history'):
        trades_df = pd.DataFrame([{
            'date': t.date,
            'stock_code': t.stock_code,
            'stock_name': t.stock_name,
            'pattern_type': t.pattern_type,
            'action': t.action,
            'entry_price': t.entry_price,
            'exit_price': t.exit_price,
            'shares': t.shares,
            'pnl': t.pnl,
            'pnl_pct': t.pnl_pct,
            'holding_days': t.holding_days,
            'hot_resonance': t.hot_resonance,
            'resonance_sectors': t.resonance_sectors,
            'stop_loss_triggered': t.stop_loss_triggered,
            'take_profit_triggered': t.take_profit_triggered,
            'entry_date': getattr(t, 'entry_date', ''),
            'exit_reason': getattr(t, 'exit_reason', ''),
            'plan_rank': getattr(t, 'plan_rank', 0),
            'plan_score': getattr(t, 'plan_score', 0),
            'plan_reason': getattr(t, 'plan_reason', ''),
            'factor_metrics_json': getattr(t, 'factor_metrics_json', ''),
            'factor_context_json': getattr(t, 'factor_context_json', ''),
            'open_gap_pct': getattr(t, 'open_gap_pct', 0),
            'market_score': getattr(t, 'market_score', 0),
            'amount_ratio': getattr(t, 'amount_ratio', 0),
            'entry_signal': getattr(t, 'entry_signal', ''),
        } for t in result['trade_history']])

        trades_file = output_path / f"backtest_trades_{timestamp}.csv"
        trades_df.to_csv(trades_file, index=False, encoding="utf-8-sig")
        logger.info(f"交易记录已保存: {trades_file}")

    # 保存截止日持仓快照，页面据此展示现价和未实现盈亏。
    current_positions = result.get('current_positions') or {}
    as_of_date = str(result.get('as_of_date') or '')
    if not as_of_date and result.get('daily_nav'):
        as_of_date = str(result['daily_nav'][-1].get('date') or '')
    try:
        from backtest.trade_calendar import TradeCalendar
        calendar = TradeCalendar()
    except Exception:
        calendar = None
    position_rows = []
    for code, position in current_positions.items():
        shares = int(float(position.get('shares') or 0))
        entry_price = float(position.get('entry_price') or 0)
        cost_basis = float(position.get('cost_basis') or 0)
        market_value = float(position.get('market_value') or 0)
        current_price = market_value / shares if shares > 0 else 0
        unrealized_pnl = market_value - cost_basis
        unrealized_pnl_pct = unrealized_pnl / cost_basis if cost_basis > 0 else 0
        entry_date = str(position.get('entry_date') or '')
        holding_days = (
            calendar.holding_days(entry_date, as_of_date)
            if calendar is not None and entry_date and as_of_date else 0
        )
        position_rows.append({
            'as_of_date': as_of_date,
            'stock_code': str(code).zfill(6),
            'stock_name': position.get('stock_name') or '',
            'entry_date': entry_date,
            'entry_price': entry_price,
            'current_price': current_price,
            'shares': shares,
            'cost_basis': cost_basis,
            'market_value': market_value,
            'unrealized_pnl': unrealized_pnl,
            'unrealized_pnl_pct': unrealized_pnl_pct,
            'holding_days': holding_days,
            'plan_rank': position.get('plan_rank') or 0,
            'plan_score': position.get('plan_score') or 0,
            'entry_signal': position.get('entry_signal') or '',
        })
    positions_file = output_path / f"backtest_positions_{timestamp}.csv"
    pd.DataFrame(position_rows, columns=[
        'as_of_date', 'stock_code', 'stock_name', 'entry_date', 'entry_price',
        'current_price', 'shares', 'cost_basis', 'market_value', 'unrealized_pnl',
        'unrealized_pnl_pct', 'holding_days', 'plan_rank', 'plan_score', 'entry_signal',
    ]).to_csv(positions_file, index=False, encoding="utf-8-sig")
    logger.info(f"持仓快照已保存: {positions_file}")

    # 保存净值曲线
    if result.get('daily_nav'):
        nav_df = pd.DataFrame(result['daily_nav'])
        nav_file = output_path / f"backtest_nav_{timestamp}.csv"
        nav_df.to_csv(nav_file, index=False, encoding="utf-8-sig")
        logger.info(f"净值曲线已保存: {nav_file}")

    # 保存汇总报告
    summary = {
        'total_return': result.get('total_return', 0),
        'annualized_return': result.get('annualized_return', 0),
        'sharpe_ratio': result.get('sharpe_ratio', 0),
        'max_drawdown': result.get('max_drawdown', 0),
        'win_rate': result.get('win_rate', 0),
        'profit_loss_ratio': result.get('profit_loss_ratio', 0),
        'open_positions': len(current_positions),
        'unrealized_pnl': sum(float(row.get('unrealized_pnl') or 0) for row in position_rows),
        'total_trades': result.get('total_trades', 0),
        'buy_trades': result.get('buy_trades', 0),
        'closed_trades': result.get('closed_trades', result.get('total_trades', 0)),
        'initial_capital': result.get('initial_capital', 0),
        'final_capital': result.get('final_capital', 0)
    }
    if metadata:
        summary.update(metadata)

    summary_df = pd.DataFrame([summary])
    summary_file = output_path / f"backtest_summary_{timestamp}.csv"
    summary_df.to_csv(summary_file, index=False, encoding="utf-8-sig")
    logger.info(f"汇总报告已保存: {summary_file}")

    try:
        from backtest.attribution import build_attribution_frames

        frames = build_attribution_frames(result)
        for name, frame in frames.items():
            if frame is None or frame.empty:
                continue
            path = output_path / f"backtest_{name}_{timestamp}.csv"
            frame.to_csv(path, index=False, encoding="utf-8-sig")
            logger.info(f"回测归因已保存: {path}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"回测归因报表生成失败: {exc}")

    try:
        from backtest.walk_forward import build_walk_forward_frames

        folds, oos_summary = build_walk_forward_frames(result)
        for name, frame in (("walk_forward", folds), ("walk_forward_summary", oos_summary)):
            if frame is None or frame.empty:
                continue
            path = output_path / f"backtest_{name}_{timestamp}.csv"
            frame.to_csv(path, index=False, encoding="utf-8-sig")
            logger.info(f"滚动验证报表已保存: {path}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"滚动验证报表生成失败: {exc}")


def run_risk_analysis_demo():
    """运行风险分析演示"""
    logger.info("\n" + "=" * 60)
    logger.info("运行风险分析")
    logger.info("=" * 60)

    # 示例持仓数据
    sample_positions = {
        '000001.SZ': {
            'stock_name': '平安银行',
            'sector': '银行',
            'market_value': 20000,
            'hot_resonance': False
        },
        '000858.SZ': {
            'stock_name': '五粮液',
            'sector': '白酒',
            'market_value': 25000,
            'hot_resonance': True
        },
        '002594.SZ': {
            'stock_name': '比亚迪',
            'sector': '新能源汽车',
            'market_value': 30000,
            'hot_resonance': True
        }
    }

    # 创建风险分析器
    risk_analyzer = RiskAnalyzer()

    # 生成风险报告
    risk_report = risk_analyzer.generate_risk_report(sample_positions)

    print("\n【风险分析报告】")
    print(f"总体风险等级: {risk_report['summary']['overall_risk_level']}")
    print(f"持仓数量: {risk_report['summary']['total_positions']}")

    print("\n板块集中度:")
    sector_risk = risk_report['sector_risk']
    print(f"  风险等级: {sector_risk['risk_level']}")
    print(f"  最大集中度: {sector_risk['concentration']:.2%}")
    for sector, ratio in sector_risk.get('sector_ratios', {}).items():
        print(f"  - {sector}: {ratio:.2%}")

    print("\n风险热力图:")
    for stock in risk_report['risk_heatmap'].get('stock_risks', []):
        print(f"  {stock['stock_name']}: 风险分{stock['risk_score']}, 因素: {', '.join(stock['risk_factors'])}")

    print("\n建议:")
    for rec in risk_report['recommendations']:
        print(f"  {rec}")


def run_position_sizing_demo():
    """运行仓位管理演示"""
    logger.info("\n" + "=" * 60)
    logger.info("运行仓位管理演示")
    logger.info("=" * 60)

    position_sizer = PositionSizer()

    # 示例1: 高置信度+热点共振
    result1 = position_sizer.calculate_position(
        signal_quality=0.92,
        market_condition=position_sizer.get_market_condition({'cycle_name': '发酵期'}),
        base_size='medium',
        hot_resonance=True,
        sector_heat_score=85
    )

    print("\n【示例1: 高置信度+强共振】")
    print(f"建议仓位: {result1['position_pct']:.2%}")
    print(f"计算逻辑: {result1['rationale']}")

    # 示例2: 中等置信度+无共振
    result2 = position_sizer.calculate_position(
        signal_quality=0.75,
        market_condition=position_sizer.get_market_condition({'cycle_name': '震荡期'}),
        base_size='medium',
        hot_resonance=False,
        sector_heat_score=0
    )

    print("\n【示例2: 中等置信度+无共振】")
    print(f"建议仓位: {result2['position_pct']:.2%}")
    print(f"计算逻辑: {result2['rationale']}")

    # 组合仓位配置
    matrix = position_sizer.calculate_position_matrix(
        market_condition=position_sizer.get_market_condition({'cycle_name': '启动期'}),
        available_signals=5
    )

    print("\n【组合仓位配置矩阵】")
    print(f"市场环境: {matrix['market_condition']}")
    print(f"总仓位上限: {matrix['total_position_limit']:.0%}")
    print(f"单信号仓位: {matrix['per_signal_position']:.0%}")
    print(f"建议持仓数: {matrix['max_signals']}只")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='回测与风控分析工具')
    parser.add_argument('--mode', choices=['backtest', 'risk', 'position', 'all'],
                       default='all', help='运行模式')

    args = parser.parse_args()

    if args.mode in ['backtest', 'all']:
        try:
            run_backtest_demo()
        except Exception as e:
            logger.error(f"回测运行失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())

    if args.mode in ['risk', 'all']:
        run_risk_analysis_demo()

    if args.mode in ['position', 'all']:
        run_position_sizing_demo()
