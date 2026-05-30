"""
复盘胜率工具：周期 × 模式 的历史 T+1 胜率矩阵。

复用 ``core.analysis.cycle_pattern_matrix`` 的聚合引擎（它从 factor_results 历史 +
T+1 行情算胜率），把结果固化成 JSON，供：
- KBTools 定量查询（问答取真值）
- 每日 AI 解读注入「当前周期下各模式历史胜率」
- KB 检索（每个单元格 + 一条总览作为知识块）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import loguru

logger = loguru.logger


def compute_matrix(end_date: Optional[str] = None, lookback_days: int = 60,
                   win_threshold_pct: float = 0.0) -> Dict[str, Any]:
    """跑一遍胜率矩阵，返回可 JSON 化的 dict（需要 DataManager 拉 T+1 行情）。"""
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    from core.data.data_manager import DataManager
    from core.analysis.cycle_pattern_matrix import compute_cycle_pattern_matrix

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    matrix = compute_cycle_pattern_matrix(
        end_date=end_date, lookback_days=lookback_days,
        data_manager=dm, win_threshold_pct=win_threshold_pct)

    cells: Dict[str, Any] = {}
    for (cyc, pat), c in matrix.cells.items():
        cells[f"{cyc}|{pat}"] = {
            "cycle": cyc, "pattern": pat, "n": c.n,
            "win_rate": round(c.win_rate, 4),
            "avg_return": round(c.avg_return, 3),
            "max_return": round(c.max_return, 3),
            "min_return": round(c.min_return, 3),
        }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": list(matrix.sample_window),
        "sample_total": matrix.sample_count_total,
        "win_threshold_pct": win_threshold_pct,
        "cycles": matrix.cycles,
        "patterns": matrix.patterns,
        "cells": cells,
    }


def save_matrix(data: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"[Winrate] 矩阵已保存: {path}（样本 {data.get('sample_total')}）")


def load_matrix(path: Path) -> Optional[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _cell_significant(cell: Dict[str, Any], min_n: int = 3) -> bool:
    return cell.get("n", 0) >= min_n


def matrix_to_chunks(data: Dict[str, Any], min_n: int = 3) -> List[Any]:
    """把矩阵转成 KB 知识块（每个有效单元格一块 + 一条总览）。"""
    from kb.store import Chunk

    if not data or not data.get("cells"):
        return []
    win = data.get("window") or ["", ""]
    date = (win[1] or datetime.now().strftime("%Y%m%d"))[:8] or datetime.now().strftime("%Y%m%d")
    chunks: List[Chunk] = []

    sig_cells = [c for c in data["cells"].values() if _cell_significant(c, min_n)]
    for c in sig_cells:
        text = (f"周期×模式胜率：{c['cycle']}周期下「{c['pattern']}」历史T+1胜率"
                f"{c['win_rate'] * 100:.0f}%（样本{c['n']}，平均收益{c['avg_return']:+.2f}%，"
                f"最佳{c['max_return']:+.1f}%/最差{c['min_return']:+.1f}%）。"
                f"统计窗口{win[0]}~{win[1]}。")
        chunks.append(Chunk(id=f"winrate:{c['cycle']}:{c['pattern']}", date=date,
                            kind="winrate", text=text, sector=c["pattern"]))

    if sig_cells:
        top = sorted(sig_cells, key=lambda x: x["win_rate"], reverse=True)[:6]
        summary = "；".join(f"{c['cycle']}/{c['pattern']} {c['win_rate']*100:.0f}%(n={c['n']})" for c in top)
        chunks.append(Chunk(
            id="winrate:summary", date=date, kind="winrate",
            text=(f"周期×模式胜率矩阵总览（窗口{win[0]}~{win[1]}，样本{data.get('sample_total')}）。"
                  f"历史高胜率组合：{summary}。")))
    return chunks


def winrate_for_cycle(data: Optional[Dict[str, Any]], cycle: str, min_n: int = 3) -> List[Dict[str, Any]]:
    """取某情绪周期下各模式胜率（按胜率降序），供每日解读注入。"""
    if not data or not cycle:
        return []
    out = [c for c in data.get("cells", {}).values()
           if c.get("cycle") == cycle and _cell_significant(c, min_n)]
    out.sort(key=lambda x: x["win_rate"], reverse=True)
    return out
