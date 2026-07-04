"""Persist leader lifecycle signals and attach matured forward outcomes."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd


class LeaderOutcomeTracker:
    def __init__(self, duckdb_path: Optional[Path] = None) -> None:
        if duckdb_path is None:
            from config.settings import FACTOR_DB_PATH

            duckdb_path = FACTOR_DB_PATH
        self.duckdb_path = Path(duckdb_path)

    def record(self, signal_date: str, rows: Iterable[Dict[str, Any]]) -> int:
        records = []
        for row in rows or []:
            records.append({
                "signal_date": str(signal_date),
                "code": str(row.get("code") or "").zfill(6),
                "name": str(row.get("name") or ""),
                "lifecycle_state": str(row.get("lifecycle_state") or ""),
                "pool_type": str(row.get("pool_type") or ""),
                "primary_sector": str(row.get("primary_sector") or ""),
                "leader_score": float(row.get("leader_score") or 0.0),
                "sector_status_score": float(row.get("sector_status_score") or 0.0),
                "market_status_score": float(row.get("market_status_score") or 0.0),
                "capital_recognition_score": float(row.get("capital_recognition_score") or 0.0),
                "safety_score": float(row.get("safety_score") or 0.0),
                "next_3d_excess_return": None,
                "mfe_3d": None,
                "mae_3d": None,
                "success": None,
                "outcome_date": "",
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            })
        if not records or not self.duckdb_path.exists():
            return 0
        import duckdb  # type: ignore

        frame = pd.DataFrame(records)
        con = duckdb.connect(str(self.duckdb_path))
        try:
            con.register("_leader_signals", frame)
            con.execute("CREATE TABLE IF NOT EXISTS leader_signal_history AS SELECT * FROM _leader_signals WHERE 1=0")
            con.execute("DELETE FROM leader_signal_history WHERE CAST(signal_date AS VARCHAR)=?", [str(signal_date)])
            con.execute("INSERT INTO leader_signal_history SELECT * FROM _leader_signals")
        finally:
            try:
                con.unregister("_leader_signals")
            except Exception:
                pass
            con.close()
        return len(records)

    def refresh_outcomes(self, as_of_date: str) -> int:
        if not self.duckdb_path.exists():
            return 0
        import duckdb  # type: ignore

        con = duckdb.connect(str(self.duckdb_path))
        try:
            tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            if not {"leader_signal_history", "signal_outcome_wide"}.issubset(tables):
                return 0
            before = con.execute(
                "SELECT COUNT(*) FROM leader_signal_history WHERE next_3d_excess_return IS NULL"
            ).fetchone()[0]
            con.execute(
                "UPDATE leader_signal_history AS l SET "
                "next_3d_excess_return=o.next_3d_excess_return, mfe_3d=o.mfe_3d, mae_3d=o.mae_3d, "
                "success=o.label_success, outcome_date=o.future_date "
                "FROM signal_outcome_wide AS o "
                "WHERE l.signal_date=o.trade_date AND l.code=o.code "
                "AND CAST(o.future_date AS VARCHAR) < ? AND l.next_3d_excess_return IS NULL",
                [str(as_of_date)],
            )
            after = con.execute(
                "SELECT COUNT(*) FROM leader_signal_history WHERE next_3d_excess_return IS NULL"
            ).fetchone()[0]
            return max(int(before - after), 0)
        finally:
            con.close()

    def stats(self, lifecycle_state: str, *, as_of_date: str) -> Dict[str, Any]:
        if not self.duckdb_path.exists():
            return {}
        try:
            import duckdb  # type: ignore

            con = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                exists = con.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='leader_signal_history'"
                ).fetchone()[0]
                if not exists:
                    return {}
                row = con.execute(
                    "SELECT COUNT(*), AVG(success), AVG(next_3d_excess_return), "
                    "AVG(CASE WHEN mae_3d <= -0.05 THEN 1 ELSE 0 END), AVG(mfe_3d), AVG(mae_3d) "
                    "FROM leader_signal_history WHERE lifecycle_state=? "
                    "AND CAST(signal_date AS VARCHAR) < ? AND next_3d_excess_return IS NOT NULL",
                    [str(lifecycle_state), str(as_of_date)],
                ).fetchone()
            finally:
                con.close()
        except Exception:
            return {}
        if not row or not row[0]:
            return {}
        return {
            "sample_size": int(row[0]),
            "success_probability": float(row[1] or 0.0),
            "expected_return": float(row[2] or 0.0),
            "stop_probability": float(row[3] or 0.0),
            "average_mfe": float(row[4] or 0.0),
            "average_mae": float(row[5] or 0.0),
        }


__all__ = ["LeaderOutcomeTracker"]
