"""查看交易记录"""
import pandas as pd
from pathlib import Path

# 查找最新的交易记录文件
output_dir = Path('output/backtest_results')
csv_files = list(output_dir.glob('backtest_trades_*.csv'))

if csv_files:
    # 读取最新的文件
    latest_file = max(csv_files, key=lambda x: x.stat().st_mtime)
    print(f"读取文件: {latest_file}")
    print("=" * 80)

    df = pd.read_csv(latest_file)

    # 显示所有交易
    print(f"\n总交易次数: {len(df)}")
    print(f"买入次数: {len(df[df['action'] == 'BUY'])}")
    print(f"卖出次数: {len(df[df['action'] == 'SELL'])}")
    print(f"分批卖出次数: {len(df[df['action'] == 'SELL_PARTIAL'])}")

    print("\n" + "=" * 80)
    print("卖出交易明细:")
    print("=" * 80)

    sell_trades = df[df['action'].isin(['SELL', 'SELL_PARTIAL'])]
    for _, trade in sell_trades.iterrows():
        print(f"\n日期: {trade['date']}")
        print(f"  股票: {trade['stock_name']}({trade['stock_code']})")
        print(f"  动作: {trade['action']}")
        print(f"  买入价: {trade['entry_price']:.2f}")
        print(f"  卖出价: {trade['exit_price']:.2f}")
        print(f"  股数: {trade['shares']}")
        print(f"  盈亏: ¥{trade['pnl']:.2f} ({trade['pnl_pct']:.2%})")
        print(f"  持仓天数: {trade['holding_days']}")
        print(f"  止损触发: {trade['stop_loss_triggered']}")
        print(f"  止盈触发: {trade['take_profit_triggered']}")

    print("\n" + "=" * 80)
    print("统计:")
    print("=" * 80)
    win_trades = sell_trades[sell_trades['pnl'] > 0]
    loss_trades = sell_trades[sell_trades['pnl'] < 0]
    print(f"盈利交易: {len(win_trades)}次")
    print(f"亏损交易: {len(loss_trades)}次")
    if len(win_trades) > 0:
        print(f"平均盈利: ¥{win_trades['pnl'].mean():.2f}")
    if len(loss_trades) > 0:
        print(f"平均亏损: ¥{loss_trades['pnl'].mean():.2f}")

else:
    print("未找到交易记录文件")