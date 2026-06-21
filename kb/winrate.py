"""筛选胜率接口占位。

旧版这里计算“周期 × 策略模式”胜率。dev 分支已切到因子筛选，暂时保留同名接口让
问答和复盘模块安全降级；后续可改为基于 ``webdata/screening`` 的因子组合表现矩阵。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import loguru

logger = loguru.logger


def compute_matrix(
    end_date: Optional[str] = None,
    lookback_days: int = 60,
    win_threshold_pct: float = 0.0,
) -> Dict[str, Any]:
    """返回空的筛选表现矩阵。"""
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": ["", end_date or ""],
        "sample_total": 0,
        "win_threshold_pct": win_threshold_pct,
        "cycles": [],
        "patterns": [],
        "cells": {},
        "note": f"dev 分支已移除旧策略胜率矩阵；lookback_days={lookback_days}",
    }


def save_matrix(data: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[Winrate] 空矩阵已保存: {path}")


def load_matrix(path: Path) -> Optional[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def matrix_to_chunks(data: Dict[str, Any], min_n: int = 3) -> List[Any]:
    return []


def winrate_for_cycle(data: Optional[Dict[str, Any]], cycle: str, min_n: int = 3) -> List[Dict[str, Any]]:
    return []
