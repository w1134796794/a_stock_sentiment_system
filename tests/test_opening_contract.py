import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.matching_rules import open_gap_pct
from backtest.minute_entry import EntryDecision, MinuteEntryEvaluator
from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.day_replay import DayReplayLoader, _ReplayEntryService, replay_minutes, replay_saved_inputs
from core.realtime.entry_signal_service import RealtimeEntrySignalService, classify_entry_mode
from core.realtime.quote_cache import RealtimeQuoteCache
from core.realtime.session_open_repository import SessionOpenRepository, quote_opening
from core.signals.opening import opening_context, opening_gap, sector_reference_rows


DATE = "20261015"
CLOCK = datetime.fromisoformat("2026-10-15T09:40:00+08:00")


def quote(code="000001", **extra):
    return {"code": code, "date": DATE, "time": "09:39:00", "open_price": 10.2,
            "pre_close": 10, "last_price": 10.8, "source": "eltdx_batch", "quality_ok": True,
            "received_at": "2026-10-15T09:39:00.000+08:00", "is_stale": False, **extra}


def bars(code="000001"):
    return pd.DataFrame([{"code": code, "trade_date": DATE, "time": f"09:{minute:02d}:00",
                          "open": 10.8, "high": 10.8, "low": 10.8, "close": 10.8,
                          "volume": 100, "amount": 108000} for minute in range(31, 51)])


def profiles():
    return SimpleNamespace(available=False, load=lambda: {}, expected_fraction=lambda *a: (None, 0))


def repository(tmp_path, name="opening_contract"):
    return SessionOpenRepository(tmp_path, backend=MemoryStateBackend(name + str(tmp_path)))


@pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_prices_have_one_missing_semantics(bad):
    assert opening_gap(bad, 10) is None
    assert open_gap_pct({"open": bad, "pre_close": 10}) is None
    assert classify_entry_mode(bad, 10) == ""
    assert opening_context({"open_price": bad, "pre_close": 10})["open_gap"] is None


@pytest.mark.parametrize("opening,mode", [(10.1, "weak_only"), (10.1001, "continuation_only"),
                                         (10.5, "continuation_only"), (10.5001, "acceleration_only")])
def test_gap_boundaries_match_all_classifiers(opening, mode):
    gap = opening_gap(opening, 10)
    assert gap == open_gap_pct({"open": opening, "pre_close": 10})
    engine = BacktestEngine(None, BacktestConfig())
    assert engine._entry_mode_for_plan(pd.Series(dtype=object), gap) == mode
    assert classify_entry_mode(opening, 10, weak_max_gap=.01, continuation_max_gap=.05) == mode


@pytest.mark.parametrize("extra", [
    {"date": "20261014"}, {"source": "other"}, {"quality_ok": False}, {"is_stale": True},
    {"received_at": ""}, {"received_at": "2026-10-14T09:39:00+08:00"},
    {"received_at": "2026-10-15T09:25:00+08:00"}, {"open_price": 0}, {"pre_close": float("nan")},
])
def test_unverified_quotes_are_not_opening_evidence(tmp_path, extra):
    repo = repository(tmp_path)
    assert quote_opening(quote(**extra), DATE) is None
    assert repo.record_batch([quote(**extra)], now=CLOCK) == 0
    assert not repo.root.exists()


def test_daily_opening_survives_expired_ticks_and_keeps_original_receipt(tmp_path):
    repo = repository(tmp_path)
    assert repo.record_batch([quote()], now=CLOCK) == 1
    path = repo.root / f"{DATE}.json"
    original = path.read_bytes()
    assert repo.record_batch([quote(received_at="2026-10-15T09:40:00+08:00")], now=CLOCK) == 0
    assert path.read_bytes() == original
    # A different node reads the shared day key; a restarted collector can read disk alone.
    remote = SessionOpenRepository(tmp_path / "remote", backend=repo.backend)
    disk = SessionOpenRepository(tmp_path, backend=MemoryStateBackend("empty" + str(tmp_path)))
    assert remote.read(DATE) == disk.read(DATE) == repo.read(DATE)
    assert remote.read("20261014") == {}
    assert repo.read(DATE)["000001"]["received_at"] == quote()["received_at"]
    assert path.read_bytes() == original


def test_corrected_quotes_keep_asof_revisions_without_future_leakage(tmp_path):
    repo = repository(tmp_path)
    repo.record_batch([quote()], now=CLOCK)
    corrected = quote(open_price=10.1, received_at="2026-10-15T09:40:00+08:00")
    assert repo.record_batch([corrected], now=CLOCK) == 1
    assert repo.read(DATE, until="09:39:59")["000001"]["open_price"] == 10.2
    assert repo.read(DATE, until="09:40:00")["000001"]["open_price"] == 10.1
    assert repo.read(DATE, until="09:38:00") == {}
    assert repo.record_batch([quote()], now=CLOCK) == 0


def test_collector_archives_constituent_opens_without_collecting_more_ticks(tmp_path, monkeypatch):
    import core.realtime.quote_cache as module
    monkeypatch.setattr(module, "shanghai_now", lambda: CLOCK)
    repo, tick_rows = repository(tmp_path), []
    cache = RealtimeQuoteCache(backend=repo.backend, opening_repository=repo,
                              snapshot_repository=SimpleNamespace(append_batch=lambda date, rows: tick_rows.extend(rows)))
    # Freshness is measured at the fixed test clock, independent of the host's day.
    monkeypatch.setattr(module.QuoteSnapshot, "with_freshness", lambda self, **kw: self)
    cache.write_batch([quote(), quote("000002")], source="eltdx_batch", collector_id="test",
                      snapshot_codes=["000001"])
    assert [row["code"] for row in tick_rows] == ["000001"]
    assert set(repo.read(DATE)) == {"000001", "000002"}


def test_replay_prefers_eltdx_archive_over_different_daily_open(tmp_path):
    repo = repository(tmp_path)
    repo.record_batch([quote()], now=CLOCK)
    directory = tmp_path / "stock/all_daily"
    directory.mkdir(parents=True)
    pd.DataFrame([{"code": "000001", "trade_date": DATE, "open": 11, "pre_close": 11},
                  {"code": "000002", "trade_date": DATE, "open": 9.9, "pre_close": 10}]).to_csv(directory / f"{DATE}.csv", index=False)
    loader = DayReplayLoader(cache_dir=tmp_path, db_path=tmp_path / "none.duckdb", opening_repository=repo,
                             snapshot_repository=SimpleNamespace(read=lambda *a: []))
    values, source = loader._opening_evidence(DATE, ["000001", "000002"], fetch_missing=False)
    assert values == {"000001": 10.2, "000002": 9.9}
    assert source == "eltdx_open_archive+stock_daily_open_cache"
    assert loader.opening_evidence["000001"]["pre_close"] == 10
    assert loader.opening_evidence["000001"]["received_at"] == quote()["received_at"]


def test_replay_accepts_original_rolling_quote_but_not_a_different_date(tmp_path):
    repo = repository(tmp_path)
    snapshots = SimpleNamespace(read=lambda date, code: [quote(code)])
    loader = DayReplayLoader(cache_dir=tmp_path, db_path=tmp_path / "none.duckdb", opening_repository=repo,
                             snapshot_repository=snapshots)
    values, source = loader._opening_evidence(DATE, ["000001"], fetch_missing=False)
    assert values == {"000001": 10.2} and source == "eltdx_snapshot_cache"
    assert loader._opening_evidence("20261014", ["000001"], fetch_missing=False)[0] == {}
    assert loader._opening_evidence(DATE, ["000001"], fetch_missing=False, until="09:38:00")[0] == {}


def test_quote_fetch_is_same_day_opt_in_and_never_relabels_historical_quotes(tmp_path, monkeypatch):
    import core.realtime.day_replay as module
    monkeypatch.setattr(module, "shanghai_now", lambda: CLOCK)
    calls = []
    dm = SimpleNamespace(get_quote_snapshots=lambda codes: calls.append(codes) or {"000001": quote()})
    loader = DayReplayLoader(cache_dir=tmp_path, db_path=tmp_path / "none.duckdb", data_manager=dm,
                             opening_repository=repository(tmp_path), snapshot_repository=SimpleNamespace(read=lambda *a: []))
    assert loader._opening_evidence(DATE, ["000001"], fetch_missing=False)[0] == {}
    assert loader._opening_evidence("20261014", ["000001"], fetch_missing=True)[0] == {}
    assert calls == []
    assert loader._opening_evidence(DATE, ["000001"], fetch_missing=True)[0] == {"000001": 10.2}
    assert calls == [["000001"]]


def test_no_live_minute_fallback_and_same_opening_inputs_in_three_adapters(monkeypatch):
    class Recorder(MinuteEntryEvaluator):
        def __init__(self):
            super().__init__(weak_max_gap=.025)
            self.seen = []
        def evaluate(self, **kwargs):
            self.seen.append((kwargs["mode"], kwargs["open_gap"], kwargs["prev_close"]))
            return EntryDecision("observing", reason="test", open_gap_pct=kwargs["open_gap"])

    previous = {"000001": {"close": 11}}
    evidence = {"000001": quote()}
    live_recorder, replay_recorder, history_recorder = Recorder(), Recorder(), Recorder()
    live = RealtimeEntrySignalService(evaluator=live_recorder, amount_profile_repository=profiles(),
                                     signal_stats_repository=SimpleNamespace(get=lambda *a, **kw: {}))
    replay = _ReplayEntryService(previous, {}, {}, {"000001": bars()}, profiles(), {"000001": 10.2},
                                 evaluator=replay_recorder, opening_evidence=evidence)
    for service in (live, replay):
        monkeypatch.setattr(service, "_snapshot_frame", lambda *a: pd.DataFrame())
        monkeypatch.setattr(service, "_sector_checker", lambda *a: (lambda t: True, {"data_completeness": 1}))
        signal = service._evaluate_one({"code": "000001"}, quote(time="09:51:00"), bars(), {}, {}, previous["000001"], DATE, [], {})
        assert signal["entry_mode"] == "weak_only"
        assert signal["opening_evidence"]["reference_source"] == "session_pre_close"
    invalid = live._evaluate_one({"code": "000001"}, quote(open_price=None), bars(), {}, {}, previous["000001"], DATE, [], {})
    assert invalid["data_status"] == "missing_session_open" and invalid["entry_mode"] == ""
    assert len(live_recorder.seen) == 1

    engine = BacktestEngine(None, BacktestConfig(production_rules=True, three_board_feedback_enabled=False))
    engine.minute_evaluator = history_recorder
    engine._production_inputs = {"previous": previous, "opening_evidence": evidence, "opening_prices": {"000001": 10.2}}
    monkeypatch.setattr(engine, "_is_full_day_suspended", lambda *a, **kw: False)
    monkeypatch.setattr(engine, "_sector_sync_checker", lambda *a: lambda t: True)
    engine._check_buy_conditions(pd.Series({"目标价": 0}), DATE, "000001", "test")
    assert history_recorder.seen == replay_recorder.seen == live_recorder.seen == [("weak_only", .02, 10)]
    assert engine.entry_attempts[-1]["opening_evidence"]["open_price"] == 10.2


def test_saved_replay_freezes_opening_and_thresholds_together():
    report = replay_minutes(request={"market_date": DATE, "until": "09:51:00", "profile": "leader_pool"},
                            candidate_date="20261014", rows=[{"code": "000001"}], frames={"000001": bars()},
                            previous={"000001": {"close": 11}}, profiles=profiles(),
                            opening_prices={"000001": 10.2}, opening_evidence={"000001": quote()},
                            risk_config={"weak_entry_max_gap": .025})
    stock = report["stocks"][0]
    assert stock["price_evidence"]["entry_mode"] == stock["last_signal"]["entry_mode"] == "weak_only"
    assert stock["price_evidence"]["previous_close"] == 10
    repeated = replay_saved_inputs(report["replay_inputs"])
    assert repeated["input_hash"] == report["input_hash"]
    assert repeated["stocks"] == report["stocks"]


def test_sector_reference_uses_exchange_preclose_without_overwriting_previous_session():
    previous = {"000001": {"close": 11, "amount": 123}}
    observed = sector_reference_rows(previous, {"000001": {"pre_close": 10}, "000002": {"pre_close": 9}})
    assert observed["000001"] == {"close": 10, "amount": 123}
    assert observed["000002"]["close"] == 9
    assert previous["000001"]["close"] == 11


def test_production_replay_keeps_frozen_strategy_and_entry_thresholds(tmp_path, monkeypatch):
    import core.realtime.day_replay as module
    row = {"code": "000001", "name": "test", "strategy_id": "weak_to_strong",
           "strategy_execution": {"allowed_entry_modes": ["weak_only"], "confirmation_deadline": "09:50:00"},
           "strategy_version": "frozen"}
    inputs = dict(rows=[row], frames={}, previous={}, auctions={"000001": {"竞价成交额": 100}},
                  members={}, sector_codes={}, opening_prices={"000001": 10.2}, opening_price_source="eltdx_open_archive",
                  opening_evidence={"000001": quote()}, risk_config={"weak_entry_max_gap": .025},
                  bundle_id="frozen", input_basis="preopen_frozen")
    class Loader:
        def __init__(self, **kwargs):
            pass
        def run(self, *args, **kwargs):
            return inputs
    monkeypatch.setattr(module, "DayReplayLoader", Loader)
    engine = BacktestEngine(None, BacktestConfig(production_rules=True))
    engine._production_profiles["weak_to_strong"] = {"enabled": False, "version": "current"}
    result = engine._prepare_production_day(DATE)
    assert len(result) == 1
    assert engine._production_rows["000001"]["strategy_version"] == "frozen"
    assert engine.minute_evaluator.weak_max_gap == .025
    assert engine._auction_rows[(DATE, "000001")]["竞价成交额"] == 100
    assert engine._production_input_manifest[-1]["configuration_basis"] == "preopen_frozen"
    json.dumps(engine._production_input_manifest, allow_nan=False)


def test_observation_and_production_use_the_same_frozen_execution_context():
    from core.signals.production_rules import production_candidates, production_context
    leader = {'code': '000001', 'pool_type': '核心龙头', 'leader_score': 80}
    inputs = {'leader_pools': {'10': {'rows': [leader]}},
              'candidates': [{'code': '000001', 'strategy_id': 'weak_to_strong'}],
              'profiles': [{'id': 'weak_to_strong', 'version': 'frozen', 'enabled': True,
                            'execution': {'allowed_entry_modes': ['weak_only']}}]}
    production = production_candidates(inputs)[0]
    observed = production_context([leader], inputs)[0]
    assert {k: observed[k] for k in production} == production
    assert observed['strategy_execution']['allowed_entry_modes'] == ['weak_only']
    assert observed['execution_scope'] == 'production_candidate'
    assert production_context([{'code': '000002'}], inputs)[0]['execution_scope'] == 'observation_only'


def test_price_cli_uses_archived_open_and_frozen_thresholds_without_daily_open(tmp_path, monkeypatch):
    import scripts.diagnose_intraday_signal as diagnostics
    import core.realtime.day_replay as replay_module
    import core.operations.preopen_evidence as preopen
    repo = repository(tmp_path)
    repo.record_batch([quote()], now=CLOCK)
    directory = tmp_path / 'stock/tick'
    directory.mkdir(parents=True)
    bars().to_csv(directory / f'000001.SZ_{DATE}.csv', index=False)
    loader = DayReplayLoader(cache_dir=tmp_path, db_path=tmp_path / 'none.duckdb', opening_repository=repo)
    monkeypatch.setattr(replay_module, 'DayReplayLoader', lambda **kw: loader)
    monkeypatch.setattr(preopen, 'PreopenEvidenceStore', lambda: SimpleNamespace(
        load=lambda *a: {'inputs': {'risk_config': {'weak_entry_max_gap': .025}}}))
    monkeypatch.setattr(diagnostics, 'CACHE_DIR', tmp_path)
    monkeypatch.setattr(diagnostics, 'runtime_evidence', lambda *a: {'rows': []})
    output = tmp_path / 'result.json'
    monkeypatch.setattr(diagnostics.sys, 'argv', ['diagnose', '--date', DATE, '--codes', '000001', '--output', str(output)])
    diagnostics.main()
    stock = json.loads(output.read_text(encoding='utf-8'))['stocks'][0]
    assert stock['entry_mode'] == 'weak_only' and stock['open'] == 10.2
    assert stock['previous_close'] == 10
    assert stock['opening_evidence']['opening_source'] == 'eltdx_open_archive'


def test_archive_failure_does_not_discard_live_quote_and_is_visible(tmp_path, monkeypatch):
    import core.realtime.quote_cache as module
    monkeypatch.setattr(module, 'shanghai_now', lambda: CLOCK)
    monkeypatch.setattr(module.QuoteSnapshot, 'with_freshness', lambda self, **kw: self)
    def fail(*args, **kwargs):
        raise OSError('archive unavailable')
    backend = MemoryStateBackend('opening_failure' + str(tmp_path))
    cache = RealtimeQuoteCache(backend=backend, opening_repository=SimpleNamespace(record_batch=fail),
                              snapshot_repository=SimpleNamespace(append_batch=lambda *a: None))
    result = cache.write_batch([quote()], source='eltdx_batch', collector_id='test')
    assert result['ok'] and result['opening_evidence_error'] == 'archive unavailable'
    assert backend.get_json(cache._quote_key('000001'))['open_price'] == 10.2
