"""批量重跑历史交易日数据。"""
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta
import loguru

sys.path.insert(0, str(Path(__file__).parent))

from main import SentimentSystem, setup_logging
from core.utils import DateUtils

logger = loguru.logger

def get_trade_dates(start: str, end: str) -> list:
    """获取区间内的所有交易日"""
    du = DateUtils()
    dates = []
    current = datetime.strptime(start, "%Y%m%d")
    end_dt = datetime.strptime(end, "%Y%m%d")

    while current <= end_dt:
        date_str = current.strftime("%Y%m%d")
        try:
            # 用 get_nearest_trade_date 验证是否为交易日
            trade_date = du.get_nearest_trade_date(date_str)
            if trade_date == date_str:
                dates.append(date_str)
        except Exception:
            pass
        current += timedelta(days=1)

    return dates


def main():
    parser = argparse.ArgumentParser(description="批量重跑历史交易日数据")
    parser.add_argument("--start-date", default="20260506", help="开始日期 YYYYMMDD")
    parser.add_argument("--end-date", default="20260618", help="结束日期 YYYYMMDD")
    args = parser.parse_args()

    setup_logging()

    trade_dates = get_trade_dates(args.start_date, args.end_date)
    total = len(trade_dates)

    logger.info(f"批量重跑开始: {args.start_date} ~ {args.end_date}")
    logger.info(f"共 {total} 个交易日")
    print("=" * 60)
    print(f"批量重跑: {args.start_date} ~ {args.end_date} ({total}个交易日)")
    print("=" * 60)

    system = SentimentSystem()

    for i, date in enumerate(trade_dates, 1):
        print(f"\n[{i}/{total}] 正在跑 {date} ...")
        try:
            system.run_daily_analysis(date)
            logger.info(f"[{i}/{total}] {date} 完成")
        except Exception as e:
            logger.error(f"[{i}/{total}] {date} 失败: {e}")
            print(f"[X] {date} 出错: {e}")

    print("\n" + "=" * 60)
    print("批量重跑完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
