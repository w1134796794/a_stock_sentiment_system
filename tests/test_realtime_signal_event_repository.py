from pathlib import Path

from core.realtime.signal_event_repository import RealtimeSignalEventRepository


def _payload(status: str = "confirmed"):
    return {
        "market_date": "20260807",
        "profile": "decision_pool",
        "counts": {},
        "rows": [{
            "code": "600641.SH",
            "name": "先导基电",
            "entry_mode": "weak_to_strong",
            "entry_mode_text": "弱转强",
            "confirm_status": status,
            "status": status,
            "status_text": "转强确认" if status == "confirmed" else "继续观察",
            "confirm_time": "09:47:00",
            "last_price": 13.21,
        }],
    }


def test_confirmation_remains_visible_after_latest_state_weakens(tmp_path: Path):
    repository = RealtimeSignalEventRepository(tmp_path / "events.sqlite")
    assert repository.record_payload(_payload()) == 1

    current = _payload("observe")
    repository.merge_history(current)

    row = current["rows"][0]
    assert row["was_confirmed_today"] is True
    assert row["status"] == "confirmed_history"
    assert row["status_text"] == "今日曾确认"
    assert row["current_status"] == "observe"
    assert current["counts"]["confirmed"] == 1
    assert current["counts"]["confirmed_history"] == 1


def test_archived_confirmation_is_added_when_stock_leaves_current_pool(tmp_path: Path):
    repository = RealtimeSignalEventRepository(tmp_path / "events.sqlite")
    repository.record_payload(_payload())
    current = {
        "market_date": "20260807",
        "profile": "decision_pool",
        "rows": [],
    }

    repository.merge_history(current)

    assert len(current["rows"]) == 1
    assert current["rows"][0]["name"] == "先导基电"
    assert current["rows"][0]["status"] == "confirmed_history"
