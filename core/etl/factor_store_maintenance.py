"""Archive and compact the large factor long table without blocking daily jobs."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_EXPLAIN_FACTORS = (
    "mkt_%",
    "sec_mainline_score",
    "sec_behavior_%",
    "sec_lhb_%",
    "stk_total_score",
    "stk_limit_progress",
    "stk_sector_mainline_score",
    "stk_sector_resonance_score",
    "stk_relative_strength_sector",
    "stk_behavior_%",
    "stk_lhb_%",
    "stk_capital_flow_%",
)


@dataclass
class FactorStoreMaintenanceResult:
    archive_before: str
    archive_path: str = ""
    archived_rows: int = 0
    active_rows_before: int = 0
    active_rows_after: int = 0
    deleted_rows: int = 0
    compacted_rows: int = 0
    dry_run: bool = True
    verified: bool = False
    manifest_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def rows_selected(self) -> int:
        """Compatibility name used by dry-run callers and audit reports."""
        return self.archived_rows


class FactorStoreMaintenance:
    """Move old long-form evidence to compressed Parquet and keep a lean live table.

    Deletion is deliberately opt-in. The archive is written and counted first;
    the DuckDB partition is deleted only after row-count verification succeeds.
    """

    def __init__(
        self, *, duckdb_path: Path | None = None, db_path: Path | None = None,
        archive_dir: Path | None = None,
    ) -> None:
        from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

        self.duckdb_path = Path(duckdb_path or db_path or FACTOR_DB_PATH)
        self.archive_dir = Path(
            archive_dir or Path(WEB_DATA_DIR) / "warehouse" / "archive" / "factor_value_long"
        )

    def archive(
        self, archive_before: str, *, prune: bool = False, dry_run: bool = True,
    ) -> FactorStoreMaintenanceResult:
        date = str(archive_before or "").replace("-", "")
        if len(date) != 8 or not date.isdigit():
            raise ValueError("archive_before 必须是 YYYYMMDD")
        if not self.duckdb_path.exists():
            raise FileNotFoundError(self.duckdb_path)
        import duckdb  # type: ignore

        result = FactorStoreMaintenanceResult(archive_before=date, dry_run=bool(dry_run))
        with duckdb.connect(str(self.duckdb_path)) as con:
            if not self._table_exists(con, "factor_value_long"):
                return result
            result.active_rows_before = int(
                con.execute("SELECT COUNT(*) FROM factor_value_long").fetchone()[0]
            )
            result.archived_rows = int(con.execute(
                "SELECT COUNT(*) FROM factor_value_long WHERE CAST(trade_date AS VARCHAR) < ?",
                [date],
            ).fetchone()[0])
            if not result.archived_rows or dry_run:
                result.active_rows_after = result.active_rows_before
                return result

            self.archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = self.archive_dir / f"before_{date}.parquet"
            escaped = str(archive_path).replace("'", "''")
            con.execute(
                "COPY (SELECT * FROM factor_value_long "
                "WHERE CAST(trade_date AS VARCHAR) < ?) "
                f"TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)",
                [date],
            )
            result.archive_path = str(archive_path)
            archived_count = int(con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{escaped}')"
            ).fetchone()[0])
            result.verified = archived_count == result.archived_rows
            if prune and result.verified:
                con.execute(
                    "DELETE FROM factor_value_long WHERE CAST(trade_date AS VARCHAR) < ?",
                    [date],
                )
                result.deleted_rows = result.archived_rows
                con.execute("CHECKPOINT")
            result.active_rows_after = int(
                con.execute("SELECT COUNT(*) FROM factor_value_long").fetchone()[0]
            )
        return self._write_manifest(result)

    def archive_before(
        self, archive_before: str, *, prune: bool = False, dry_run: bool = True,
    ) -> FactorStoreMaintenanceResult:
        """Archive rows older than a date; retained as the public, explicit API."""
        return self.archive(archive_before, prune=prune, dry_run=dry_run)

    def compact_live(
        self, *, keep_patterns: Sequence[str] = DEFAULT_EXPLAIN_FACTORS,
        dry_run: bool = True,
    ) -> FactorStoreMaintenanceResult:
        """Deduplicate live rows and optionally keep only explain/sparse factors."""
        import duckdb  # type: ignore

        result = FactorStoreMaintenanceResult(archive_before="live", dry_run=bool(dry_run))
        with duckdb.connect(str(self.duckdb_path)) as con:
            if not self._table_exists(con, "factor_value_long"):
                return result
            result.active_rows_before = int(con.execute(
                "SELECT COUNT(*) FROM factor_value_long"
            ).fetchone()[0])
            clause, params = self._pattern_clause(keep_patterns)
            selected = int(con.execute(
                "SELECT COUNT(*) FROM factor_value_long WHERE " + clause, params,
            ).fetchone()[0])
            result.compacted_rows = selected
            if dry_run:
                result.active_rows_after = result.active_rows_before
                return result
            con.execute("DROP TABLE IF EXISTS factor_value_long_compacted")
            con.execute(
                "CREATE TABLE factor_value_long_compacted AS "
                "SELECT * EXCLUDE(_row_number) FROM ("
                "SELECT *, ROW_NUMBER() OVER (PARTITION BY trade_date, entity_type, entity_id, factor_id "
                "ORDER BY computed_at DESC) AS _row_number FROM factor_value_long WHERE "
                + clause + ") WHERE _row_number=1",
                params,
            )
            con.execute("BEGIN TRANSACTION")
            try:
                con.execute("ALTER TABLE factor_value_long RENAME TO factor_value_long_precompact")
                con.execute("ALTER TABLE factor_value_long_compacted RENAME TO factor_value_long")
                con.execute("DROP TABLE factor_value_long_precompact")
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            con.execute("CHECKPOINT")
            result.active_rows_after = int(con.execute(
                "SELECT COUNT(*) FROM factor_value_long"
            ).fetchone()[0])
            result.verified = result.active_rows_after <= result.compacted_rows
        return self._write_manifest(result)

    @staticmethod
    def _table_exists(con, table: str) -> bool:
        return bool(con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?", [table],
        ).fetchone()[0])

    @staticmethod
    def _pattern_clause(patterns: Iterable[str]) -> tuple[str, list[str]]:
        values = [str(pattern) for pattern in patterns if str(pattern)]
        if not values:
            return "1=1", []
        return " OR ".join("factor_id LIKE ?" for _ in values), values

    def _write_manifest(
        self, result: FactorStoreMaintenanceResult,
    ) -> FactorStoreMaintenanceResult:
        target = self.archive_dir / "manifests"
        target.mkdir(parents=True, exist_ok=True)
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = target / f"maintenance_{result.archive_before}_{suffix}.json"
        result.manifest_path = str(path)
        path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return result


__all__ = [
    "DEFAULT_EXPLAIN_FACTORS", "FactorStoreMaintenance",
    "FactorStoreMaintenanceResult",
]
