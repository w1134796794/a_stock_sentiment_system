"""Validate minute evidence without filling missing prices or trading minutes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

import numpy as np
import pandas as pd


SHANGHAI = timezone(timedelta(hours=8))


def inspect_minute_bars(frame: pd.DataFrame | None, trade_date: str, *,
                        require_full_day: bool = True,
                        expected_code: str = "",
                        now: datetime | None = None) -> dict[str, Any]:
    date = str(trade_date).replace("-", "")
    result: dict[str, Any] = {
        "valid": False, "trade_date": date, "evidence": "minute_bars",
        "rows": 0, "expected_minutes": 240, "missing_count": 0,
        "missing_times": [], "issues": [], "reason": "",
    }
    issues = result["issues"]
    if not re.fullmatch(r"\d{8}", date):
        issues.append("交易日期无效")
    if frame is None or frame.empty:
        issues.append("分钟数据为空")
        result["reason"] = "；".join(issues)
        return result
    result["rows"] = len(frame)
    if expected_code:
        canonical = re.sub(r"\D", "", expected_code.split(".")[0]).zfill(6)
        for column in ("ts_code", "code"):
            if column in frame and frame[column].notna().any():
                codes = frame[column].astype(str).str.split(".").str[0].str.replace(r"\D", "", regex=True).str.zfill(6)
                if not codes.eq(canonical).all():
                    issues.append("分钟数据股票代码与请求不一致")
    for column in ("trade_date", "date", "datetime"):
        if column in frame:
            if frame[column].isna().all():
                continue
            observed = pd.to_datetime(frame[column].astype(str), errors="coerce")
            if observed.isna().any() or not observed.dt.strftime("%Y%m%d").eq(date).all():
                issues.append(f"{column} 与请求日期不一致或无法解析")
    time_column = next((c for c in ("time", "datetime") if c in frame), "")
    if not time_column:
        issues.append("缺少分钟时间字段")
        result["reason"] = "；".join(issues)
        return result
    texts = frame[time_column].astype(str).str.strip()
    dated = texts.str.match(r"^\d{4}-?\d{2}-?\d{2}[ T]")
    if dated.any():
        observed = pd.to_datetime(texts[dated], errors="coerce")
        if observed.isna().any() or not observed.dt.strftime("%Y%m%d").eq(date).all():
            issues.append("分钟时间所属日期不一致")
    times = texts.str.replace("T", " ", regex=False).str.split().str[-1]
    times = times.where(times.str.len().ne(5), times + ":00")
    valid_time = times.str.match(r"^(?:[01]\d|2[0-3]):[0-5]\d:00$")
    if not valid_time.all():
        issues.append("存在无效或非整分钟时间")
    session = times.between("09:30:00", "11:30:00") | times.between("13:00:00", "15:00:00")
    # Auction rows may coexist with continuous trading data, but never count as bars.
    continuous = valid_time & session
    actual = set(times[continuous])
    if times[continuous].duplicated().any():
        issues.append("分钟时间重复")
    price_column = next((c for c in ("close", "price") if c in frame), "")
    volume_column = next((c for c in ("volume", "vol") if c in frame), "")
    for column, positive in ((price_column, True), (volume_column, False)):
        if not column:
            issues.append("缺少价格字段" if positive else "缺少成交量字段")
            continue
        values = pd.to_numeric(frame.loc[continuous, column], errors="coerce")
        invalid = ~np.isfinite(values) | (values.le(0) if positive else values.lt(0))
        if invalid.any():
            issues.append(f"{column} 存在无效数值")
    for column in ("open", "high", "low"):
        if column in frame:
            values = pd.to_numeric(frame.loc[continuous, column], errors="coerce")
            if (~np.isfinite(values) | values.le(0)).any():
                issues.append(f"{column} 存在无效数值")
    if all(column in frame for column in ("open", "high", "low", "close")):
        prices = frame.loc[continuous, ["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        if (prices["high"].lt(prices[["open", "low", "close"]].max(axis=1))
                | prices["low"].gt(prices[["open", "high", "close"]].min(axis=1))).any():
            issues.append("分钟OHLC价格结构不一致")
    if "amount" in frame:
        values = pd.to_numeric(frame.loc[continuous, "amount"], errors="coerce")
        if (~np.isfinite(values) | values.lt(0)).any():
            issues.append("amount 存在无效数值")
    clock = now or datetime.now(SHANGHAI)
    clock = clock.replace(tzinfo=SHANGHAI) if clock.tzinfo is None else clock.astimezone(SHANGHAI)
    if date > clock.strftime("%Y%m%d"):
        issues.append("请求日期尚未发生")
    grids = []
    labels = set(frame['bar_label'].dropna()) if 'bar_label' in frame else set()
    offsets = (1,) if labels == {'end'} else (0,) if labels == {'start'} else (0, 1)
    for offset in offsets:
        grid = set()
        for start in (9 * 60 + 30, 13 * 60):
            for minute in range(start + offset, start + 120 + offset):
                time = f"{minute // 60:02d}:{minute % 60:02d}:00"
                cutoff = clock if labels == {'end'} else clock - timedelta(minutes=1)
                if require_full_day or time <= cutoff.strftime("%H:%M:00"):
                    grid.add(time)
        grids.append(grid)
    expected = min(grids, key=lambda grid: (len(grid - actual), -len(grid)))
    missing = sorted(expected - actual)
    result.update({"expected_minutes": len(expected), "observed_minutes": len(actual),
                   "missing_count": len(missing), "missing_times": missing,
                   "first_time": min(actual) if actual else "",
                   "last_time": max(actual) if actual else ""})
    if missing:
        issues.append(f"缺少 {len(missing)} 个交易分钟（{', '.join(missing[:8])}）")
    if len(actual) < 2:
        issues.append("有效交易分钟不足")
    result["valid"] = not issues
    result["reason"] = "；".join(issues)
    return result


def minute_failure(data_manager: Any, frame: pd.DataFrame | None,
                   trade_date: str, ts_code: str) -> dict[str, Any]:
    report = inspect_minute_bars(frame, trade_date, expected_code=ts_code)
    getter = getattr(data_manager, "get_minute_quality_report", None)
    if callable(getter):
        report = getter(ts_code, trade_date) or report
    return {**report, "trade_date": trade_date, "ts_code": ts_code,
            "code": ts_code.split(".")[0], "evidence": "minute_bars"}
