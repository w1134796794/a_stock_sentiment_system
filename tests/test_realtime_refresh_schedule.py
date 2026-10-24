from types import SimpleNamespace

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


def test_embedded_refresh_runs_leader_strength_and_candidate_overlay_when_market_is_open(monkeypatch):
    monkeypatch.setattr("config.settings.REALTIME_STRATEGY_EXTERNAL", False)
    monkeypatch.setattr(web_app, "_ensure_preopen_inputs", lambda: None)
    monkeypatch.setattr(web_app, "_get_position_monitor", lambda: SimpleNamespace(run_once=lambda **kw: None))
    monkeypatch.setattr(web_app, "_trading_ledger", lambda: SimpleNamespace(event=lambda *a: None))
    monkeypatch.setattr(web_app, "_realtime_market_date", lambda: "20260630")
    monkeypatch.setattr(web_app, "_realtime_candidate_date", lambda *a: "20260629")
    monkeypatch.setattr("core.operations.preopen_evidence.PreopenEvidenceStore.load",
                        lambda *a: {"inputs": {"leader_pools": {"10": {"rows": []}}, "candidates": []}})
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

    assert [key[0] for key in calls] == ["overlay", "intraday-strength", "overlay"]


def test_external_strategy_never_recalculates_signals_from_web_refresh(monkeypatch):
    monkeypatch.setattr("config.settings.REALTIME_STRATEGY_EXTERNAL", True)
    calls = []
    monkeypatch.setattr(web_app._REALTIME_PAYLOAD_CACHE, "refresh", lambda *args: calls.append(args))
    web_app._refresh_realtime_defaults()
    assert calls == []
