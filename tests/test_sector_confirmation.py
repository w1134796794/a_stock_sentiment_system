from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from core.factors.strategy_training import StrategyMinuteTrainingBuilder
from core.realtime.entry_signal_service import RealtimeEntrySignalService
from core.signals.sector_confirmation import (
    SECTOR_CONFIRMATION_VERSION,
    make_sector_sync_checker,
    select_sector_peers,
)

DATE = "20260921"
PLAN_DATE = "20260918"


def peer(code, sectors="robotics", **kwargs):
    return {"code": code, "resonance_sectors": sectors, **kwargs}


def minutes(close=11.0, pre_close=10.0, **kwargs):
    return pd.DataFrame([
        {"time": "09:35:00", "close": close, "open": 8.0, "high": max(close, 8.0),
         "low": min(close, 8.0), "volume": 100.0, "pre_close": pre_close, "trade_date": DATE, **kwargs},
        {"time": "11:00:00", "close": 20.0, "open": 20.0, "high": 20.0,
         "low": 20.0, "volume": 100.0, "pre_close": pre_close, "trade_date": DATE, **kwargs},
    ])


def fallback(rows, frames, quotes=None, provider=None):
    service = RealtimeEntrySignalService.__new__(RealtimeEntrySignalService)
    service.sector_breadth = provider or SimpleNamespace(
        evaluate=lambda *args: (None, {}),
        _members=lambda name, date: {"000002", "000003", "000004", "000005"}
        if name in {"robotics", "auto"} else set(),
    )
    checker, detail = service._sector_checker(
        peer("000001", '["robotics", "auto"]'), "000001", rows, frames, quotes or {}, DATE,
    )
    return service, checker, detail


def test_peer_selection_exact_multisector_unique_without_self_or_st():
    rows = [
        peer("000001.SZ"), peer("000002", "auto"), peer("000002.SZ", "auto"),
        peer("000003", "robotics_extended"), peer("000004", '["robotics"]'),
        peer("000005", "auto", is_st=True), peer("000006", "auto", name="*ST\u6d4b\u8bd5"),
        peer("000007", "auto", context={"is_st": True}),
        peer("000008", "auto"), peer("000008", "auto", is_st=True),
    ]
    assert [row["code"] for row in select_sector_peers("000001", ["robotics", "auto"], rows)] == [
        "000002", "000004",
    ]


@pytest.mark.parametrize("positive,total,expected", [(2, 2, True), (1, 2, False), (11, 20, True), (10, 20, False)])
def test_minimum_samples_and_exact_55_percent_boundary(positive, total, expected):
    rows = [peer(f"{index + 2:06d}") for index in range(total)]
    frames = {row["code"]: minutes(11 if index < positive else 9) for index, row in enumerate(rows)}
    detail = {}
    checker = make_sector_sync_checker("000001", "robotics", rows, frames, evidence=detail, trade_date=DATE)
    assert checker("09:35:00") is expected
    assert detail["observed_count"] == total
    assert detail["positive_count"] == positive
    assert detail["sector_confirmation_version"] == SECTOR_CONFIRMATION_VERSION
    assert detail["reference_basis"] == "previous_close"


def test_flat_is_observed_but_not_rising_and_open_is_not_the_baseline():
    rows = [peer("000002"), peer("000003")]
    detail = {}
    checker = make_sector_sync_checker(
        "000001", "robotics", rows, {"000002": minutes(10), "000003": minutes(9)}, evidence=detail,
    )
    assert checker("09:35") is False
    assert detail["observed_count"] == 2
    assert detail["positive_count"] == 0
    assert checker("11:00:00") is True
    assert checker("09:35:00") is False


def test_one_distinct_real_sample_is_insufficient():
    rows = [peer("000001"), peer("000002"), peer("000002.SZ"), peer("000003", is_st=True)]
    checker = make_sector_sync_checker(
        "000001.SZ", "robotics", rows,
        {code: minutes() for code in ("000001", "000002", "000003")},
    )
    assert checker("09:35:00") is None


@pytest.mark.parametrize("invalid", [
    minutes(pre_close=0), minutes(pre_close=float("inf")), minutes(close=float("nan")),
    minutes(close=float("inf")), minutes(source="simulated"), minutes(is_simulated=True),
    minutes(trade_date="20260922"), minutes(datetime="2026-09-22T09:35:00"), pd.DataFrame(),
])
def test_invalid_samples_are_missing_evidence_not_negative_votes(invalid):
    detail = {}
    checker = make_sector_sync_checker(
        "000001", "robotics", [peer("000002"), peer("000003")],
        {"000002": minutes(), "000003": invalid}, trade_date=DATE, evidence=detail,
    )
    assert checker("09:35:00") is None
    assert detail["observed_count"] == 1
    assert "000003" in detail["peer_failures"]


def test_future_prices_and_previous_close_are_not_borrowed():
    frame = minutes(pre_close=0)
    frame.loc[1, "pre_close"] = 10
    detail = {}
    checker = make_sector_sync_checker(
        "000001", "robotics", [peer("000002"), peer("000003")],
        {"000002": minutes(), "000003": frame.iloc[::-1]}, evidence=detail,
    )
    assert checker("09:35:00") is None
    assert checker("11:00:00") is True
    assert checker("09:34:00") is None
    assert checker("not-a-time") is None


def test_explicit_previous_close_falls_back_only_when_invalid():
    rows = [peer("000002"), peer("000003")]
    frames = {row["code"]: minutes(11, pre_close=100) for row in rows}
    checker = make_sector_sync_checker(
        "000001", "robotics", rows, frames, previous_closes={"000002.SZ": 10, "000003": 10},
    )
    assert checker("09:35:00") is True
    checker = make_sector_sync_checker(
        "000001", "robotics", rows, frames, previous_closes={"000002": float("nan"), "000003": 0},
    )
    assert checker("09:35:00") is False


def test_realtime_fallback_requires_two_filtered_peers_and_records_version():
    rows = [peer("000001"), peer("000002", '["auto"]'), peer("000003", "robotics"),
            peer("000004", "auto", is_st=True), peer("000005", "auto")]
    frames = {row["code"]: minutes(9) for row in rows}
    quotes = {"000005": {"name": "*ST\u6d4b\u8bd5", "pre_close": 10}}
    _, checker, detail = fallback(rows, frames, quotes)
    assert checker("09:35:00") is False
    assert detail["peer_codes"] == ["000002", "000003"]
    assert detail["observed_members"] == 2
    assert detail["sector_confirmation_version"] == SECTOR_CONFIRMATION_VERSION
    assert checker("11:00:00") is True
    frames["000003"] = pd.DataFrame()
    _, checker, detail = fallback(rows, frames, quotes)
    assert checker("09:35:00") is None
    assert detail["data_completeness"] == 0.0


@pytest.mark.parametrize("index_state", [False, True])
def test_real_index_keeps_priority_and_observation_time_constraint(index_state):
    provider = SimpleNamespace(evaluate=lambda *args: (index_state, {
        "observed_at": "2026-09-21T09:55:00+08:00", "data_completeness": 1.0,
    }))
    _, checker, detail = fallback(
        [peer("000002"), peer("000003")], {"000002": minutes(), "000003": minutes()}, provider=provider,
    )
    assert checker("09:35:00") is None
    assert checker("09:55:00") is index_state
    assert checker("09:58:00") is None
    assert detail["sector_confirmation_source"] == "sector_index_breadth"
    assert detail["sector_confirmation_version"] == "sector_index_breadth_v1"


@pytest.fixture
def training_builder():
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE factor_stock_wide(code VARCHAR, ts_code VARCHAR, trade_date VARCHAR, "
                       "resonance_sectors VARCHAR, total_score DOUBLE, name VARCHAR, is_st BOOLEAN)")
    connection.executemany("INSERT INTO factor_stock_wide VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("000001", "000001.SZ", PLAN_DATE, "robotics", 100, "target", False),
        ("000004", "000004.SZ", PLAN_DATE, "robotics_extended", 99, "substring", False),
        ("000005", "000005.SZ", PLAN_DATE, "auto", 98, "risk", True),
        ("000006", "000006.SZ", DATE, "robotics", 97, "future", False),
        ("000002", "000002.SZ", PLAN_DATE, "auto", 80, "peer1", False),
        ("000002", "000002.SZ", PLAN_DATE, "auto", 79, "duplicate", False),
        ("000003", "000003.SZ", PLAN_DATE, '["robotics"]', 78, "peer2", False),
    ])
    connection.execute("CREATE TABLE stock_daily_silver(code VARCHAR, trade_date VARCHAR, close DOUBLE)")
    connection.execute("INSERT INTO stock_daily_silver VALUES ('000002', '20260918', 10), "
                       "('000003', '20260918', 10)")
    builder = StrategyMinuteTrainingBuilder.__new__(StrategyMinuteTrainingBuilder)
    builder._connection = connection
    yield builder
    connection.close()


def test_training_and_realtime_share_multisector_previous_close_and_asof_rules(training_builder):
    frames = {"000002": minutes(9).drop(columns="pre_close"), "000003": minutes(9).drop(columns="pre_close")}
    loaded = []
    def load(ts_code, trade_date):
        loaded.append((ts_code, trade_date))
        return frames[ts_code.split(".")[0]]
    training_builder._load_tick = load
    train = training_builder._sector_sync_callback(PLAN_DATE, DATE, "000001", '["robotics", "auto"]')
    _, realtime, _ = fallback(
        [peer("000002", "auto"), peer("000003", "robotics")], frames,
        {"000002": {"pre_close": 10}, "000003": {"pre_close": 10}},
    )
    assert loaded == [("000002.SZ", DATE), ("000003.SZ", DATE)]
    for time, state in [("09:34:00", None), ("09:35:00", False), ("11:00:00", True)]:
        assert train(time) is realtime(time) is state


def test_training_reads_old_partitions_without_optional_risk_columns(training_builder):
    training_builder._connection.execute("ALTER TABLE factor_stock_wide DROP COLUMN is_st")
    training_builder._connection.execute("ALTER TABLE factor_stock_wide DROP COLUMN name")
    training_builder._load_tick = lambda code, date: minutes() if code in {"000002.SZ", "000003.SZ"} else pd.DataFrame()
    checker = training_builder._sector_sync_callback(PLAN_DATE, DATE, "000001", ["robotics", "auto"])
    assert checker("09:35:00") is True


def test_training_audit_records_version_when_no_candidates():
    builder = StrategyMinuteTrainingBuilder.__new__(StrategyMinuteTrainingBuilder)
    builder._candidate_map = lambda strategy_id: {}
    result = builder.apply(pd.DataFrame(), strategy_id="mainline_leader", horizon_days=3, allowed_entry_modes=[])
    assert result.empty
    assert builder.audit["sector_confirmation_version"] == SECTOR_CONFIRMATION_VERSION


def test_training_label_records_confirmation_version(monkeypatch):
    import core.factors.strategy_training as training
    builder = StrategyMinuteTrainingBuilder.__new__(StrategyMinuteTrainingBuilder)
    builder._load_tick = lambda *args: minutes(open=10.0)
    builder._daily_window = lambda *args: pd.DataFrame([
        {"trade_date": PLAN_DATE, "close": 10, "amount_yuan": 1000000, "vol_hand": 1000},
        {"trade_date": DATE, "close": 11, "high": 11.2, "low": 10.5},
    ])
    builder._load_auction = lambda *args: {}
    builder._sector_sync_callback = lambda *args: lambda time: True
    monkeypatch.setattr(training, "MinuteEntryEvaluator", lambda: SimpleNamespace(evaluate=lambda **kwargs:
        SimpleNamespace(filled=True, entry_price=11.0, entry_time="09:36:00", signal="weak_to_strong")))
    labelled, reason = builder._label_row(
        pd.Series({"entry_date": DATE, "trade_date": PLAN_DATE, "code": "000001"}),
        peer("000001"), strategy_id="mainline_leader", horizon_days=1, allowed_entry_modes=["weak_only"],
    )
    assert reason == ""
    assert labelled["sector_confirmation_version"] == SECTOR_CONFIRMATION_VERSION


def test_training_with_only_one_loaded_peer_cannot_confirm(training_builder):
    training_builder._load_tick = lambda code, date: minutes() if code == "000002.SZ" else pd.DataFrame()
    assert training_builder._sector_sync_callback(PLAN_DATE, DATE, "000001", ["robotics", "auto"]) is None


def test_realtime_index_error_uses_the_shared_fallback():
    def unavailable(*args):
        raise RuntimeError("index unavailable")
    _, checker, detail = fallback(
        [peer("000002"), peer("000003")], {"000002": minutes(), "000003": minutes()},
        provider=SimpleNamespace(evaluate=unavailable, _members=lambda *args:{"000002","000003"}),
    )
    assert checker("09:35:00") is True
    assert detail["sector_confirmation_version"] == SECTOR_CONFIRMATION_VERSION


def test_realtime_payload_exposes_the_confirmation_version():
    from backtest.minute_entry import ENTRY_WEAK, EntryDecision
    service = RealtimeEntrySignalService.__new__(RealtimeEntrySignalService)
    service.signal_stats = SimpleNamespace(get=lambda *args, **kwargs: {})
    payload = service._payload(
        EntryDecision(status="observe"), ENTRY_WEAK, DATE,
        sector_detail={"sector_confirmation_version": SECTOR_CONFIRMATION_VERSION},
    )
    assert payload["sector_confirmation_version"] == SECTOR_CONFIRMATION_VERSION


def test_shared_checker_preserves_specific_missing_minute_diagnostics():
    detail = {"peer_failures": {"000003": "missing 20 trading minutes"}}
    checker = make_sector_sync_checker(
        "000001", "robotics", [peer("000002"), peer("000003")], {"000002": minutes()}, evidence=detail,
    )
    assert checker("09:35:00") is None
    assert detail["peer_failures"]["000003"] == "missing 20 trading minutes"


def test_dated_null_timestamps_do_not_discard_legacy_minutes():
    frames = {code: minutes(datetime=None) for code in ("000002", "000003")}
    checker = make_sector_sync_checker(
        "000001", "robotics", [peer("000002"), peer("000003")], frames, trade_date=DATE,
    )
    assert checker("09:35:00") is True
