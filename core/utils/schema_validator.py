"""
DataFrame 数据契约校验框架（P2-5）

设计目标：
1. 不引入外部依赖（pandera 体量较大，且我们的契约相对简单）。
2. 声明式：用 `Schema` + `Column` 描述期望，`assert_schema` 检查。
3. 校验失败时给出可读错误并附 sample 行，便于调试。
4. 支持 `strict=False` 仅记日志，`strict=True` 直接抛错。

用法：
    from core.utils.schema_validator import assert_schema, Schema, Column

    LimitUpSchema = Schema(
        name="LimitUpPool",
        columns=[
            Column("ts_code",  required=True,  allow_null=False),
            Column("name",     required=True),
            Column("first_time", required=False),
        ],
        min_rows=0,
    )

    assert_schema(df, LimitUpSchema, strict=False)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Union

import pandas as pd
import loguru

logger = loguru.logger


class SchemaValidationError(ValueError):
    """数据契约校验失败"""


@dataclass
class Column:
    """单列契约描述"""

    name: str
    required: bool = True
    allow_null: bool = True
    dtype: Optional[str] = None      # 例如 'object', 'float64', 'int64'; None 表示不检查
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    allowed_values: Optional[List] = None
    validator: Optional[Callable[[pd.Series], bool]] = None  # 自定义校验回调


@dataclass
class Schema:
    """DataFrame 整体契约"""

    name: str
    columns: List[Column] = field(default_factory=list)
    min_rows: int = 0
    max_rows: Optional[int] = None
    unique_keys: List[Union[str, List[str]]] = field(default_factory=list)

    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    def required_columns(self) -> List[str]:
        return [c.name for c in self.columns if c.required]


@dataclass
class SchemaReport:
    """校验报告"""

    schema_name: str
    passed: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{self.schema_name}] {status} (errors={len(self.errors)}, warnings={len(self.warnings)})"


def assert_schema(df: Optional[pd.DataFrame], schema: Schema,
                  strict: bool = False, log_on_fail: bool = True) -> SchemaReport:
    """
    校验 df 是否满足 schema。

    Args:
        df:           待校验 DataFrame。允许 None（视作 0 行）。
        schema:       Schema 对象
        strict:       True 时校验失败抛 SchemaValidationError，否则只填充报告
        log_on_fail:  发生 error/warning 时是否打印日志

    Returns:
        SchemaReport
    """
    report = SchemaReport(schema_name=schema.name, passed=True)

    if df is None:
        df = pd.DataFrame()

    # 1. 行数
    row_count = len(df)
    if row_count < schema.min_rows:
        report.errors.append(f"rows={row_count} < min_rows={schema.min_rows}")
    if schema.max_rows is not None and row_count > schema.max_rows:
        report.errors.append(f"rows={row_count} > max_rows={schema.max_rows}")

    # 2. 必需列存在性
    for col in schema.columns:
        if col.required and col.name not in df.columns:
            report.errors.append(f"missing required column: {col.name}")

    # 3. 逐列校验（仅校验存在的列）
    for col in schema.columns:
        if col.name not in df.columns:
            continue
        series = df[col.name]

        if not col.allow_null and series.isna().any():
            null_count = int(series.isna().sum())
            report.errors.append(
                f"column '{col.name}' has {null_count} null values (allow_null=False)"
            )

        if col.dtype is not None:
            actual_dtype = str(series.dtype)
            if not actual_dtype.startswith(col.dtype):
                report.warnings.append(
                    f"column '{col.name}' dtype={actual_dtype}, expected {col.dtype}"
                )

        non_null = series.dropna()
        if col.min_value is not None and len(non_null) > 0:
            try:
                if (non_null < col.min_value).any():
                    bad = int((non_null < col.min_value).sum())
                    report.errors.append(
                        f"column '{col.name}' has {bad} values < {col.min_value}"
                    )
            except TypeError:
                report.warnings.append(
                    f"column '{col.name}' min_value check skipped (non-numeric)"
                )

        if col.max_value is not None and len(non_null) > 0:
            try:
                if (non_null > col.max_value).any():
                    bad = int((non_null > col.max_value).sum())
                    report.errors.append(
                        f"column '{col.name}' has {bad} values > {col.max_value}"
                    )
            except TypeError:
                report.warnings.append(
                    f"column '{col.name}' max_value check skipped (non-numeric)"
                )

        if col.allowed_values is not None and len(non_null) > 0:
            invalid = non_null[~non_null.isin(col.allowed_values)]
            if not invalid.empty:
                report.errors.append(
                    f"column '{col.name}' has {len(invalid)} values outside allowed set; "
                    f"sample={list(invalid.head(3))}"
                )

        if col.validator is not None and len(non_null) > 0:
            try:
                if not bool(col.validator(non_null)):
                    report.errors.append(f"column '{col.name}' failed custom validator")
            except Exception as e:
                report.errors.append(f"column '{col.name}' validator raised: {e}")

    # 4. 唯一性约束
    for key in schema.unique_keys:
        keys = [key] if isinstance(key, str) else list(key)
        missing = [k for k in keys if k not in df.columns]
        if missing:
            report.warnings.append(
                f"unique_keys {keys} skipped, missing columns: {missing}"
            )
            continue
        dup_count = int(df.duplicated(subset=keys).sum())
        if dup_count > 0:
            report.errors.append(f"unique_keys {keys} duplicated rows={dup_count}")

    report.passed = len(report.errors) == 0

    if log_on_fail and (report.errors or report.warnings):
        for w in report.warnings:
            logger.warning(f"[Schema:{schema.name}] WARN {w}")
        for e in report.errors:
            logger.error(f"[Schema:{schema.name}] ERROR {e}")

    if strict and not report.passed:
        raise SchemaValidationError(
            f"Schema '{schema.name}' validation failed: " + "; ".join(report.errors)
        )

    return report


# =============================================================================
# 项目内置 schema —— 关键数据契约
# =============================================================================

# 涨停池
LIMIT_UP_POOL = Schema(
    name="LimitUpPool",
    columns=[
        Column("ts_code", required=True, allow_null=False),
        Column("name", required=False),
        Column("first_time", required=False),
        Column("last_time", required=False),
        Column("up_stat", required=False),
    ],
    min_rows=0,
)

# 个股日线
STOCK_DAILY = Schema(
    name="StockDaily",
    columns=[
        Column("ts_code", required=True, allow_null=False),
        Column("trade_date", required=True, allow_null=False),
        Column("open", required=True, dtype="float"),
        Column("high", required=True, dtype="float"),
        Column("low", required=True, dtype="float"),
        Column("close", required=True, dtype="float"),
        Column("vol", required=False, min_value=0),
        Column("amount", required=False, min_value=0),
        Column("pct_chg", required=False),
    ],
    unique_keys=[["ts_code", "trade_date"]],
)

# 资金流向汇总
MONEYFLOW_SUMMARY = Schema(
    name="MoneyflowSummary",
    columns=[
        Column("ts_code", required=True, allow_null=False),
        Column("buy_elg_amount", required=False),
        Column("sell_elg_amount", required=False),
        Column("buy_lg_amount", required=False),
        Column("sell_lg_amount", required=False),
        Column("net_mf_amount", required=False),
    ],
)

# 板块日线
SECTOR_DAILY = Schema(
    name="SectorDaily",
    columns=[
        Column("ts_code", required=True, allow_null=False),
        Column("trade_date", required=True, allow_null=False),
        Column("close", required=False, dtype="float"),
        Column("pct_change", required=False),
        Column("vol", required=False, min_value=0),
        Column("turnover_rate", required=False),
    ],
)

# Layer4 交易计划 CSV
TRADE_PLAN = Schema(
    name="TradePlan",
    columns=[
        Column("stock_code", required=True, allow_null=False),
        Column("stock_name", required=False),
        Column("entry_price", required=True, dtype="float", min_value=0),
        Column("target_price", required=False, dtype="float", min_value=0),
        Column("stop_loss", required=False, dtype="float", min_value=0),
        Column("position", required=False, dtype="float", min_value=0, max_value=1),
    ],
)


__all__ = [
    "Column",
    "Schema",
    "SchemaReport",
    "SchemaValidationError",
    "assert_schema",
    "LIMIT_UP_POOL",
    "STOCK_DAILY",
    "MONEYFLOW_SUMMARY",
    "SECTOR_DAILY",
    "TRADE_PLAN",
]
