"""Read-only, cache-first daily evidence for the shared board-feedback rules."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pandas as pd

from backtest.trade_calendar import TradeCalendar
from core.signals.board_feedback import date_text, history_dates
from core.signals.opening import positive_price
from core.utils.price_limit import normalize_code


def feedback_inputs(market_date, codes, dates, records, *, source_basis, calendar_verified=True):
    wanted = {normalize_code(code) for code in codes if normalize_code(code)}
    stocks = {code: [] for code in wanted}
    for record in records:
        code = normalize_code(record.get("code") or record.get("ts_code"))
        day = date_text(record.get("trade_date"))
        if code not in wanted or day not in dates or day >= market_date:
            continue
        stocks[code].append({"code": code, "trade_date": day,
                             "name": str(record.get("name") or ""),
                             "close": positive_price(record.get("close")),
                             "pre_close": positive_price(record.get("pre_close"))})
    return {"market_date": market_date, "dates": list(dates), "stocks": stocks,
            "source_basis": source_basis, "calendar_verified": calendar_verified}


class BoardFeedbackRepository:
    def __init__(self, cache_dir=None, *, db_path=None, calendar=None):
        from config.settings import CACHE_DIR, FACTOR_DB_PATH
        self.cache = Path(cache_dir or CACHE_DIR)
        self.database = Path(db_path or FACTOR_DB_PATH)
        self.calendar = calendar or TradeCalendar()
        self._cached = None

    def capture(self, market_date, codes, *, frozen_inputs=None):
        codes = list(codes)
        try:
            if frozen_inputs is not None:
                captured = frozen_inputs.get("board_feedback_inputs") or {}
                return deepcopy({**captured, "stocks": {
                    normalize_code(code): (captured.get("stocks") or {}).get(normalize_code(code), []) for code in codes}})
            return self._capture_local(market_date, codes)
        except Exception as exc:
            from loguru import logger
            logger.warning("三板负反馈因子历史不可用，保留未评估状态: {}", type(exc).__name__)
            return feedback_inputs(market_date, codes, [], [], source_basis="optional_history_unavailable",
                                   calendar_verified=False)

    def _capture_local(self, market_date, codes):
        dates = history_dates(market_date, self.calendar)
        paths = [self.cache / "stock" / "all_daily" / f"{day}.csv" for day in dates]
        revisions = tuple(path.stat().st_mtime_ns if path.exists() else None for path in paths)
        signature = (market_date, revisions,
                     self.database.stat().st_mtime_ns if None in revisions and self.database.exists() else None)
        if self._cached is None or self._cached[0] != signature:
            records, missing = [], []
            for day, path in zip(dates, paths):
                try:
                    frame = pd.read_csv(path, dtype={"code": str, "ts_code": str})
                    if "trade_date" not in frame.columns:
                        frame["trade_date"] = day
                    records.extend(frame.to_dict("records"))
                except (OSError, ValueError, pd.errors.ParserError):
                    missing.append(day)
            if missing and self.database.exists():
                from core.infrastructure.duckdb_access import connect
                try:
                    with connect(self.database, read_only=True) as con:
                        placeholders = ",".join("?" for _ in missing)
                        records.extend(con.execute(
                            f"SELECT * FROM stock_daily_silver WHERE CAST(trade_date AS VARCHAR) IN ({placeholders})",
                            missing).fetchdf().to_dict("records"))
                except Exception:
                    pass  # Missing rows remain explicit, never manufactured as non-limit days.
            self._cached = (signature, records)
        return feedback_inputs(market_date, codes, dates, self._cached[1],
                               source_basis="local_prior_daily_not_frozen",
                               calendar_verified=bool(getattr(self.calendar, "is_real", False))
                               and self.calendar.is_trade_date(market_date))
