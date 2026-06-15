"""五层复盘流水线最小调用示例。

用法：
    python examples/demo_usage.py 20260612

该示例复用正式入口的 DataManager / IndustryMapper / ReviewPipeline，仅打印摘要；
完整 Excel、快照、知识库落盘请直接运行 ``python main.py --date YYYYMMDD``。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import CACHE_DIR, TUSHARE_TOKEN  # noqa: E402
from core.data.data_manager_main import DataManager  # noqa: E402
from core.data.industry_mapper import IndustryMapper  # noqa: E402
from core.pipeline.review_pipeline import ReviewPipeline  # noqa: E402
from main import setup_logging  # noqa: E402


def main() -> None:
    setup_logging()
    date = sys.argv[1] if len(sys.argv) > 1 else None
    if not date:
        raise SystemExit("请传入交易日，例如: python examples/demo_usage.py 20260612")

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    mapper = IndustryMapper(dm)
    pipeline = ReviewPipeline(dm, mapper)
    ctx = pipeline.execute(date)
    pipeline.print_summary(ctx)


if __name__ == "__main__":
    main()
