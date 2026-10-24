"""Viewer/admin routing, read-only storage and bounded response projections."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.collection_plan import digest
from core.realtime.evidence_views import EvidenceViews
from core.realtime.research_store import ResearchStore
from core.realtime.trading_session import shanghai_now

DATE = "20261023"


def frozen(backend):
    body = dict(market_date=DATE, rows=[{"code": "000001"}], sectors=[], inputs_hash="hash")
    plan = {**body, "plan_id": digest(body)}
    backend.set_json(f"realtime:collection-plan:{DATE}", plan)
    return plan


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    import web.app as module
    from web import auth_store

    backend = MemoryStateBackend("evidence-web")
    plan = frozen(backend)
    role = ["viewer"]
    monkeypatch.setattr(module, "WEB_DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "_evidence_backend", lambda: backend)
    monkeypatch.setattr(
        module,
        "validate_session",
        lambda token: ({"id": 123, "role": role[0], "username": "test"}, None) if token else (None, ""),
    )
    monkeypatch.setattr(auth_store, "AUTH_DB_PATH", tmp_path / "auth.sqlite")
    auth_store.get_role_permission_overrides.cache_clear()
    module._RATE_BUCKETS.clear()
    client = TestClient(module.app)
    client.cookies.set(auth_store.SESSION_COOKIE_NAME, "test")
    yield client, backend, plan, role, tmp_path
    client.close()
    auth_store.get_role_permission_overrides.cache_clear()


def test_login_and_admin_boundaries(app_client):
    client, _, _, role, _ = app_client
    client.cookies.clear()
    assert client.get("/api/market-evidence/stock/000001", params={"market_date": DATE}).status_code == 401
    from web.auth_store import SESSION_COOKIE_NAME

    client.cookies.set(SESSION_COOKIE_NAME, "test")
    assert client.get("/api/admin/market-evidence/diagnostics", params={"market_date": DATE}).status_code == 403
    assert client.get("/api/admin/market-evidence/research/000001/money_flow").status_code == 403
    role[0] = "admin"
    assert client.get("/api/admin/market-evidence/diagnostics", params={"market_date": DATE}).status_code == 200


def test_viewer_summary_never_exposes_rows_or_transport_error(app_client):
    client, backend, plan, _, _ = app_client
    backend.set_json(
        f"realtime:evidence:v1:{DATE}:000001:trade_batch",
        dict(
            code="000001",
            market_date=DATE,
            collection_plan_id=plan["plan_id"],
            capture_mode="live",
            quality_status="partial",
            quality_reasons=["private transport detail"],
            rows=[{"raw": "payload"}],
            summary={},
            coverage={"anchor": ["private-cursor"], "complete": False},
            received_at=shanghai_now().isoformat(),
        ),
    )
    response = client.get("/api/market-evidence/stock/000001", params={"market_date": DATE})
    assert response.status_code == 200
    body = response.json()
    assert not body["strategy_affected"] and not body["formal_eligible"]
    text = response.text
    assert "private transport detail" not in text and "private-cursor" not in text and "payload" not in text


def test_scope_mismatch_does_not_show_other_plan_data(app_client):
    client, backend, _, _, _ = app_client
    backend.set_json(
        f"realtime:evidence:v1:{DATE}:000001:opening_match",
        dict(
            code="000001",
            market_date=DATE,
            collection_plan_id="other",
            quality_status="ok",
            capture_mode="live",
            rows=[],
        ),
    )
    response = client.get("/api/market-evidence/stock/000001", params={"market_date": DATE})
    assert all(row["status"] == "missing" for row in response.json()["live"])


@pytest.mark.parametrize(
    "url",
    [
        "/api/market-evidence/stock/000001?market_date=../../bad",
        "/api/market-evidence/stock/bad?market_date=20261023",
        "/api/market-evidence/boards?market_date=bad",
    ],
)
def test_invalid_view_inputs_are_rejected(app_client, url):
    assert app_client[0].get(url).status_code == 400


def test_raw_pagination_is_bounded(app_client):
    client, backend, plan, role, _ = app_client
    role[0] = "admin"
    backend.set_json(f"realtime:evidence:v1:{DATE}:000001:trade_batch", dict(rows=list(range(100))))
    response = client.get(
        "/api/admin/market-evidence/raw/000001/trade_batch", params={"market_date": DATE, "offset": 20, "limit": 20}
    )
    assert response.json()["rows"] == list(range(20, 40))
    assert (
        client.get(
            "/api/admin/market-evidence/raw/000001/trade_batch", params={"market_date": DATE, "limit": 201}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/admin/market-evidence/raw/000001/trade_batch", params={"market_date": DATE, "offset": -1}
        ).status_code
        == 422
    )


def test_page_has_admin_tools_only_for_admin(app_client):
    client, _, _, role, _ = app_client
    viewer = client.get("/market-evidence")
    assert viewer.status_code == 200 and "me-form" in viewer.text and "me-diagnostics-button" not in viewer.text
    role[0] = "admin"
    admin = client.get("/market-evidence")
    assert "me-diagnostics-button" in admin.text and "me-experiments-button" in admin.text


def test_empty_diagnostics_and_versions_do_not_initialize_market_db(tmp_path):
    views = EvidenceViews(tmp_path, MemoryStateBackend())
    assert views.stock(DATE, "000001")["research"] == []
    assert views.diagnostics(DATE)["pending"] is None
    assert views.experiments()["rows"] == []
    assert list(tmp_path.iterdir()) == []


def test_permissions_follow_existing_intraday_setting(app_client):
    from web.auth_store import save_role_permission_overrides
    from web.permissions import can_access_path

    save_role_permission_overrides(
        [dict(role="viewer", permission_key="intraday", can_access=False, menu_visible=False)]
    )
    assert not can_access_path({"role": "viewer"}, "/market-evidence")
    assert not can_access_path({"role": "viewer"}, "/api/market-evidence/stock/000001")
    assert not can_access_path({"role": "viewer"}, "/api/admin/market-evidence/diagnostics")


def test_fresh_standalone_depth_is_not_overwritten_by_stale_quote(app_client):
    from datetime import timedelta

    client, backend, plan, _, _ = app_client
    now = shanghai_now()
    book = dict(
        quality_status="ok",
        spread_yuan=0.01,
        buy_levels=[dict(level=1, price=10, volume_hand=10)],
        sell_levels=[dict(level=1, price=10.01, volume_hand=10)],
    )
    backend.set_json(
        f"realtime:evidence:v1:{DATE}:000001:depth",
        dict(
            code="000001",
            market_date=DATE,
            collection_plan_id=plan["plan_id"],
            capture_mode="live",
            quality_status="ok",
            summary={},
            received_at=now.isoformat(),
            rows=[book],
        ),
    )
    backend.set_json(
        "realtime:quotes:latest:000001",
        dict(date=DATE, received_at=(now - timedelta(seconds=60)).isoformat(), depth={**book, "spread_yuan": 1}),
    )
    response = client.get("/api/market-evidence/stock/000001", params={"market_date": DATE}).json()
    row = next(row for row in response["live"] if row["kind"] == "depth")
    assert row["summary"]["spread_yuan"] == 0.01 and not row["is_stale"]
    assert response["depth_levels"]["buy_levels"][0]["price"] == 10
