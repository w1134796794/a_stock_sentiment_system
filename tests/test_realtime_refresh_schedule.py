import web.app as web_app


def test_default_refresh_skips_all_jobs_when_market_is_closed(monkeypatch):
    calls = []
    monkeypatch.setattr(web_app, "_data_generation_running", lambda: False)
    monkeypatch.setattr(
        web_app,
        "_current_realtime_session",
        lambda: {"is_open": False},
    )
    monkeypatch.setattr(
        web_app._REALTIME_PAYLOAD_CACHE,
        "refresh",
        lambda key, loader: calls.append(key),
    )

    web_app._refresh_realtime_defaults()

    assert calls == []


def test_default_refresh_runs_jobs_when_market_is_open(monkeypatch):
    calls = []
    monkeypatch.setattr(web_app, "_data_generation_running", lambda: False)
    monkeypatch.setattr(
        web_app,
        "_current_realtime_session",
        lambda: {"is_open": True},
    )
    monkeypatch.setattr(web_app, "_latest_date", lambda: "20260629")
    monkeypatch.setattr(
        web_app._REALTIME_PAYLOAD_CACHE,
        "refresh",
        lambda key, loader: calls.append(key),
    )

    web_app._refresh_realtime_defaults()

    assert [key[0] for key in calls] == ["overlay", "sectors", "health"]
