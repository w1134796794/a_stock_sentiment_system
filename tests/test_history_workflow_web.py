import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import web.app as module
    from web import auth_store

    monkeypatch.setattr(module, "WEB_DATA_DIR", tmp_path)
    role = ["viewer"]
    monkeypatch.setattr(
        module,
        "validate_session",
        lambda token: ({"id": 42, "role": role[0], "username": "test"}, None) if token else (None, ""),
    )
    monkeypatch.setattr(auth_store, "AUTH_DB_PATH", tmp_path / "auth.sqlite")
    auth_store.get_role_permission_overrides.cache_clear()
    module._RATE_BUCKETS.clear()
    session = TestClient(module.app, raise_server_exceptions=False)
    session.cookies.set(auth_store.SESSION_COOKIE_NAME, "test")
    yield session, role
    session.close()
    auth_store.get_role_permission_overrides.cache_clear()


def test_admin_boundary_and_read_only_listing(client):
    session, role = client
    path = "/api/admin/history-workflows"
    assert session.get("/market-backfill").status_code == 403
    assert session.get("/market-collection").status_code == 403
    assert session.get("/api/admin/market-collection/defaults").status_code == 403
    assert session.get(path).status_code == 403
    assert session.post(path, json={"start": "20261022", "end": "20261023", "capital": 100000}).status_code == 403
    role[0] = "admin"
    assert session.get(path).json()["data"] == []


def test_collection_pages_do_not_start_work(client, monkeypatch):
    session, role = client
    role[0] = "admin"
    monkeypatch.setattr("core.operations.collection_center.subprocess.Popen",
        lambda *a, **kw: pytest.fail("Page rendering must not start collection"))
    for url in ("/market-collection", "/market-backfill"):
        response = session.get(url)
        assert response.status_code == 200
        assert 'data-tab="postclose"' in response.text
        assert 'data-tab="preopen"' in response.text
        assert 'data-tab="intraday"' in response.text
        assert 'data-tab="backfill"' in response.text
        assert 'data-hw-phase="run"' not in response.text
    response = session.get("/fetch", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/market-collection?tab=postclose"
    assert session.get("/api/admin/market-collection/status?phase=bad&date=20261023").status_code == 409


def test_page_controls_and_invalid_stage_do_not_start_market_work(client, monkeypatch):
    session, role = client
    role[0] = "admin"

    def forbidden(*a, **kw):
        pytest.fail("Reading a page or invalid request must not start a child process")

    monkeypatch.setattr("core.portfolio.history_workflow.subprocess.Popen", forbidden)
    response = session.get("/portfolio/backtest")
    assert response.status_code == 200
    assert 'data-hw-phase="freeze"' in response.text and "history-workflow.js" in response.text
    assert 'href="/market-backfill"' in response.text
    page = session.get("/market-backfill")
    assert page.status_code == 200
    assert 'data-backfill="true"' in page.text
    assert 'data-hw-phase="prefetch"' in page.text
    assert 'data-hw-phase="run"' not in page.text and 'data-hw-phase="reconcile"' not in page.text
    assert 'id="portfolio-account"' not in page.text
    created = session.post(
        "/api/admin/history-workflows", json={"start": "20261022", "end": "20261023", "capital": 100000}
    )
    assert created.status_code == 200, created.text
    ident = created.json()["data"]["job_id"]
    assert session.post(f"/api/admin/history-workflows/{ident}/run", json={}).status_code == 409
    assert session.get(f"/api/admin/history-workflows/{ident}/report/result").status_code == 404
    assert session.get(f"/api/admin/history-workflows/{ident}/report/status").status_code == 409


def test_stale_result_report_is_not_presented_as_current(client, tmp_path):
    import json

    session, role = client
    role[0] = "admin"
    response = session.post(
        "/api/admin/history-workflows", json={"start": "20261022", "end": "20261023", "capital": 100000}
    )
    job = response.json()["data"]
    job.update(stages={"run": "complete"}, frozen_version="new")
    directory = tmp_path / "history_workflows" / job["job_id"]
    (directory / "status.json").write_text(json.dumps(job), encoding="utf-8")
    (directory / "result.json").write_text(json.dumps({"frozen_version": "old"}), encoding="utf-8")
    path = f"/api/admin/history-workflows/{job['job_id']}/report/result"
    assert session.get(path).status_code == 409
    (directory / "result.json").write_text(json.dumps({"frozen_version": "new", "trades": []}), encoding="utf-8")
    assert session.get(path).status_code == 200
