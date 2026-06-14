"""模式信号黄金快照回归（Phase 0 护栏）。

为"龙头生命周期重构"提供改造前后**结果一致性**的客观判据：对给定交易日跑
``ReviewPipeline.execute``，把四个策略（弱转强/二板定龙/龙二波/首板突破）的信号
序列化为**稳定 JSON**（排序键、定点小数），存为 ``tests/golden/patterns_{date}.json``。

用法：
    # 生成/更新基线（重构前先跑一次存基线）
    python scripts/regression_pattern_snapshot.py --date 20260610

    # 改造后校验：重跑并与基线 diff，字段级输出差异；有差异 exit code=1
    python scripts/regression_pattern_snapshot.py --date 20260610 --check

    # 多日：逗号分隔
    python scripts/regression_pattern_snapshot.py --date 20260609,20260610 --check

设计：只取稳定字段（代码/名称/置信度/仓位/描述/key_metrics），便于解释 diff 来源。

⚠ 重要——龙头池状态副作用：``update_dragon_pools`` 每次运行都会**覆盖写**
``dragon_pools.json``（持久化龙头/走弱池）。因此同一交易日重复跑流水线**不是幂等的**：
首次跑会消费/推进池状态并落盘，二次跑读到的是被改写后的池 → 信号漂移。

为让快照成为**可靠判据**，本工具在每次跑流水线前后都把 ``dragon_pools.json``
还原到一份"夹具"（fixture）：
  - 生成基线时：把当前 ``dragon_pools.json`` 存为 ``tests/golden/dragon_pools_{date}.json``
    作为该日的固定起点；
  - 之后每次（生成/校验）跑流水线前都先从夹具还原，跑完再还原，保证起点一致、可复现。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent
POOL_FILE = _REPO_ROOT / "dragon_pools.json"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import loguru

# 静默流水线日志，仅保留本脚本输出
loguru.logger.remove()

PATTERN_KEYS = ["弱转强", "二板定龙", "龙二波", "首板突破"]
GOLDEN_DIR = Path(__file__).resolve().parent.parent / "tests" / "golden"


def _round_floats(obj: Any, ndigits: int = 4) -> Any:
    """递归把 float 定点化，保证快照稳定可比。"""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _signal_record(sig: Any) -> Dict[str, Any]:
    """从信号对象提取稳定字段（兼容 PatternSignal / TradeSignal）。"""
    return {
        "code": str(getattr(sig, "stock_code", "")),
        "name": getattr(sig, "stock_name", ""),
        "confidence": round(float(getattr(sig, "confidence", 0) or 0), 4),
        "position_size": getattr(sig, "position_size", ""),
        "description": getattr(sig, "description", ""),
        "key_metrics": _round_floats(getattr(sig, "key_metrics", {}) or {}),
    }


def pool_fixture_path(date: str) -> Path:
    return GOLDEN_DIR / f"dragon_pools_{date}.json"


def _restore_pool_from_fixture(date: str) -> bool:
    """跑流水线前：把 dragon_pools.json 还原到该日夹具（保证起点一致）。"""
    fx = pool_fixture_path(date)
    if fx.exists():
        shutil.copyfile(fx, POOL_FILE)
        return True
    return False


def build_snapshot(date: str, *, capture_pool_fixture: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """跑流水线，返回 {pattern_type: [record,...]}（按 code 排序）。

    capture_pool_fixture=True（生成基线时）：先把当前 dragon_pools.json 存为夹具；
    之后（含校验）每次跑前都从夹具还原，跑完再还原，消除池状态副作用。
    """
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    fx = pool_fixture_path(date)
    if capture_pool_fixture and POOL_FILE.exists() and not fx.exists():
        shutil.copyfile(POOL_FILE, fx)

    # 跑前还原到夹具固定起点
    _restore_pool_from_fixture(date)

    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    from core.data.data_manager_main import DataManager
    from core.data.industry_mapper import IndustryMapper
    from core.pipeline.review_pipeline import ReviewPipeline

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    mapper = IndustryMapper(dm)
    try:
        ctx = ReviewPipeline(dm, mapper).execute(date)
    finally:
        # 跑后还原，撤销 _save_pools 对 dragon_pools.json 的改写
        _restore_pool_from_fixture(date)

    snap: Dict[str, List[Dict[str, Any]]] = {}
    for key in PATTERN_KEYS:
        sigs = ctx.patterns.get(key, []) or []
        recs = [_signal_record(s) for s in sigs]
        recs.sort(key=lambda r: (r["code"], r["name"]))
        snap[key] = recs
    return snap


def snapshot_path(date: str) -> Path:
    return GOLDEN_DIR / f"patterns_{date}.json"


def save_snapshot(date: str, snap: Dict[str, Any]) -> Path:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    p = snapshot_path(date)
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _diff_pattern(name: str, old: List[Dict], new: List[Dict]) -> List[str]:
    """对单个策略做集合 + 字段级 diff，返回差异描述行。"""
    diffs: List[str] = []
    old_by = {r["code"]: r for r in old}
    new_by = {r["code"]: r for r in new}
    only_old = sorted(set(old_by) - set(new_by))
    only_new = sorted(set(new_by) - set(old_by))
    for c in only_old:
        diffs.append(f"  [{name}] - 缺失: {c} {old_by[c]['name']} (基线有，现无)")
    for c in only_new:
        diffs.append(f"  [{name}] + 新增: {c} {new_by[c]['name']} (基线无，现有)")
    for c in sorted(set(old_by) & set(new_by)):
        o, n = old_by[c], new_by[c]
        for field in ("confidence", "position_size", "description", "key_metrics"):
            if o.get(field) != n.get(field):
                diffs.append(f"  [{name}] ~ {c} {n['name']} 字段'{field}': {o.get(field)!r} → {n.get(field)!r}")
    return diffs


def check_snapshot(date: str) -> bool:
    """重跑并与基线 diff。返回 True=一致，False=有差异/无基线。"""
    p = snapshot_path(date)
    if not p.exists():
        print(f"[{date}] 基线不存在: {p}（请先不带 --check 跑一次生成基线）")
        return False
    if not pool_fixture_path(date).exists():
        print(f"[{date}] ⚠ 缺少龙头池夹具 {pool_fixture_path(date).name}，"
              f"校验可能受池状态漂移影响（请重新生成基线以固化夹具）")
    baseline = json.loads(p.read_text(encoding="utf-8"))
    current = build_snapshot(date)

    all_diffs: List[str] = []
    for name in PATTERN_KEYS:
        all_diffs.extend(_diff_pattern(name, baseline.get(name, []), current.get(name, [])))

    if not all_diffs:
        counts = ", ".join(f"{k}={len(current.get(k, []))}" for k in PATTERN_KEYS)
        print(f"[{date}] ✓ 与基线一致（{counts}）")
        return True
    print(f"[{date}] ✗ 发现 {len(all_diffs)} 处差异：")
    print("\n".join(all_diffs))
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="模式信号黄金快照回归")
    ap.add_argument("--date", required=True, help="交易日 YYYYMMDD，可逗号分隔多个")
    ap.add_argument("--check", action="store_true", help="校验模式：与基线 diff（默认生成基线）")
    args = ap.parse_args()

    dates = [d.strip() for d in str(args.date).split(",") if d.strip()]
    ok = True
    for date in dates:
        if args.check:
            ok = check_snapshot(date) and ok
        else:
            snap = build_snapshot(date, capture_pool_fixture=True)
            p = save_snapshot(date, snap)
            counts = ", ".join(f"{k}={len(snap[k])}" for k in PATTERN_KEYS)
            print(f"[{date}] 基线已写入 {p}（{counts}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())