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


class FakeEntrySignalService:
    def evaluate(self, rows, quotes, *, market_date):
        return {
            "000001": {
                "signal_status": "confirmed", "entry_mode": "continuation_only",
                "entry_mode_text": "强势延续", "reason": "分钟强势延续确认",
            },
            "600000": {
                "signal_status": "observe", "entry_mode": "weak_only",
                "entry_mode_text": "弱转强", "reason": "低开后等待收复昨收",
            },
            "300001": {
                "signal_status": "observe", "entry_mode": "weak_only",
                "entry_mode_text": "弱转强", "reason": "等待分钟确认",
            },
        }


def test_realtime_overlay_uses_shared_minute_entry_signals(tmp_path):
    service = RealtimeOverlayService(
        FakeQuoteService(),
        output_dir=tmp_path,
        entry_signal_service=FakeEntrySignalService(),
    )
    payload = service.build_overlay(
        "20260616", market_date="20260617",
        candidates=[
            {
                "code": "000001", "name": "平安银行", "score": 88, "rank": 1,
                "resonance_sectors": "银行,跨境支付",
            },
            {"code": "600000", "name": "浦发银行", "score": 70, "rank": 2},
            {"code": "300001", "name": "特锐德", "score": 60, "rank": 3},
        ],
        persist=True,
    )

    rows = {row["code"]: row for row in payload["rows"]}
    assert rows["000001"]["confirm_status"] == "confirmed"
    assert rows["000001"]["resonance_sectors"] == "银行,跨境支付"
    assert rows["600000"]["confirm_status"] == "observe"
    assert rows["600000"]["entry_mode_text"] == "弱转强"
    assert rows["300001"]["confirm_status"] == "observe"
    assert payload["candidate_date"] == "20260616"
    assert payload["market_date"] == "20260617"
    assert payload["counts"] == {"confirmed": 1, "cancelled": 0, "observe": 2, "unfilled": 0}
    assert (tmp_path / "overlay_20260616.json").exists()


def test_realtime_overlay_does_not_fallback_to_snapshot_plans(tmp_path):
    class FakeSnapshotReader:
        def latest(self):
            return "20260616"

        def load(self, trade_date):
            return {"trade_plans": {"rows": [{"股票代码": "000001", "股票名称": "旧计划"}]}}

    screening_dir = tmp_path / "screening"
    screening_dir.mkdir()
    service = RealtimeOverlayService(
        FakeQuoteService(), screening_dir=screening_dir,
        snapshot_reader=FakeSnapshotReader(), output_dir=tmp_path,
    )

    payload = service.build_overlay("20260616")

    assert payload["rows"] == []
    assert payload["source"] == "候选日指标筛选未生成"
