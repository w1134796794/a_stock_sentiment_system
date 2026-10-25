from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from backtest.confirmation_quality import ConfirmationPolicy, policy_from_config
from core.realtime.collection_plan import digest
from core.signals.production_rules import candidate_strength, minute_evaluator
from core.signals.sector_trend import VERSION, assess_trend, make_trend_checker
from risk.risk_config import RiskConfig


def evidence(direction=1):
    now = datetime.fromisoformat("2026-10-26T09:36:00+08:00")
    plan = {
        "market_date": "20261026",
        "codes": ["sh880001"],
        "rows": [
            {
                "ths_name": "板块",
                "score_eligible": True,
                "score_code": "sh880001",
                "score_mapping_basis": "preopen_membership_overlap_ge_0.8",
            }
        ],
    }
    plan["plan_id"] = digest(plan)
    points = [
        {
            "code": "sh880001",
            "date": "20261026",
            "board_plan_id": plan["plan_id"],
            "received_at": (now - timedelta(seconds=180 - i * 10)).isoformat(),
            "last_price": 100 + direction * i / 18,
            "open_price": 100,
            "pre_close": 99 if direction > 0 else 101,
            "source": "eltdx_board_snapshot",
        }
        for i in range(19)
    ]
    return now, {"plan": plan, "points": points}


@pytest.mark.parametrize("direction,expected", [(1, 5), (-1, -5)])
def test_actual_index_direction_scores_without_constituent_quotes(direction, expected):
    now, inputs = evidence(direction)
    result = assess_trend({"板块"}, inputs, now=now)
    assert result["assessed"] and result["score_adjustment"] == expected
    assert result["role"] == "score_only" and result["entry_veto"] is False
    row = {"screening_score": 70, "sector_trend": result}
    assert candidate_strength(row) == 70 + expected


@pytest.mark.parametrize(
    "fault", ["missing", "stale", "future", "gap", "unmapped", "reference", "wrong_date", "wrong_plan"]
)
def test_missing_or_inconsistent_board_evidence_is_neutral_not_a_veto(fault):
    now, inputs = evidence()
    if fault == "missing":
        inputs["points"] = []
    elif fault == "stale":
        now += timedelta(seconds=21)
    elif fault == "future":
        inputs["points"] = inputs["points"][-1:]
        inputs["points"][0]["received_at"] = (now + timedelta(seconds=1)).isoformat()
    elif fault == "gap":
        del inputs["points"][6:10]
    elif fault == "unmapped":
        inputs["plan"]["rows"][0]["score_eligible"] = False
    elif fault == "reference":
        inputs["points"][0]["open_price"] = 99
    elif fault == "wrong_date":
        inputs["plan"]["market_date"] = "20261023"
    else:
        for row in inputs["points"]:
            row["board_plan_id"] = "wrong"
    result = assess_trend({"板块"}, inputs, now=now)
    assert result["assessed"] is False and result["score_adjustment"] == 0
    assert candidate_strength({"screening_score": 70, "sector_trend": result}) == 70


def test_board_daily_red_is_not_automatically_a_positive_trend_score():
    now, inputs = evidence(-1)
    for row in inputs["points"]:
        row["pre_close"] = 98
    result = assess_trend({"板块"}, inputs, now=now)
    assert result["assessed"] and result["score_adjustment"] < 0


def test_missing_mapped_board_is_neutral_in_the_score_denominator():
    now, inputs = evidence()
    inputs["plan"]["codes"].append("sh880002")
    inputs["plan"]["rows"].append({"ths_name": "另一板块", "score_eligible": True, "score_code": "sh880002"})
    result = assess_trend({"板块", "另一板块"}, inputs, now=now)
    assert result["score_adjustment"] == 2.5 and result["data_coverage"] == 0.5
    row = {"turn_score": 75, "turn_score_before_feedback": 70, "sector_trend": assess_trend({"板块"}, inputs, now=now)}
    assert candidate_strength(row) == 75  # The displayed adjusted score is not adjusted twice.


def test_future_snapshot_cannot_rewrite_an_earlier_score_and_plan_hash_is_checked():
    now, inputs = evidence()
    baseline = assess_trend({"板块"}, inputs, now=now)
    future = deepcopy(inputs["points"][-1])
    future.update(received_at=(now + timedelta(seconds=10)).isoformat(), last_price=1)
    inputs["points"].append(future)
    assert assess_trend({"板块"}, inputs, now=now) == baseline
    check, detail = make_trend_checker({"板块"}, inputs, date="20261026")
    assert check("09:36:00") is True and detail["trend_factor"]["score_adjustment"] == 5
    inputs["plan"]["rows"][0]["score_code"] = "sh880002"
    check, detail = make_trend_checker({"板块"}, inputs, date="20261026")
    assert check("09:36:00") is None


def test_invalid_optional_board_payload_is_neutral():
    _, inputs = evidence()
    inputs["points"] = [None]
    check, detail = make_trend_checker({"板块"}, inputs, date="20261026")
    assert check("09:36:00") is None
    assert detail["trend_factor"]["score_adjustment"] == 0


def test_new_production_policy_and_old_frozen_policy_are_distinct():
    assert not minute_evaluator(RiskConfig()).confirmation_policy.require_sector
    assert minute_evaluator(RiskConfig.from_dict({})).confirmation_policy.require_sector
    assert policy_from_config(RiskConfig.load()).require_sector is False
    assert ConfirmationPolicy().require_sector  # Explicit old low-level research contract.
    with pytest.raises(ValueError):
        minute_evaluator(RiskConfig(entry_sector_mode="unknown"))


@pytest.mark.parametrize("state", [None, False])
def test_stock_snapshot_confirmation_does_not_require_sector_and_keeps_price_checks(state):
    from test_snapshot_primary import primary

    from backtest.minute_entry import MinuteEntryEvaluator

    policy = ConfirmationPolicy(mode="volume_price", require_sector=False)
    decision, fallback = primary(
        evaluator=MinuteEntryEvaluator(confirmation_policy=policy), sector_sync=lambda at: state
    )
    assert not fallback and decision.status == "confirmed"
    assert decision.sector_confirmed is False
    rejected, _ = primary(
        evaluator=MinuteEntryEvaluator(confirmation_policy=policy),
        sector_sync=lambda at: True,
        expected_amount_fraction=None,
    )
    assert rejected.status != "confirmed" and "模型" in rejected.reason


@pytest.mark.parametrize("state", [None, False])
def test_minute_confirmation_keeps_volume_price_thresholds_without_sector_gate(state):
    from test_volume_price_confirmation import bars

    from backtest.minute_entry import MinuteEntryEvaluator

    policy = ConfirmationPolicy(mode="volume_price", require_sector=False)
    engine = MinuteEntryEvaluator(confirmation_policy=policy)
    args = dict(mode="weak_only", open_gap=0, prev_close=10, plan_amount_ratio=1.5, sector_sync=lambda at: state)
    result = engine.evaluate(bars=bars(), **args)
    assert result.status == "filled" and result.sector_confirmed is False
    weak = bars()
    weak.loc[5, "volume"] = 90
    assert engine.evaluate(bars=weak.iloc[:7], **args).status != "filled"


def test_backfill_new_policy_does_not_expand_sector_members(monkeypatch, tmp_path):
    from core.operations.replay_research import FrozenCalendar
    from core.realtime.day_replay import DayReplayLoader

    monkeypatch.setattr(
        "core.realtime.sector_evidence.prepare_sector_cache", lambda *a, **kw: pytest.fail("No constituent acquisition")
    )
    loader = DayReplayLoader(
        web_data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "missing",
        calendar=FrozenCalendar(["20261021", "20261022"]),
    )
    packet = loader.run(
        {"market_date": "20261022", "until": "15:01:00", "profile": "production", "codes": [], "fetch_missing": False},
        inputs_only=True,
        rule_risk=RiskConfig().to_dict(),
        pool_inputs={
            "inputs": {},
            "bundle_id": "",
            "market_date": "20261022",
            "candidate_date": "20261021",
            "rows": [{"code": "600001", "resonance_sectors": "板块"}],
        },
    )
    assert packet["members"] == {} and packet["minute_acquisition"]["total"] == 1
    assert packet["board_trend_inputs"] == {"plan": None, "points": []}


def test_new_live_plan_collects_only_the_two_stock_pools(tmp_path):
    from test_market_process_pipeline import DAY, PREOPEN, bundle, make_plans

    from core.realtime.eltdx_collector import EltdxQuoteCollector

    plans, _ = make_plans(tmp_path)
    data = bundle("trend_score")
    data["inputs"]["sector_members"]["board"] = [f"{i:06d}" for i in range(1000)]
    data["inputs"]["sector_codes"] = {}  # Optional scoring metadata cannot prevent the stock plan.
    plan = plans.freeze(data, now=PREOPEN)
    collector = EltdxQuoteCollector.__new__(EltdxQuoteCollector)
    collector.plans, collector.fixed_codes = plans, []
    collector.clock = lambda: datetime.strptime(DAY + "09:36:00", "%Y%m%d%H:%M:%S")
    assert plan["sector_confirmation_mode"] == "trend_score"
    assert collector.targets() == collector.targets(minutes_only=True)
    assert len(collector.targets()) == 35


def test_archived_snapshot_replay_without_sector_archives_still_confirms_stock(monkeypatch):
    from test_snapshot_archive_replay import sample

    from core.realtime.snapshot_replay import replay_snapshot_inputs

    values = sample(sector_mode="trend_score")
    values["events"] = [row for row in values["events"] if row["kind"] != "sector_minutes"]

    def forbidden(*a, **kw):
        pytest.fail("Archive scoring cannot access providers or hot storage")

    monkeypatch.setattr("core.signals.sector_trend.BoardTrendReader", forbidden)
    report = replay_snapshot_inputs(values)
    stock = report["stocks"][0]
    assert stock["first_confirm_time"] == "09:36:03"
    assert stock["last_signal"]["sector_trend"]["assessed"] is False
    assert stock["last_signal"]["sector_trend"]["score_adjustment"] == 0
    assert stock["last_signal"]["sector_detail"]["data_completeness"] == 0
    assert stock["last_signal"]["data_completeness"] == 100


def test_new_readiness_does_not_require_missing_constituent_archive(tmp_path):
    import sqlite3

    from test_market_readiness import healthy_inputs, run

    _, now, ledger, live, _, _, points, journal = healthy_inputs(tmp_path, sector_mode="trend_score")
    points.clear()
    with sqlite3.connect(journal.path) as con:
        con.execute("DELETE FROM market_events WHERE kind='sector_minutes'")
    report = run(tmp_path, now, ledger, live)
    assert report["live_ready"] and report["replay_ready"]
    assert report["sector_confirmation_mode"] == "trend_score"


def test_backtest_and_day_replay_share_new_board_trend_score():
    from types import SimpleNamespace

    import pandas as pd

    from backtest.backtest_engine import BacktestConfig, BacktestEngine
    from core.realtime.day_replay import _ReplayEntryService

    now, inputs = evidence()
    config = BacktestConfig.from_risk_config(RiskConfig())
    config.production_rules = True
    engine = BacktestEngine(None, config)
    engine._production_inputs = {"board_trend_inputs": inputs}
    historical = engine._sector_sync_checker(pd.Series({"共振板块": "板块"}), "20261026", "000001")
    service = _ReplayEntryService(
        {}, {}, {}, {}, SimpleNamespace(), evaluator=minute_evaluator(RiskConfig()), board_trend_inputs=inputs
    )
    checker, detail = service._sector_checker({"sector_names": ["板块"]}, "000001", [], {}, {}, "20261026")
    assert historical("09:36:00") is checker("09:36:00") is True
    assert detail["trend_factor"]["score_adjustment"] == 5
    assert engine._sector_evidence[("20261026", "000001")]["trend_factor"] == detail["trend_factor"]
