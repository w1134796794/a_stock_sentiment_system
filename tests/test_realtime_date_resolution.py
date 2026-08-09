import web.app as web_app


def test_selected_candidate_uses_next_available_trade_date(monkeypatch):
    monkeypatch.setattr(
        web_app,
        "_list_dates",
        lambda: ["20260805", "20260806", "20260807"],
    )

    assert web_app._realtime_market_date_for_candidate("20260805") == "20260806"
    assert web_app._realtime_market_date_for_candidate("20260806") == "20260807"
