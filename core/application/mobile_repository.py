"""Read-only data access used by mobile and other thin clients."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Dict, List

from config.settings import CACHE_DIR, FACTOR_DB_PATH, WEB_DATA_DIR
from snapshot.artifact_cache import GLOBAL_ARTIFACT_CACHE


def _date_text(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


class MobileReadRepository:
    """Read generated artifacts without invoking any external data provider."""

    def __init__(
        self,
        *,
        web_data_dir: Path | str = WEB_DATA_DIR,
        factor_db_path: Path | str = FACTOR_DB_PATH,
        cache_dir: Path | str = CACHE_DIR,
    ) -> None:
        self.web_data_dir = Path(web_data_dir)
        self.factor_db_path = Path(factor_db_path)
        self.cache_dir = Path(cache_dir)
        self.decision_pool_dir = self.web_data_dir / "screening" / "decision_pool"
        self._cache_lock = RLock()
        self._dates_cache: tuple[float, List[str]] | None = None
        self._decision_cache: Dict[str, tuple[int, Dict[str, Any]]] = {}

    def list_dates(self) -> List[str]:
        now = monotonic()
        with self._cache_lock:
            if self._dates_cache and self._dates_cache[0] > now:
                return list(self._dates_cache[1])
        dates = {
            path.stem.rsplit("_", 1)[-1]
            for path in self.decision_pool_dir.glob("decision_pool_*.json")
            if len(path.stem.rsplit("_", 1)[-1]) == 8
        }
        if self.factor_db_path.exists():
            try:
                with self._connect() as con:
                    dates.update(
                        str(row[0])
                        for row in con.execute(
                            "SELECT DISTINCT trade_date FROM factor_market_wide "
                            "WHERE trade_date IS NOT NULL ORDER BY trade_date DESC"
                        ).fetchall()
                    )
            except Exception:
                pass
        result = sorted((_date_text(value) for value in dates if _date_text(value)), reverse=True)
        with self._cache_lock:
            self._dates_cache = (now + 30.0, result)
        return list(result)

    def latest_date(self) -> str:
        dates = self.list_dates()
        return dates[0] if dates else ""

    def load_decision_pool(self, trade_date: str = "") -> Dict[str, Any]:
        date = _date_text(trade_date) or self.latest_date()
        path = self.decision_pool_dir / f"decision_pool_{date}.json"
        if not path.exists():
            return {}
        try:
            mtime_ns = path.stat().st_mtime_ns
            with self._cache_lock:
                cached = self._decision_cache.get(date)
                if cached and cached[0] == mtime_ns:
                    return dict(cached[1])
            payload = GLOBAL_ARTIFACT_CACHE.load_json(path)
            result = dict(payload) if isinstance(payload, dict) else {}
            with self._cache_lock:
                self._decision_cache[date] = (mtime_ns, result)
                if len(self._decision_cache) > 16:
                    self._decision_cache.pop(next(iter(self._decision_cache)))
            return dict(result)
        except Exception:
            return {}

    def market_snapshot(self, trade_date: str = "") -> Dict[str, Any]:
        date = _date_text(trade_date) or self.latest_date()
        if not date or not self.factor_db_path.exists():
            return {}
        try:
            with self._connect() as con:
                cursor = con.execute(
                    "SELECT * FROM factor_market_wide WHERE trade_date=? LIMIT 1",
                    [date],
                )
                row = cursor.fetchone()
                if not row:
                    return {}
                return dict(zip([item[0] for item in cursor.description], row, strict=False))
        except Exception:
            return {}

    def limit_rows(self, trade_date: str = "", direction: str = "up") -> List[Dict[str, Any]]:
        date = _date_text(trade_date) or self.latest_date()
        table = "limit_up_pool_silver" if direction == "up" else "limit_down_pool_silver"
        if not date or not self.factor_db_path.exists():
            return []
        try:
            with self._connect() as con:
                cursor = con.execute(
                    f"SELECT trade_date, code, ts_code, name, pct_chg, first_time, "
                    f"last_time, open_times, limit_times, fd_amount, float_mv, turnover_ratio "
                    f"FROM {table} WHERE trade_date=? "
                    f"ORDER BY limit_times DESC, first_time ASC, code ASC",
                    [date],
                )
                columns = [item[0] for item in cursor.description]
                return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
        except Exception:
            return []

    def stock_profile(self, code: str, trade_date: str = "") -> Dict[str, Any]:
        pure = str(code or "").split(".", 1)[0].zfill(6)
        date = _date_text(trade_date) or self.latest_date()
        result: Dict[str, Any] = {"code": pure, "trade_date": date}
        pool = self.load_decision_pool(date)
        for row in pool.get("rows") or []:
            if str(row.get("code") or row.get("代码") or "").split(".", 1)[0].zfill(6) == pure:
                result["candidate"] = dict(row)
                result["name"] = str(row.get("name") or row.get("名称") or "")
                break
        if not self.factor_db_path.exists():
            return result
        try:
            with self._connect() as con:
                cursor = con.execute(
                    "SELECT * FROM factor_stock_wide WHERE trade_date=? AND code=? LIMIT 1",
                    [date, pure],
                )
                row = cursor.fetchone()
                if row:
                    result["factors"] = dict(
                        zip(
                            [item[0] for item in cursor.description],
                            row,
                            strict=False,
                        )
                    )
                cursor = con.execute(
                    "SELECT trade_date, code, ts_code, name, open, high, low, close, "
                    "pre_close, pct_chg, vol_hand, amount_yuan "
                    "FROM stock_daily_silver WHERE code=? AND trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT 1",
                    [pure, date],
                )
                row = cursor.fetchone()
                if row:
                    quote = dict(
                        zip(
                            [item[0] for item in cursor.description],
                            row,
                            strict=False,
                        )
                    )
                    result["daily"] = quote
                    result["name"] = result.get("name") or str(quote.get("name") or "")
        except Exception:
            pass
        return result

    def daily_candles(self, code: str, trade_date: str = "", limit: int = 120) -> List[Dict[str, Any]]:
        pure = str(code or "").split(".", 1)[0].zfill(6)
        date = _date_text(trade_date) or self.latest_date()
        if not date or not self.factor_db_path.exists():
            return []
        try:
            with self._connect() as con:
                cursor = con.execute(
                    "SELECT trade_date, open, high, low, close, vol_hand AS volume, amount_yuan AS amount "
                    "FROM stock_daily_silver WHERE code=? AND trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT ?",
                    [pure, date, max(1, min(int(limit or 120), 500))],
                )
                columns = [item[0] for item in cursor.description]
                rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
                return list(reversed(rows))
        except Exception:
            return []

    def _connect(self):
        import duckdb

        return duckdb.connect(str(self.factor_db_path), read_only=True)


__all__ = ["MobileReadRepository"]
