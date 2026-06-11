"""黄金快照回归工具（Phase 0 护栏）。

目的：在重构（数据解耦 / 因子引擎 / 置信度扣分制）期间，提供一个**客观判据**——
重构前后对同一交易日重跑分析，输出是否一致、哪些字段变了。

它**不重跑**重流水线、也不碰 DataManager；只在已落盘的 `webdata/snapshots/{date}.json`
上工作（纯标准库），因此你用 GUI（run_manager）还是 main.py 重跑都适用。

工作流：
    # 重构前：先正常跑分析（GUI 或 main.py）生成快照，再固化黄金基线
    python scripts/regression_snapshot.py dump --all
    # 重构后：再跑一次分析覆盖快照，然后比对
    python scripts/regression_snapshot.py check --all

只比对**有意义且确定性**的子集：meta.date + market(情绪/仓位/评分) + patterns(信号) +
trade_plans(计划) + risk_gate(风控)。默认排除派生且易变的 `sections`（可 --include-sections 加入）。
比对前会：剔除时间戳(generated_at)、浮点定点到 N 位、NaN/Inf 归一，避免无意义抖动。

用法：
    python scripts/regression_snapshot.py dump 20260609 20260608   # 指定日期固化基线
    python scripts/regression_snapshot.py dump --all               # 所有快照固化基线
    python scripts/regression_snapshot.py check 20260609           # 比对单日
    python scripts/regression_snapshot.py check --all              # 比对全部已有基线
    python scripts/regression_snapshot.py list                     # 看基线/快照清单

退出码：check 有差异时返回 1（便于接 CI / 脚本判断），否则 0。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK，无法编码 ✓/→ 等符号；尽量切到 UTF-8，失败则忽略
try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

try:
    from config.settings import SNAPSHOT_DIR, BASE_DIR  # noqa: E402
except Exception:  # pragma: no cover - 离线兜底
    BASE_DIR = Path(__file__).resolve().parent.parent
    SNAPSHOT_DIR = BASE_DIR / "webdata" / "snapshots"

GOLDEN_DIR = Path(BASE_DIR) / "tests" / "golden"

# 归一化参数
FLOAT_NDIGITS = 4                       # 浮点定点位数（吸收 FP 抖动）
DROP_KEYS = {"generated_at"}            # 任意层级都剔除的易变键
# 默认纳入比对的顶层区块（确定性 + 有意义）；sections 派生且体积大，默认排除
DEFAULT_SECTIONS = ["meta", "market", "patterns", "trade_plans", "risk_gate"]


# ----------------------------------------------------------------------
# 归一化：把快照压成稳定可比的子集
# ----------------------------------------------------------------------
def _norm(value: Any) -> Any:
    """递归归一化：dict 按键排序并剔除易变键；list 保留顺序（顺序常含排名语义）；
    float 定点；NaN/Inf 归一为字符串，避免 nan != nan 造成的伪差异。"""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k in sorted(value.keys(), key=str):
            if k in DROP_KEYS:
                continue
            out[str(k)] = _norm(value[k])
        return out
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "__NaN__"
        if math.isinf(value):
            return "__Inf__" if value > 0 else "__-Inf__"
        return round(value, FLOAT_NDIGITS)
    if isinstance(value, int):
        return value
    return value


def normalize_snapshot(snapshot: Dict[str, Any], sections: List[str]) -> Dict[str, Any]:
    """从完整快照抽取指定顶层区块并归一化。"""
    subset = {key: snapshot.get(key) for key in sections if key in snapshot}
    return _norm(subset)


# ----------------------------------------------------------------------
# 递归 diff
# ----------------------------------------------------------------------
def _diff(golden: Any, current: Any, path: str, out: List[Tuple[str, str, Any, Any]]) -> None:
    if isinstance(golden, dict) and isinstance(current, dict):
        for k in sorted(set(golden) | set(current), key=str):
            sub = f"{path}.{k}" if path else str(k)
            if k not in current:
                out.append((sub, "removed", golden[k], None))
            elif k not in golden:
                out.append((sub, "added", None, current[k]))
            else:
                _diff(golden[k], current[k], sub, out)
        return
    if isinstance(golden, list) and isinstance(current, list):
        if len(golden) != len(current):
            out.append((path, "list_len", len(golden), len(current)))
        for i in range(min(len(golden), len(current))):
            _diff(golden[i], current[i], f"{path}[{i}]", out)
        return
    if golden != current:
        out.append((path, "changed", golden, current))


def diff_normalized(golden: Dict[str, Any], current: Dict[str, Any]) -> List[Tuple[str, str, Any, Any]]:
    out: List[Tuple[str, str, Any, Any]] = []
    _diff(golden, current, "", out)
    return out


# ----------------------------------------------------------------------
# IO 辅助
# ----------------------------------------------------------------------
def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  ! 读取失败 {path.name}: {e}")
        return None


def _snapshot_dates() -> List[str]:
    if not Path(SNAPSHOT_DIR).exists():
        return []
    return sorted((p.stem for p in Path(SNAPSHOT_DIR).glob("*.json") if p.stem.isdigit()), reverse=True)


def _golden_dates() -> List[str]:
    if not GOLDEN_DIR.exists():
        return []
    return sorted((p.stem for p in GOLDEN_DIR.glob("*.json") if p.stem.isdigit()), reverse=True)


def _fmt_val(v: Any, width: int = 60) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= width else s[: width - 1] + "…"


# ----------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------
def cmd_dump(dates: List[str], sections: List[str]) -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for d in dates:
        snap = _load_json(Path(SNAPSHOT_DIR) / f"{d}.json")
        if snap is None:
            print(f"  [X] {d}: 快照不存在，跳过")
            continue
        norm = normalize_snapshot(snap, sections)
        out_path = GOLDEN_DIR / f"{d}.json"
        out_path.write_text(json.dumps(norm, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        sig = sum(len(b.get("rows") or []) for b in (norm.get("patterns") or {}).values())
        plans = len((norm.get("trade_plans") or {}).get("rows") or [])
        print(f"  [OK] {d}: 已固化基线（信号{sig} 计划{plans}） -> {out_path.relative_to(BASE_DIR)}")
        ok += 1
    print(f"\ndump 完成：{ok}/{len(dates)} 个日期。基线目录：{GOLDEN_DIR.relative_to(BASE_DIR)}")
    return 0


def cmd_check(dates: List[str], sections: List[str], max_diffs: int) -> int:
    total_diff_days = 0
    for d in dates:
        golden = _load_json(GOLDEN_DIR / f"{d}.json")
        if golden is None:
            print(f"  [?] {d}: 无基线（先 dump），跳过")
            continue
        snap = _load_json(Path(SNAPSHOT_DIR) / f"{d}.json")
        if snap is None:
            print(f"  [X] {d}: 当前快照不存在")
            total_diff_days += 1
            continue
        current = normalize_snapshot(snap, sections)
        diffs = diff_normalized(golden, current)
        if not diffs:
            print(f"  [OK] {d}: 一致")
            continue
        total_diff_days += 1
        print(f"  [X] {d}: {len(diffs)} 处差异")
        for path, kind, old, new in diffs[:max_diffs]:
            if kind == "changed":
                print(f"      ~ {path}: {_fmt_val(old)}  ->  {_fmt_val(new)}")
            elif kind == "added":
                print(f"      + {path}: {_fmt_val(new)}")
            elif kind == "removed":
                print(f"      - {path}: {_fmt_val(old)}")
            elif kind == "list_len":
                print(f"      # {path}: 列表长度 {old} -> {new}")
        if len(diffs) > max_diffs:
            print(f"      ... 还有 {len(diffs) - max_diffs} 处（--max-diffs 调整上限）")

    print()
    if total_diff_days == 0:
        print("check 通过：所有日期与基线一致 [OK]")
        return 0
    print(f"check 未通过：{total_diff_days} 个日期存在差异 [X]")
    return 1


def cmd_list() -> int:
    snaps = _snapshot_dates()
    goldens = _golden_dates()
    print(f"快照目录 {Path(SNAPSHOT_DIR).relative_to(BASE_DIR)}：{len(snaps)} 个")
    print(f"  {', '.join(snaps) if snaps else '(空)'}")
    print(f"基线目录 {GOLDEN_DIR.relative_to(BASE_DIR) if GOLDEN_DIR.exists() else GOLDEN_DIR}：{len(goldens)} 个")
    print(f"  {', '.join(goldens) if goldens else '(空)'}")
    missing = [d for d in goldens if d not in snaps]
    if missing:
        print(f"[!] 有基线但无当前快照：{', '.join(missing)}")
    return 0


def _resolve_dates(args, *, for_check: bool) -> List[str]:
    if args.all:
        return _golden_dates() if for_check else _snapshot_dates()
    if args.dates:
        return list(args.dates)
    # 缺省：取最新一个快照
    snaps = _snapshot_dates()
    return snaps[:1]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="黄金快照回归工具（Phase 0 护栏）")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("dates", nargs="*", help="交易日 YYYYMMDD（可多个）")
    common.add_argument("--all", action="store_true", help="处理全部（dump=全部快照 / check=全部基线）")
    common.add_argument("--include-sections", action="store_true",
                        help="额外纳入派生的 sections 区块一起比对（默认不比）")

    p_dump = sub.add_parser("dump", parents=[common], help="把当前快照固化为黄金基线")
    p_check = sub.add_parser("check", parents=[common], help="比对当前快照与黄金基线")
    p_check.add_argument("--max-diffs", type=int, default=50, help="每个日期最多打印多少处差异")
    sub.add_parser("list", help="列出快照/基线清单")

    args = parser.parse_args(argv)

    sections = list(DEFAULT_SECTIONS)
    if getattr(args, "include_sections", False):
        sections.append("sections")

    if args.command == "list":
        return cmd_list()
    if args.command == "dump":
        dates = _resolve_dates(args, for_check=False)
        if not dates:
            print("没有可固化的快照。")
            return 1
        return cmd_dump(dates, sections)
    if args.command == "check":
        dates = _resolve_dates(args, for_check=True)
        if not dates:
            print("没有可比对的基线（先运行 dump）。")
            return 1
        return cmd_check(dates, sections, args.max_diffs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())