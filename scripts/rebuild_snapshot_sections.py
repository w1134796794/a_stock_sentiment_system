"""重建存量快照里的「富表格」section（可读性优化）。

历史快照里的 涨停梯队 / 概念连板梯队 / 龙虎榜 / 资金流向 / 复盘总结 / 周期模式胜率
等 section 是用旧的 `tabulate` 落出来的——要么把整段嵌套 JSON 塞进单个单元格，
要么把一级/二级行业拆成两列，可读性很差。

本脚本就地把这些 section 用 `snapshot.section_format` 的 formatter 重新格式化成
干净的多列表格 / 多子表，而**无需重新跑整条收盘流水线**：旧 section 里已经包含
全部原始数据（只是被 JSON 字符串化了），脚本会先还原成结构化 source，再重新格式化。

幂等：已是新格式的 section 会被跳过。

用法：
    python scripts/rebuild_snapshot_sections.py                # 处理 SNAPSHOT_DIR 下全部
    python scripts/rebuild_snapshot_sections.py 20260529       # 只处理指定日期
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import SNAPSHOT_DIR  # noqa: E402
from snapshot.section_format import SECTION_FORMATTERS  # noqa: E402


def _maybe_json(v: Any) -> Any:
    """旧格式单元格里的 JSON 字符串还原成结构；非 JSON 原样返回。"""
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except Exception:
                return v
    return v


# 已是新格式的判定（用于幂等跳过）
def _already_new(name: str, section: Dict[str, Any]) -> bool:
    cols = section.get("columns") or []
    if section.get("blocks"):
        return True
    if name == "涨停梯队":
        return "行业" in cols
    if name == "概念连板梯队":
        return "概念名称" in cols
    if name in ("龙虎榜", "资金流向"):
        return "股票代码" in cols
    return False


def _reconstruct_source(name: str, section: Dict[str, Any]) -> Optional[Any]:
    """从旧 section 还原出 formatter 需要的 jsonable source。"""
    cols = section.get("columns") or []
    rows = section.get("rows") or []
    if name == "涨停梯队":
        return rows  # rows 本身就是 hierarchy_df 的 jsonable 记录
    if cols == ["字段", "值"]:
        return {r.get("字段"): _maybe_json(r.get("值")) for r in rows}
    if cols == ["值"]:
        return _maybe_json(rows[0].get("值")) if rows else {}
    return None


def rebuild_snapshot(path: Path) -> List[str]:
    """就地重建一个快照文件，返回被重写的 section 名列表。"""
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    sections = snapshot.get("sections") or []
    changed: List[str] = []

    for i, section in enumerate(sections):
        name = section.get("name")
        formatter = SECTION_FORMATTERS.get(name)
        if formatter is None or _already_new(name, section):
            continue
        src = _reconstruct_source(name, section)
        if src is None:
            continue
        try:
            new_section = formatter(src)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {name} 重建失败: {e}")
            continue
        if new_section and new_section.get("rows"):
            sections[i] = new_section
            changed.append(name)

    if changed:
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return changed


def main(argv: List[str]) -> None:
    snap_dir = Path(SNAPSHOT_DIR)
    if argv:
        files = [snap_dir / f"{d}.json" for d in argv]
    else:
        files = sorted(snap_dir.glob("*.json"))

    if not files:
        print(f"未找到快照文件（{snap_dir}）")
        return

    for path in files:
        if not path.exists():
            print(f"跳过（不存在）：{path.name}")
            continue
        changed = rebuild_snapshot(path)
        if changed:
            print(f"[OK] {path.name}: 已重建 {', '.join(changed)}")
        else:
            print(f"[--] {path.name}: 无需变更")


if __name__ == "__main__":
    main(sys.argv[1:])
