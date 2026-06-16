from core.realtime.overlay_service import RealtimeOverlayService


class FakeQuoteService:
    def get_quotes(self, codes):
        rows = {
            "000001": {
                "code": "000001",
                "name": "平安银行",
                "open_price": 10.2,
                "pre_close": 10.0,
                "last_price": 10.5,
                "change_pct": 5.0,
                "time": "09:31:00",
                "is_stale": False,
            },
            "600000": {
                "code": "600000",
                "name": "浦发银行",
                "open_price": 7.7,
                "pre_close": 7.8,
                "last_price": 7.75,
                "change_pct": -0.64,
                "time": "09:31:00",
                "is_stale": False,
            },
            "300001": {
                "code": "300001",
                "name": "特锐德",
                "open_price": 20.0,
                "pre_close": 20.0,
                "last_price": 19.9,
                "change_pct": -0.5,
                "time": "09:31:00",
                "is_stale": False,
            },
        }
        return {"ok": True, "quotes": [rows[c] for c in codes if c in rows]}


def test_realtime_overlay_confirms_high_open_and_cancels_low_open(tmp_path):
    service = RealtimeOverlayService(
        FakeQuoteService(),
        output_dir=tmp_path,
        min_open_gap_pct=0.0,
        min_intraday_pct=0.0,
    )
    payload = service.build_overlay(
        "20260616",
        candidates=[
            {"code": "000001", "name": "平安银行", "score": 88, "rank": 1},
            {"code": "600000", "name": "浦发银行", "score": 70, "rank": 2},
            {"code": "300001", "name": "特锐德", "score": 60, "rank": 3},
        ],
        persist=True,
    )

    rows = {row["code"]: row for row in payload["rows"]}
    assert rows["000001"]["confirm_status"] == "confirmed"
    assert rows["600000"]["confirm_status"] == "cancelled"
    assert rows["300001"]["confirm_status"] == "observe"
    assert payload["counts"] == {"confirmed": 1, "cancelled": 1, "observe": 1}
    assert (tmp_path / "overlay_20260616.json").exists()
