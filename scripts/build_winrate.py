"""
计算并固化「周期 × 模式」历史 T+1 胜率矩阵，并灌入知识库。

    python scripts/build_winrate.py [--lookback 60] [--end 20260529]

产物：
- webdata/winrate_matrix.json   （供 Web / 每日解读 / KBTools 读取）
- kb.sqlite 内 kind=winrate 的知识块（供问答检索）

需要 Tushare（拉 T+1 行情，缓存命中很快）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import WINRATE_PATH, KB_DB_PATH  # noqa: E402
from kb.winrate import compute_matrix, save_matrix, matrix_to_chunks  # noqa: E402
from kb.store import KBStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="构建周期×模式胜率矩阵")
    ap.add_argument("--lookback", type=int, default=60, help="回看交易日数（默认 60）")
    ap.add_argument("--end", default=None, help="截止日期 YYYYMMDD（默认全部）")
    ap.add_argument("--win-threshold", type=float, default=0.0, help="盈利阈值%（默认 >0）")
    args = ap.parse_args()

    data = compute_matrix(end_date=args.end, lookback_days=args.lookback,
                          win_threshold_pct=args.win_threshold)
    save_matrix(data, WINRATE_PATH)

    # 灌入 KB（先清掉旧的 winrate 块，避免过期单元格残留）
    store = KBStore(KB_DB_PATH)
    chunks = matrix_to_chunks(data)
    store.delete_by_kind("winrate")
    if chunks:
        store.upsert_many(chunks)

    sig = sum(1 for c in data.get("cells", {}).values() if c.get("n", 0) >= 3)
    print(f"完成：样本 {data.get('sample_total')}，有效单元格 {sig}，"
          f"窗口 {data.get('window')}，KB 写入 {len(chunks)} 块 → {WINRATE_PATH}")


if __name__ == "__main__":
    main()
