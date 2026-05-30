"""Sprint D-2：周期 × 模式胜率矩阵

把过去 N 天的 ``factor_results_*.json`` 聚合成一个二维矩阵：

::

           首板突破    二板定龙   弱转强    龙头二波
    上升期   65%(28)   78%(15)   58%(12)   75%(8)
    高潮期   45%(40)   62%(22)   40%(18)   55%(11)
    震荡期   35%(15)   50%(8)    52%(20)   30%(3)
    退潮期   20%(8)    25%(4)    65%(15)   N/A
    冰点期   15%(5)    N/A       70%(10)   N/A

意义
====
- 操盘核心问题之一：「**当前是上升期，弱转强这个模式在这个周期里历史胜率到底有多高？**」
- 现有 Layer5 的 pattern_stats 只是"全局胜率"——它把高潮期的强信号和退潮期的烂信号
  混在一起，结果数字平庸、无法指导实战。
- 加上情绪周期分桶后，就能给操盘手"**在当前情绪下，哪个模式最值得做**"的精准答案。

样本量阈值
==========
当某 (周期, 模式) 组合样本数 < 3，单元格显示 "N/A"，避免误导。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import loguru

logger = loguru.logger


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class CellStats:
    """二维矩阵单元格的聚合统计。"""
    n: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    max_return: float = 0.0
    min_return: float = 0.0

    def is_significant(self, min_n: int = 3) -> bool:
        """样本数是否达到阈值（用于决定矩阵显示 N/A）。"""
        return self.n >= min_n


@dataclass
class CyclePatternMatrix:
    """周期 × 模式胜率矩阵完整结果。"""
    cells: Dict[Tuple[str, str], CellStats] = field(default_factory=dict)
    cycles: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    sample_window: Tuple[str, str] = ("", "")
    sample_count_total: int = 0

    def get(self, cycle: str, pattern: str) -> Optional[CellStats]:
        return self.cells.get((cycle, pattern))

    def to_dataframe(self, value: str = "win_rate", min_n: int = 3) -> pd.DataFrame:
        """转成一个直接可写 Excel 的 DataFrame。

        Args:
            value: 单元格显示哪个指标 —— ``"win_rate"`` / ``"avg_return"``
            min_n: 样本数低于这个值的单元格显示 "N/A"
        """
        df = pd.DataFrame(index=self.cycles, columns=self.patterns)
        for (cyc, pat), cell in self.cells.items():
            if not cell.is_significant(min_n):
                df.loc[cyc, pat] = f"N/A(n={cell.n})"
                continue
            if value == "win_rate":
                df.loc[cyc, pat] = f"{cell.win_rate * 100:.0f}%(n={cell.n})"
            elif value == "avg_return":
                df.loc[cyc, pat] = f"{cell.avg_return:+.2f}%(n={cell.n})"
            else:
                df.loc[cyc, pat] = f"{getattr(cell, value, 0):.2f}"
        return df.fillna(f"N/A(n=0)")


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

# 在 A 股语境下规范化情绪周期名称
_CYCLE_ORDER = ["高潮期", "上升期", "震荡期", "回暖期", "退潮期", "冰点期"]

# 在 A 股语境下"模式胜率"看的核心模式（覆盖 99% 短线决策）
_DEFAULT_PATTERNS = ["首板突破", "二板定龙", "弱转强", "龙头二波", "龙二波"]


def compute_cycle_pattern_matrix(
    factor_results_dir: Optional[Path] = None,
    *,
    end_date: Optional[str] = None,
    lookback_days: int = 30,
    data_manager=None,
    win_threshold_pct: float = 0.0,
) -> CyclePatternMatrix:
    """从 ``factor_results_*.json`` 聚合周期×模式胜率矩阵。

    Args:
        factor_results_dir: factor_results JSON 目录，默认 ``output/factor_results``
        end_date: 仅考虑 <= end_date 的日期（``YYYYMMDD``）；None 全部
        lookback_days: 仅取最近 N 个有 JSON 的日期
        data_manager: 用来拉 T+1 行情（必须提供，否则只能用 JSON 内置的信号字段）
        win_threshold_pct: 视为"盈利"的最低 T+1 涨跌幅（默认 > 0% 即算盈利）

    Returns:
        CyclePatternMatrix
    """
    if factor_results_dir is None:
        factor_results_dir = Path(__file__).parent.parent.parent / "output" / "factor_results"
    factor_results_dir = Path(factor_results_dir)
    if not factor_results_dir.exists():
        logger.debug(f"[CyclePatternMatrix] {factor_results_dir} 不存在，返回空矩阵")
        return CyclePatternMatrix()

    # 列出所有 JSON 文件
    files = sorted(factor_results_dir.glob("factor_results_*.json"))
    if end_date:
        files = [f for f in files if f.stem.replace("factor_results_", "") <= end_date]
    files = files[-lookback_days:] if lookback_days > 0 else files
    if not files:
        return CyclePatternMatrix()

    # 收集 raw 信号：(date, cycle, pattern, code, name)
    rows: List[Dict] = []
    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except Exception as e:
            logger.debug(f"[CyclePatternMatrix] 读取 {fp.name} 失败: {e}")
            continue

        trade_date = raw.get("meta", {}).get("trade_date", "")
        cycle = (raw.get("emotion_cycle", {}) or {}).get("情绪周期", "")
        if not trade_date or not cycle:
            continue

        layer3 = raw.get("layer3_stock_selection", {}) or {}
        mode_signals = layer3.get("模式信号", {}) or {}
        for pat, signals in mode_signals.items():
            for sig in signals or []:
                code = str(sig.get("股票代码") or sig.get("stock_code") or "").strip()
                if not code:
                    continue
                rows.append({
                    "trade_date": trade_date,
                    "cycle": cycle,
                    "pattern": pat,
                    "code": code,
                    "name": str(sig.get("股票名称") or sig.get("stock_name") or ""),
                    "confidence": float(sig.get("置信度") or 0),
                })

    if not rows:
        return CyclePatternMatrix(
            sample_window=(files[0].stem[-8:], files[-1].stem[-8:]),
            sample_count_total=0,
        )

    df_signals = pd.DataFrame(rows)

    # 给每个信号配 T+1 涨跌幅
    if data_manager is None:
        logger.warning(
            "[CyclePatternMatrix] data_manager 为空，无法拉 T+1 行情，返回 N/A 矩阵"
        )
        return CyclePatternMatrix(
            cycles=_CYCLE_ORDER,
            patterns=_DEFAULT_PATTERNS,
            sample_window=(rows[0]["trade_date"], rows[-1]["trade_date"]),
            sample_count_total=len(rows),
        )

    from core.utils.date_utils import DateUtils
    du = DateUtils()

    # 按 trade_date 分组，一次性拉 T+1 行情
    df_signals["t1_return"] = None
    for trade_date, group in df_signals.groupby("trade_date"):
        try:
            t1 = du.get_next_trade_date(trade_date)
        except Exception:
            continue
        if not t1 or t1 == trade_date:
            continue
        try:
            daily = data_manager.get_all_stocks_daily(t1)
        except Exception:
            continue
        if daily is None or daily.empty or 'ts_code' not in daily.columns:
            continue
        ret_map = {row['ts_code']: float(row.get('pct_chg', 0) or 0)
                   for _, row in daily.iterrows()}
        for idx in group.index:
            code = df_signals.loc[idx, "code"]
            if code in ret_map:
                df_signals.loc[idx, "t1_return"] = ret_map[code]

    # 过滤掉拿不到 T+1 数据的
    df_evaluated = df_signals[df_signals["t1_return"].notna()].copy()
    if df_evaluated.empty:
        return CyclePatternMatrix(
            cycles=_CYCLE_ORDER,
            patterns=_DEFAULT_PATTERNS,
            sample_window=(rows[0]["trade_date"], rows[-1]["trade_date"]),
            sample_count_total=0,
        )
    df_evaluated["t1_return"] = df_evaluated["t1_return"].astype(float)

    # 二维聚合
    cells: Dict[Tuple[str, str], CellStats] = {}
    for (cyc, pat), grp in df_evaluated.groupby(["cycle", "pattern"]):
        n = len(grp)
        wins = int((grp["t1_return"] > win_threshold_pct).sum())
        cells[(cyc, pat)] = CellStats(
            n=n,
            win_rate=wins / n if n else 0.0,
            avg_return=float(grp["t1_return"].mean()),
            max_return=float(grp["t1_return"].max()),
            min_return=float(grp["t1_return"].min()),
        )

    # 收集 cycle / pattern 全集（按统一顺序排）
    cycles_in_data = list(df_evaluated["cycle"].unique())
    patterns_in_data = list(df_evaluated["pattern"].unique())
    cycles = [c for c in _CYCLE_ORDER if c in cycles_in_data] + \
             [c for c in cycles_in_data if c not in _CYCLE_ORDER]
    patterns = [p for p in _DEFAULT_PATTERNS if p in patterns_in_data] + \
               [p for p in patterns_in_data if p not in _DEFAULT_PATTERNS]

    matrix = CyclePatternMatrix(
        cells=cells,
        cycles=cycles,
        patterns=patterns,
        sample_window=(df_evaluated["trade_date"].min(),
                       df_evaluated["trade_date"].max()),
        sample_count_total=len(df_evaluated),
    )

    logger.info(
        f"[CyclePatternMatrix] 已聚合 {len(df_evaluated)} 个历史信号，"
        f"窗口 {matrix.sample_window[0]}~{matrix.sample_window[1]}，"
        f"周期 {len(cycles)} × 模式 {len(patterns)} = {len(cells)} 个非空单元格"
    )
    return matrix


__all__ = [
    "CellStats",
    "CyclePatternMatrix",
    "compute_cycle_pattern_matrix",
]
