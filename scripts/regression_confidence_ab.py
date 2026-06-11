"""二板定龙 置信度 A/B 回归：legacy vs deduction（灰度切换单策略）。

做法：对同一交易日，分别以 confidence_mode=legacy / deduction 跑完整五层流水线
（仅 ReviewPipeline.execute，不生成报表/快照/DB，零落盘），对比 ctx.patterns["二板定龙"]：
  - 各股置信度 legacy vs deduction 及差值；
  - 入选定龙集合 / 排名是否变化（置信度是 0.70 准入闸，可能改变入选）；
  - deduction 模式逐项扣分明细。

运行：
    python scripts/regression_confidence_ab.py --date 20260610

注意：脚本会临时修改 webdata/config_overrides.json 的 patterns 作用域，结束时**精确还原**。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loguru

# Windows 控制台默认 GBK，强制 UTF-8 避免 emoji/特殊符号崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 静默流水线日志，仅保留本脚本输出
loguru.logger.remove()

from config import overrides as ov  # noqa: E402

STRATEGY = "second_board_dragon"
PATH = f"{STRATEGY}.confidence_mode"


def run_mode(date: str, mode: str) -> dict:
    """以指定 confidence_mode 跑一次流水线，返回 {code: {...}}（二板定龙最终信号）。"""
    if mode == "deduction":
        ov.set_override("patterns", PATH, "deduction")
    else:
        ov.clear_override("patterns", PATH)

    # 延迟 import，确保读取到刚写入的覆盖
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    from core.data.data_manager_main import DataManager
    from core.data.industry_mapper import IndustryMapper
    from core.pipeline.review_pipeline import ReviewPipeline

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    mapper = IndustryMapper(dm)
    pipe = ReviewPipeline(dm, mapper)
    ctx = pipe.execute(date)

    out = {}
    for s in (ctx.patterns.get("二板定龙", []) or []):
        km = getattr(s, "key_metrics", {}) or {}
        out[s.stock_code] = {
            "name": s.stock_name,
            "confidence": float(s.confidence),
            "rank": km.get("全场龙头排名"),
            "breakdown": km.get("置信扣分明细"),
        }
    return out


def fmt_breakdown(bd: dict) -> str:
    if not bd:
        return ""
    items = bd.get("breakdown") or []
    parts = [f"{d['factor']}(-{d['penalty']})" for d in items if d.get("penalty")]
    return f"ceil={bd.get('ceiling')} floor={bd.get('floor')} 扣分:{'; '.join(parts) or '无'}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20260610")
    args = ap.parse_args()
    date = args.date

    original = ov.load_overrides()  # 用于精确还原
    try:
        print(f"\n===== 二板定龙 置信度 A/B 回归 · {date} =====\n")
        print(">> 跑 legacy ...")
        legacy = run_mode(date, "legacy")
        print(">> 跑 deduction ...")
        deduction = run_mode(date, "deduction")
    finally:
        ov.save_overrides(original)
        print("\n[已还原 config_overrides.json]")

    codes = sorted(set(legacy) | set(deduction))
    print(f"\n二板定龙最终信号：legacy={len(legacy)} 只, deduction={len(deduction)} 只\n")
    print(f"{'代码':<9}{'名称':<10}{'legacy':>8}{'deduction':>11}{'delta':>8}  入选(L/D)")
    print("-" * 70)
    for code in codes:
        l = legacy.get(code)
        d = deduction.get(code)
        lc = f"{l['confidence']:.2f}" if l else "-"
        dc = f"{d['confidence']:.2f}" if d else "-"
        delta = (f"{(d['confidence'] - l['confidence']):+.2f}" if l and d else "-")
        flag = f"{'Y' if l else 'N'}/{'Y' if d else 'N'}"
        name = (l or d or {}).get("name", "")
        print(f"{code:<9}{name:<10}{lc:>8}{dc:>11}{delta:>8}  {flag}")

    # deduction 扣分明细
    print("\n--- deduction 扣分明细 ---")
    for code in codes:
        d = deduction.get(code)
        if d and d.get("breakdown"):
            print(f"  {code} {d['name']}: {fmt_breakdown(d['breakdown'])}")

    # 入选差异
    only_l = set(legacy) - set(deduction)
    only_d = set(deduction) - set(legacy)
    if only_l or only_d:
        print("\n[!] 入选集合发生变化（置信度 0.70 准入闸影响）：")
        if only_l:
            print(f"    仅 legacy 入选: {[legacy[c]['name'] for c in only_l]}")
        if only_d:
            print(f"    仅 deduction 入选: {[deduction[c]['name'] for c in only_d]}")
    else:
        print("\n[OK] 两种模式入选的二板定龙集合一致（仅置信度数值差异）。")


if __name__ == "__main__":
    main()