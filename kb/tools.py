"""
定量只读查询（基于 app.sqlite）。

这些是给 LLM 用的"工具"：涉及次数 / 频率 / 历史出现等定量问题一律走这里取真值，
不让模型自行推算，杜绝幻觉。全部只读，永不写库。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


class KBTools:
    def __init__(self, app_db_path: Path, winrate_path: Optional[Path] = None):
        self.app_db_path = Path(app_db_path)
        if winrate_path is None:
            try:
                from config.settings import WINRATE_PATH
                winrate_path = WINRATE_PATH
            except Exception:
                winrate_path = None
        self.winrate_path = Path(winrate_path) if winrate_path else None

    def _ok(self) -> bool:
        return self.app_db_path.exists()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.app_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    def recent_dates(self, n: int = 10) -> List[str]:
        if not self._ok():
            return []
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT date FROM daily_snapshot ORDER BY date DESC LIMIT ?", (n,)).fetchall()]

    def plans_on(self, date: str) -> List[Dict[str, Any]]:
        if not self._ok():
            return []
        with self._conn() as c:
            rows = c.execute(
                """SELECT stock_code, stock_name, pattern_type, priority, score,
                          suggested_position, entry_range, stop_loss, take_profit,
                          gate_action, gate_hint
                   FROM trade_plans WHERE date=? ORDER BY score DESC""", (date,)).fetchall()
            return [dict(r) for r in rows]

    def signal_counts(self, days: int = 20) -> Dict[str, int]:
        """最近 N 个交易日各模式信号数。"""
        if not self._ok():
            return {}
        with self._conn() as c:
            dates = [r[0] for r in c.execute(
                "SELECT DISTINCT date FROM signals ORDER BY date DESC LIMIT ?", (days,)).fetchall()]
            if not dates:
                return {}
            ph = ",".join("?" * len(dates))
            rows = c.execute(
                f"SELECT pattern_type, count(*) FROM signals WHERE date IN ({ph}) "
                f"GROUP BY pattern_type ORDER BY 2 DESC", dates).fetchall()
            return {r[0]: r[1] for r in rows}

    def stock_history(self, stock_code: str) -> List[Dict[str, Any]]:
        """某只股票在历史计划/信号中的出现记录。"""
        if not self._ok():
            return []
        out: List[Dict[str, Any]] = []
        with self._conn() as c:
            for r in c.execute(
                """SELECT date, pattern_type, score, gate_action
                   FROM trade_plans WHERE stock_code=? ORDER BY date DESC""", (stock_code,)).fetchall():
                out.append({"date": r[0], "type": "plan", "pattern": r[1],
                            "score": r[2], "gate": r[3]})
            for r in c.execute(
                """SELECT date, pattern_type, confidence
                   FROM signals WHERE stock_code=? ORDER BY date DESC""", (stock_code,)).fetchall():
                out.append({"date": r[0], "type": "signal", "pattern": r[1], "confidence": r[2]})
        out.sort(key=lambda x: x["date"], reverse=True)
        return out

    def pattern_history(self, pattern: str, days: int = 20) -> List[Dict[str, Any]]:
        """某模式最近 N 天每日信号数。"""
        if not self._ok():
            return []
        with self._conn() as c:
            rows = c.execute(
                """SELECT date, count(*) FROM signals WHERE pattern_type LIKE ?
                   GROUP BY date ORDER BY date DESC LIMIT ?""", (f"%{pattern}%", days)).fetchall()
            return [{"date": r[0], "count": r[1]} for r in rows]

    # ------------------------------------------------------------------
    # 复盘胜率（周期 × 模式 历史 T+1 胜率矩阵）
    # ------------------------------------------------------------------
    def _load_winrate(self) -> Optional[Dict[str, Any]]:
        if not self.winrate_path or not self.winrate_path.exists():
            return None
        try:
            import json
            with open(self.winrate_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None

    def pattern_winrate(self, pattern: str, min_n: int = 3) -> List[Dict[str, Any]]:
        """某模式在各情绪周期下的历史 T+1 胜率（按胜率降序）。"""
        data = self._load_winrate()
        if not data:
            return []
        out = [c for c in data.get("cells", {}).values()
               if pattern in str(c.get("pattern", "")) and c.get("n", 0) >= min_n]
        out.sort(key=lambda x: x.get("win_rate", 0), reverse=True)
        return out

    def cycle_winrate(self, cycle: str, min_n: int = 3) -> List[Dict[str, Any]]:
        """某情绪周期下各模式的历史 T+1 胜率（按胜率降序）。"""
        data = self._load_winrate()
        if not data:
            return []
        out = [c for c in data.get("cells", {}).values()
               if cycle in str(c.get("cycle", "")) and c.get("n", 0) >= min_n]
        out.sort(key=lambda x: x.get("win_rate", 0), reverse=True)
        return out

    def winrate_top(self, top: int = 8, min_n: int = 3) -> List[Dict[str, Any]]:
        """全局历史高胜率组合 Top-N。"""
        data = self._load_winrate()
        if not data:
            return []
        cells = [c for c in data.get("cells", {}).values() if c.get("n", 0) >= min_n]
        cells.sort(key=lambda x: x.get("win_rate", 0), reverse=True)
        return cells[:top]

    def cycle_distribution(self, days: int = 60) -> Dict[str, int]:
        """最近 N 天情绪周期分布。"""
        if not self._ok():
            return {}
        with self._conn() as c:
            dates = [r[0] for r in c.execute(
                "SELECT date FROM daily_snapshot ORDER BY date DESC LIMIT ?", (days,)).fetchall()]
            if not dates:
                return {}
            ph = ",".join("?" * len(dates))
            rows = c.execute(
                f"SELECT cycle_name, count(*) FROM daily_snapshot WHERE date IN ({ph}) "
                f"AND cycle_name IS NOT NULL GROUP BY cycle_name ORDER BY 2 DESC", dates).fetchall()
            return {r[0]: r[1] for r in rows}
