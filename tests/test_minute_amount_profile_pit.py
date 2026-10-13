from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from core.operations.ledger import TradingLedger
from core.operations.replay_research import FrozenCalendar, ReplayResearch
from core.signals.minute_amount_profile import (
    MinuteAmountProfileRepository,
    MinuteAmountProfileTrainer,
    validate_historical_profile,
)


@pytest.fixture
def trained(tmp_path):
    cache = tmp_path / "tick"
    cache.mkdir()
    times = [*pd.date_range("2026-06-30 09:31", periods=120, freq="min"),
             *pd.date_range("2026-06-30 13:01", periods=120, freq="min")]
    for index in range(1, 61):
        code = str(index).zfill(6)
        frame = pd.DataFrame({"time": [value.strftime("%H:%M:%S") for value in times],
            "trade_date": "20260630", "code": code, "open": 10, "high": 10.1,
            "low": 9.9, "close": 10, "volume": 100 + index, "amount": (100 + index) * 10})
        frame.to_csv(cache / f"{code}.SZ_20260630.csv", index=False)
    future = frame.assign(trade_date="20260701", amount=99999999)
    future.to_csv(cache / "000060.SZ_20260701.csv", index=False)
    frame.iloc[:-1].to_csv(cache / "000060.SZ_20260629.csv", index=False)
    repository = MinuteAmountProfileRepository(tmp_path / "profile.json")
    result = MinuteAmountProfileTrainer(cache_dir=cache, repository=repository).train_historical(
        start_date="20260624", end_date="20260630")
    assert result["ok"] is True
    return cache, repository, result


def test_prior_only_profile_has_full_day_curve_and_real_computation_time(trained):
    cache, repository, result = trained
    assert result["sample_days"] == 60
    assert result["historical_training"]["end"] == "20260630"
    assert not result["trained_at"].startswith("2026-06-30")
    fraction, samples = repository.expected_fraction(999999999, "14:30:00")
    assert fraction == pytest.approx(0.875)
    assert samples == 20
    validate_historical_profile(repository.load(), cache, "20260701")


def test_profile_rejects_changed_source(trained):
    cache, repository, result = trained
    source = cache / result["historical_training"]["sources"][0]["file"]
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source changed"):
        validate_historical_profile(repository.load(), cache, "20260701")


def test_profile_rejects_future_declared_training_window(trained):
    cache, repository, _ = trained
    forged = deepcopy(repository.load())
    forged["historical_training"]["requested_end"] = "20260701"
    with pytest.raises(ValueError):
        validate_historical_profile(forged, cache, "20260701")
    forged = deepcopy(repository.load())
    forged["historical_training"]["end"] = "20260701"
    with pytest.raises(ValueError, match="window"):
        validate_historical_profile(forged, cache, "20260701")


def test_profile_cannot_be_used_before_its_last_input(trained):
    cache, repository, _ = trained
    with pytest.raises(ValueError):
        validate_historical_profile(repository.load(), cache, "20260630")


def test_frozen_profile_keeps_original_sources_after_cache_changes(trained, tmp_path, monkeypatch):
    cache, repository, result = trained
    monkeypatch.setattr("core.operations.replay_research.BacktestEngine._load_sector_peer_codes",
                        lambda *args, **kwargs: {})
    service = ReplayResearch(root=tmp_path / "research", ledger=TradingLedger(tmp_path / "ledger.sqlite"))
    manifest = service.freeze(plans_dir=tmp_path / "plans", start="20260701", end="20260701",
        calendar=FrozenCalendar(["20260630", "20260701"]), coverage_dates=["20260630"],
        data_manager=SimpleNamespace(stock_dir=cache.parent), amount_profile_path=repository.path)
    directory = service._directory(manifest["experiment_id"])
    assert manifest["amount_profile_scope"] == "reconstructed_from_prior_complete_minutes"
    assert manifest["amount_profile_data_end"] == "20260630"
    assert len(list((directory / "amount_profile_sources").glob("*.csv"))) == 60
    source = result["historical_training"]["sources"][0]["file"]
    (cache / source).write_bytes(b"changed live cache")
    assert service.verify(manifest["experiment_id"])["data_hash"] == manifest["data_hash"]
    validate_historical_profile(repository.load(), directory / "amount_profile_sources", "20260701")
    (directory / "amount_profile_sources" / source).write_bytes(b"changed frozen source")
    with pytest.raises(ValueError, match="冻结数据"):
        service.verify(manifest["experiment_id"])
