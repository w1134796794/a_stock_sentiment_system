"""Data quality checks for Phase 1 ETL outputs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd


@dataclass
class QualityIssue:
    table: str
    level: str
    check: str
    message: str
    count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "level": self.level,
            "check": self.check,
            "message": self.message,
            "count": self.count,
        }


@dataclass
class QualityReport:
    trade_date: str
    tables: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    issues: List[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    def add_table(self, name: str, df: Optional[pd.DataFrame]) -> None:
        self.tables[name] = {
            "rows": 0 if df is None else int(len(df)),
            "columns": [] if df is None else list(df.columns),
        }

    def add_issue(self, table: str, level: str, check: str, message: str, count: int = 0) -> None:
        self.issues.append(QualityIssue(table, level, check, message, int(count or 0)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "ok": self.ok,
            "tables": self.tables,
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def write_json(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_markdown(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# ETL Quality Report {self.trade_date}",
            "",
            f"- status: {'OK' if self.ok else 'HAS_ERROR'}",
            "",
            "## Tables",
            "",
            "| table | rows | columns |",
            "|---|---:|---:|",
        ]
        for table, meta in self.tables.items():
            lines.append(f"| {table} | {meta.get('rows', 0)} | {len(meta.get('columns', []))} |")
        lines += ["", "## Issues", ""]
        if not self.issues:
            lines.append("No issues.")
        else:
            lines += ["| level | table | check | count | message |", "|---|---|---|---:|---|"]
            for issue in self.issues:
                msg = issue.message.replace("|", "\\|")
                lines.append(f"| {issue.level} | {issue.table} | {issue.check} | {issue.count} | {msg} |")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _missing_required(df: pd.DataFrame, required_cols: Iterable[str]) -> List[str]:
    return [col for col in required_cols if col not in df.columns]


def _blank_count(series: pd.Series) -> int:
    if series.empty:
        return 0
    return int(series.isna().sum() + (series.astype(str).str.strip() == "").sum())


def _invalid_trade_date_count(series: pd.Series) -> int:
    text = series.astype(str).str.strip()
    return int((~text.str.match(r"^\d{8}$")).sum())


def check_frame(
    report: QualityReport,
    table: str,
    df: Optional[pd.DataFrame],
    *,
    key_cols: Iterable[str],
    required_cols: Iterable[str],
    positive_cols: Iterable[str] = (),
) -> None:
    report.add_table(table, df)
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        report.add_issue(table, "warn", "empty", "表为空", 0)
        return

    missing = _missing_required(df, required_cols)
    if missing:
        report.add_issue(table, "error", "missing_columns", f"缺少必需列: {missing}", len(missing))
        return

    for col in required_cols:
        blanks = _blank_count(df[col])
        if blanks:
            level = "error" if col in key_cols else "warn"
            report.add_issue(table, level, "blank_required", f"{col} 存在空值", blanks)

    keys = [col for col in key_cols if col in df.columns]
    if keys:
        dup_count = int(df.duplicated(keys).sum())
        if dup_count:
            report.add_issue(table, "error", "duplicate_key", f"主键重复: {keys}", dup_count)

    if "trade_date" in df.columns:
        invalid_dates = _invalid_trade_date_count(df["trade_date"])
        if invalid_dates:
            report.add_issue(table, "error", "invalid_trade_date", "trade_date 不是 YYYYMMDD", invalid_dates)

    for col in positive_cols:
        if col not in df.columns:
            continue
        values = pd.to_numeric(df[col], errors="coerce").fillna(0)
        bad = int((values <= 0).sum())
        if bad:
            report.add_issue(table, "warn", "non_positive", f"{col} 存在非正值", bad)

    if {"high", "low"} <= set(df.columns):
        high = pd.to_numeric(df["high"], errors="coerce").fillna(0)
        low = pd.to_numeric(df["low"], errors="coerce").fillna(0)
        bad_range = int(((high > 0) & (low > 0) & (high < low)).sum())
        if bad_range:
            report.add_issue(table, "error", "price_range", "high 小于 low", bad_range)


def build_quality_report(trade_date: str, frames: Dict[str, pd.DataFrame]) -> QualityReport:
    report = QualityReport(trade_date=str(trade_date))
    check_frame(
        report,
        "stock_daily_silver",
        frames.get("stock_daily_silver"),
        key_cols=("trade_date", "code"),
        required_cols=("trade_date", "code", "ts_code", "open", "high", "low", "close", "source", "as_of_date"),
        positive_cols=("open", "high", "low", "close"),
    )
    check_frame(
        report,
        "sector_daily_silver",
        frames.get("sector_daily_silver"),
        key_cols=("trade_date", "sector_code"),
        required_cols=("trade_date", "sector_code", "open", "high", "low", "close", "source", "as_of_date"),
        positive_cols=("open", "high", "low", "close"),
    )
    check_frame(
        report,
        "index_daily_silver",
        frames.get("index_daily_silver"),
        key_cols=("trade_date", "index_code"),
        required_cols=("trade_date", "index_code", "open", "high", "low", "close", "source", "as_of_date"),
        positive_cols=("open", "high", "low", "close"),
    )
    return report
