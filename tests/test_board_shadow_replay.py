import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from core.infrastructure.atomic_json import atomic_write_json
from core.realtime.board_plan import BoardCollectionPlan
from core.realtime.board_replay import attach_board_replay, cutoff_for, load_board_inputs, replay_board_inputs
from core.realtime.collection_plan import digest
from scripts.replay_board_shadow import verify_fixture
from tests.test_board_shadow import DAY, OPEN, PREOPEN, Provider, make_store, parent, quote

FIXTURE = Path(__file__).parent / "fixtures" / "board_shadow_replay.json"


def setup_archive(root):
    p = {**parent(), "frozen_at": PREOPEN.isoformat()}
    p["plan_id"] = digest({key: value for key, value in p.items() if key != "plan_id"})
    atomic_write_json(root / "collection_plans" / f"{DAY}.json", p)
    plan = BoardCollectionPlan(root).prepare(p, Provider(), now=PREOPEN)
    store, journal = make_store(root)
    # Match the real configured archive layout, without touching any project data.
    store.archive_root = root / "market_archive"
    from core.realtime.market_journal import MarketJournal

    store.journal = journal = MarketJournal(store.archive_root)
    return store, journal, plan


def test_fixed_synthetic_fixture_is_labeled_and_never_claims_live_validation():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = verify_fixture(fixture)
    assert result["ok"] and result["synthetic"] and not result["live_runtime_verified"]
    assert len(result["checks"]) == 4
    fixture["cases"][0]["expected"]["observed_points"] = 99
    assert not verify_fixture(fixture)["ok"]


def test_missing_files_do_not_fetch_create_state_or_invent_evidence(tmp_path, monkeypatch):
    import core.infrastructure.shared_state as state
    from core.data.providers.eltdx_provider import EltdxProvider

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must never fetch or initialize shared state")

    monkeypatch.setattr(state, "get_shared_state_backend", forbidden)
    monkeypatch.setattr(EltdxProvider, "_client", forbidden)
    root = tmp_path / "absent"
    report = replay_board_inputs(load_board_inputs(root, DAY))
    assert report["status"] == "missing_collection_plan"
    assert not root.exists() and not report["formal_signal"] and not report["remote_fetch_performed"]


def test_archived_and_pending_observations_reconcile_using_original_clock_and_policy(tmp_path):
    store, journal, _ = setup_archive(tmp_path)
    plan = BoardCollectionPlan(tmp_path).load(DAY)
    for seconds in (0, 10):
        stamp = OPEN + timedelta(seconds=seconds, microseconds=321)
        store.write(plan, [quote(stamp)], now=stamp + timedelta(microseconds=987))
        if seconds == 0:
            journal.archive_once()
    until = "09:31:11"
    captured = load_board_inputs(tmp_path, DAY, until=until)
    report = replay_board_inputs(captured)
    assert report["observed_points"] == 2 and report["reconciliation"]["matched"] == 2
    assert report["reconciliation"]["complete"] and not report["live_runtime_verified"]
    assert report["rows"][0]["continuity_ok"]
    assert journal.health()["pending"] == 1
    assert captured == load_board_inputs(tmp_path, DAY, until=until)
    captured["points"][0]["assessment_at_capture"]["status"] = "corrupted"
    altered = replay_board_inputs(captured)
    assert altered["reconciliation"]["different"] == 1
    assert altered["timeline"][0]["differences"]["status"]["captured"] == "corrupted"


def test_future_capture_is_not_backdated_to_the_quote_receipt(tmp_path):
    store, _, plan = setup_archive(tmp_path)
    store.write(plan, [quote(OPEN)], now=OPEN + timedelta(seconds=1))
    report = replay_board_inputs(load_board_inputs(tmp_path, DAY, until="09:31:00"))
    assert report["observed_points"] == 0 and report["rows"][0]["status"] == "missing"


def test_old_snapshots_without_capture_assessment_cannot_be_claimed_as_matched(tmp_path):
    _, journal, plan = setup_archive(tmp_path)
    journal.append("board_snapshot", DAY, [{**quote(), "board_plan_id": plan["plan_id"]}])
    report = replay_board_inputs(load_board_inputs(tmp_path, DAY, until="09:31:01"))
    assert report["reconciliation"]["missing_live_assessment"] == 1
    assert not report["reconciliation"]["complete"]
    assert report["timeline"][0]["captured"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"board_plan_id": "wrong"},
        {"code": "sh880002"},
        {"date": "20261022"},
        {"source": "other"},
        {"received_at": "2026-10-23T09:31:00"},
        {"last_price": -1},
        {"capture_policy": {"stale_seconds": -1, "expected_interval": 10}},
    ],
)
def test_bad_identity_or_evidence_is_reported_not_used(change):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))["inputs"]
    inputs.update(synthetic=True, until="09:31:01")
    inputs["points"] = [{**inputs["points"][0], **change}]
    report = replay_board_inputs(inputs)
    assert report["observed_points"] == 0 and report["failures"]
    assert not report["complete"]


def test_empty_board_archive_is_missing_not_a_passing_shadow_test(tmp_path):
    setup_archive(tmp_path)
    report = replay_board_inputs(load_board_inputs(tmp_path, DAY, until="09:31:01"))
    assert report["status"] == "missing_board_snapshots"
    assert not report["complete"] and report["failures"][0]["evidence"] == "board_snapshot_missing"


def test_frozen_input_replay_is_immutable_and_independent_of_later_market_updates(tmp_path):
    store, _, plan = setup_archive(tmp_path)
    store.write(plan, [quote()], now=OPEN)
    report = {"market_date": DAY, "until": "09:31:01", "replay_inputs": {}}
    attach_board_replay(report, tmp_path)
    inputs = deepcopy(report["replay_inputs"]["board_shadow_inputs"])
    store.write(plan, [quote(OPEN + timedelta(seconds=10))], now=OPEN + timedelta(seconds=10))
    assert replay_board_inputs(inputs) == report["board_shadow"]


def test_synthetic_snapshots_cannot_enter_the_real_collection_store(tmp_path):
    store, journal, plan = setup_archive(tmp_path)
    assert store.write(plan, [quote(synthetic=True)], now=OPEN)["rejected_count"] == 1
    assert journal.health()["pending"] == 0


@pytest.mark.parametrize("date,until", [("../20261023", "10:00"), (DAY, "16:00"), (DAY, "bad"), ("20260230", "10:00")])
def test_invalid_replay_arguments_are_rejected(date, until):
    with pytest.raises(ValueError):
        cutoff_for(date, until)


def test_corrupt_archive_is_reported_as_incomplete(tmp_path):
    setup_archive(tmp_path)
    (tmp_path / "market_archive" / "ingestion.sqlite3").write_bytes(b"not a database")
    report = replay_board_inputs(load_board_inputs(tmp_path, DAY))
    assert report["failures"][0]["evidence"] == "board_archive_or_plan"
    assert not report["complete"]


def test_read_only_web_entry_rejects_bad_date_and_does_not_construct_market_clients(tmp_path, monkeypatch):
    import web.app as module
    from core.data.providers.eltdx_provider import EltdxProvider
    monkeypatch.setattr(module, "WEB_DATA_DIR", tmp_path / "absent")
    monkeypatch.setattr(EltdxProvider, "_client", lambda *args: pytest.fail("must not fetch"))
    response = module.api_board_shadow_replay(DAY, "10:00")
    assert response["ok"] and response["data"]["status"] == "missing_collection_plan"
    assert not (tmp_path / "absent").exists()
    assert module.api_board_shadow_replay("../20261023").status_code == 400
    assert module.api_board_shadow_replay("20990101").status_code == 400


def test_saved_minute_baseline_keeps_board_input_hash_without_loading_new_archives(monkeypatch):
    from core.realtime.day_replay import replay_minutes, replay_saved_inputs
    from tests.test_day_replay import bars, profile, request
    report = replay_minutes(request=request(), candidate_date="20261008", rows=[{"code": "000001"}],
                            frames={"000001": bars()}, previous={"000001": {"close": 10}},
                            opening_prices={"000001": 10}, profiles=profile())
    board_inputs = {**json.loads(FIXTURE.read_text(encoding="utf-8"))["inputs"], "until": "09:31:10", "synthetic": True}
    report["replay_inputs"]["board_shadow_inputs"] = board_inputs
    import core.realtime.board_replay as source
    monkeypatch.setattr(source, "load_board_inputs", lambda *a, **kw: pytest.fail("baseline must use saved inputs"))
    repeated = replay_saved_inputs(report["replay_inputs"])
    assert repeated["board_shadow"]["input_hash"] == source.replay_board_inputs(board_inputs)["input_hash"]
    assert repeated["input_hash"] == report["input_hash"]
