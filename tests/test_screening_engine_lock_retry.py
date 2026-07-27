from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd

from core.screening.screening_engine import ScreeningEngine


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_load_candidates_retries_transient_duckdb_lock(monkeypatch, tmp_path):
    db_path = tmp_path / "factors.duckdb"
    db_path.touch()
    attempts = []
    connection = _Connection()

    def connect(_path):
        attempts.append(_path)
        if len(attempts) == 1:
            raise RuntimeError("Cannot open file: another program is using this file")
        return connection

    monkeypatch.setitem(sys.modules, "duckdb", SimpleNamespace(connect=connect))
    monkeypatch.setattr("core.screening.screening_engine.time.sleep", lambda _delay: None)

    engine = ScreeningEngine(
        duckdb_path=db_path,
        output_dir=tmp_path / "screening",
        weight_repository=SimpleNamespace(resolve=lambda *_args, **_kwargs: None),
    )

    stock = pd.DataFrame(
        [{"code": "000001.SZ", "name": "sample", "pct_chg": 1.0, "pre_close": 10.0}]
    )
    monkeypatch.setattr(
        engine,
        "_read_table",
        lambda _con, table, _date: stock.copy() if table == "factor_stock_wide" else pd.DataFrame(),
    )

    result = engine.load_candidates("20260720")

    assert len(attempts) == 2
    assert connection.closed is True
    assert result.iloc[0]["code"] == "000001"
