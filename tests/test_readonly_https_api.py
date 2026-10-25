"""Transport/auth isolation, bounded reads and storage-only integration behavior."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.readonly_service import ReadonlyService
from core.infrastructure.shared_state import MemoryStateBackend
from scripts.configure_readonly_api import configure
from web.api.readonly.router import create_readonly_router
from web.readonly_access import PREFIX, ReadonlyAccess, is_readonly_path

TOKEN = "fixture-readonly-token-which-is-at-least-32-characters"
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone(timedelta(hours=8)))


class ReadOnlyBackend(MemoryStateBackend):
    is_shared = True
    locked = False

    def set_json(self, *args, **kwargs):
        if self.locked:
            raise AssertionError("API must not write shared business state")
        return super().set_json(*args, **kwargs)

    def acquire_lock(self, *args, **kwargs):
        raise AssertionError("API must not acquire producer leases")


@pytest.fixture
def setup_api(tmp_path):
    config = tmp_path / "settings" / "readonly_api.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            }
        )
    )
    backend = ReadOnlyBackend()
    service = ReadonlyService(
        web_data_dir=tmp_path / "webdata",
        cache_dir=tmp_path / "cache",
        base_dir=tmp_path / "base",
        backend=backend,
        clock=lambda: NOW,
    )
    access = ReadonlyAccess(config)
    app = FastAPI()
    app.middleware("http")(access)
    app.include_router(create_readonly_router(lambda: service))
    client = TestClient(app, base_url="https://testserver")
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, service, access, config, backend


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_token_required_and_cookie_does_not_authenticate(setup_api):
    client, *_ = setup_api
    client.headers.pop("Authorization")
    client.cookies.set("a_stock_sentiment_session", "browser-session")
    result = client.get(PREFIX + "/health")
    assert result.status_code == 401 and result.headers["cache-control"] == "no-store"
    assert client.get(PREFIX + "/health?token=" + TOKEN).status_code == 401
    client.headers["Authorization"] = "Bearer wrong-token-which-is-long-enough-but-invalid"
    assert client.get(PREFIX + "/health").status_code == 401


def test_https_required_and_spoofed_forwarding_header_is_not_trusted(setup_api):
    client, *_ = setup_api
    result = client.get("http://testserver" + PREFIX + "/health", headers={"X-Forwarded-Proto": "https"})
    assert result.status_code == 426 and result.json()["error"] == "https_required"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "head", "options"])
def test_non_get_requests_blocked(setup_api, method):
    client, *_ = setup_api
    result = getattr(client, method)(PREFIX + "/health")
    assert result.status_code == 405 and result.headers["allow"] == "GET"


@pytest.mark.parametrize(
    "config_value",
    [
        None,
        {},
        {"enabled": False},
        {"enabled": True, "token_sha256": "x" * 64, "expires_at": "2020-01-01T00:00:00+00:00"},
        [],
        "bad",
    ],
)
def test_missing_invalid_or_expired_config_disables_access(setup_api, config_value):
    client, _, _, path, _ = setup_api
    if config_value is None:
        path.unlink()
    else:
        path.write_text(json.dumps(config_value))
    assert client.get(PREFIX + "/health").status_code == 503


def test_rate_limit_is_bounded(setup_api):
    client, _, access, *_ = setup_api
    access.limit = 2
    assert client.get(PREFIX + "/health").status_code == 200
    assert client.get(PREFIX + "/health").status_code == 200
    result = client.get(PREFIX + "/health")
    assert result.status_code == 429 and result.headers["retry-after"] == "60"


def test_quotes_expose_source_time_and_staleness_without_writes(setup_api):
    client, _, _, _, backend = setup_api
    backend.set_json(
        "realtime:quotes:latest:000001",
        {
            "code": "000001",
            "date": "20261026",
            "time": "09:59:59",
            "last_price": 10,
            "received_at": "2026-10-26T09:59:59+08:00",
            "source": "eltdx",
            "private_token": "SECRET",
        },
    )
    backend.locked = True
    payload = client.get(PREFIX + "/quotes?codes=000001,600000").json()
    assert payload["read_only"] is True
    assert payload["data"]["missing_codes"] == ["600000"]
    row = payload["data"]["rows"][0]
    assert row["received_at"] == "2026-10-26T09:59:59+08:00"
    assert "SECRET" not in json.dumps(payload)
    assert row["data_status"] == "fresh"


def test_empty_cache_is_explicitly_missing(setup_api):
    client, service, *_ = setup_api
    result = client.get(PREFIX + "/quotes?codes=000001").json()
    assert result["data"]["rows"] == [] and result["data"]["missing_codes"] == ["000001"]
    assert not service.root.exists() and not service.cache.exists()


@pytest.mark.parametrize("query", ["codes=../xxx", "codes=" + ",".join(["000001"] * 21)])
def test_quote_inputs_reject_paths_and_unbounded_lists(setup_api, query):
    client, *_ = setup_api
    assert client.get(PREFIX + "/quotes?" + query).status_code == 400


def test_candidate_pagination_whitelist_and_business_files_unchanged(setup_api):
    client, service, *_ = setup_api
    path = service.root / "screening" / "decision_pool" / "decision_pool_20261023.json"
    write_json(
        path,
        {
            "rows": [
                {"code": "000001", "name": "测试", "排除理由": ["条件未满足"], "api_token": "SECRET"},
                {"code": "600000", "score": 80},
            ],
            "generated_at": "2026-10-23T18:00:00+08:00",
        },
    )
    original = path.read_bytes()
    result = client.get(PREFIX + "/candidates?date=20261023&offset=1&limit=1").json()["data"]
    assert result["total"] == 2 and result["rows"] == [{"code": "600000", "score": 80}]
    result = client.get(PREFIX + "/candidates?date=20261023&code=000001").json()["data"]
    assert result["rows"][0]["排除理由"] == ["条件未满足"] and "api_token" not in result["rows"][0]
    assert path.read_bytes() == original
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


@pytest.mark.parametrize(
    "url",
    [
        "/candidates?date=20260230",
        "/candidates?date=20261023&limit=101",
        "/candidates?date=20261023&offset=-1",
        "/minutes/nope?date=20261023",
    ],
)
def test_invalid_dates_limits_and_stock_codes(setup_api, url):
    client, *_ = setup_api
    assert client.get(PREFIX + url).status_code in {400, 422}


def test_old_signals_never_remain_executable_and_cache_is_unchanged(setup_api):
    client, _, _, _, backend = setup_api
    value = {
        "generated_at": "2026-10-26T09:30:00+08:00",
        "rows": [
            {
                "code": "000001",
                "confirm_status": "confirmed",
                "received_at": "2026-10-26T09:30:00+08:00",
                "reason": "已确认",
            }
        ],
    }
    backend.set_json("realtime:strategy:result:20261026", value)
    backend.locked = True
    result = client.get(PREFIX + "/signals?date=20261026").json()["data"]
    assert result["data_status"] == "stale" and result["rows"][0]["confirm_status"] == "observe"
    assert backend.get_json("realtime:strategy:result:20261026") == value


def test_minute_reads_plain_and_gzip_and_does_not_fetch_missing(setup_api):
    client, service, *_ = setup_api
    folder = service.cache / "stock" / "tick"
    folder.mkdir(parents=True)
    path = folder / "000001.SZ_20261023.csv"
    raw = b"time,close,amount,hidden\n09:31:00,10.0,12345,SECRET\n"
    path.write_bytes(raw)
    first = client.get(PREFIX + "/minutes/000001?date=20261023").json()["data"]
    path.unlink()
    with gzip.open(path.with_suffix(".csv.gz"), "wb") as stream:
        stream.write(raw)
    second = client.get(PREFIX + "/minutes/000001?date=20261023").json()["data"]
    assert first["rows"] == second["rows"] == [{"time": "09:31:00", "close": "10.0", "amount": "12345"}]
    assert client.get(PREFIX + "/minutes/600000?date=20261023").json()["data"]["data_status"] == "missing"
    assert list(folder.glob("*.lock")) == []


def test_minute_endpoint_reads_shared_bars_without_touching_disk(setup_api):
    client, service, _, _, backend = setup_api
    backend.set_json("realtime:minutes:20261026:000001", [{"time": "09:31", "close": 10}])
    backend.locked = True
    result = client.get(PREFIX + "/minutes/000001?date=20261026").json()["data"]
    assert result["source"] == "shared_cache" and not service.cache.exists()


def test_symlink_cannot_expose_other_files(setup_api, tmp_path):
    client, service, *_ = setup_api
    path = service.root / "screening" / "decision_pool" / "decision_pool_20261023.json"
    path.parent.mkdir(parents=True)
    external = tmp_path / "private.json"
    external.write_text('{"rows":[{"code":"SECRET"}]}')
    path.symlink_to(external)
    result = client.get(PREFIX + "/candidates?date=20261023")
    assert result.status_code == 503 and "SECRET" not in result.text


def test_oversized_and_corrupt_data_does_not_leak_errors(setup_api, monkeypatch):
    import core.application.readonly_service as module

    client, service, *_ = setup_api
    path = service.root / "screening" / "decision_pool" / "decision_pool_20261023.json"
    write_json(path, {"rows": [], "value": "x" * 100})
    monkeypatch.setattr(module, "MAX_JSON", 16)
    assert client.get(PREFIX + "/candidates?date=20261023").status_code == 503
    path.write_text("invalid-private-diagnostic")
    result = client.get(PREFIX + "/candidates?date=20261023")
    assert "private-diagnostic" not in result.text and result.status_code == 503


def test_log_summary_never_returns_raw_log_or_secrets(setup_api):
    client, service, *_ = setup_api
    path = service.root / "logs" / "preopen_collection_20261026.log"
    path.parent.mkdir(parents=True)
    path.write_text("ERROR Authorization: Bearer SECRET\nWARNING source unavailable\n")
    result = client.get(PREFIX + "/logs/summary?date=20261026&kind=preopen")
    assert result.json()["data"]["error_lines"] == 1
    assert "SECRET" not in result.text and "Authorization" not in result.text


def test_prefix_matching_does_not_expand_bearer_scope():
    assert is_readonly_path(PREFIX + "/health") and is_readonly_path(PREFIX)
    assert not is_readonly_path(PREFIX + "-other/health")
    assert not is_readonly_path("/api/config")


def test_cli_enable_rotate_and_disable_without_printing_token(tmp_path, capsys):
    config = tmp_path / "settings" / "readonly_api.json"
    token_file = tmp_path / "token.txt"
    result = configure(config, token_file=token_file)
    token = token_file.read_text().strip()
    assert token not in json.dumps(result) and token not in capsys.readouterr().out
    assert token not in config.read_text()
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o600
    access = ReadonlyAccess(config)
    assert access.enabled_hash() == hashlib.sha256(token.encode()).hexdigest()
    with pytest.raises(ValueError):
        configure(config, token_file=tmp_path / "another.txt")
    configure(config, token_file=tmp_path / "new.txt", rotate=True)
    assert access.enabled_hash() != hashlib.sha256(token.encode()).hexdigest()
    configure(config, disable=True)
    assert access.enabled_hash() is None


def test_existing_token_output_is_not_overwritten(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text("original")
    config = tmp_path / "settings" / "readonly_api.json"
    with pytest.raises(FileExistsError):
        configure(config, token_file=path)
    assert path.read_text() == "original" and not config.exists()


def test_app_integration_bypasses_browser_auth_db_but_not_other_routes(tmp_path, monkeypatch):
    import web.app as module

    config = tmp_path / "readonly_api.json"
    token_file = tmp_path / "token.txt"
    configure(config, token_file=token_file)
    token = token_file.read_text().strip()
    monkeypatch.setattr(module, "_READONLY_ACCESS", ReadonlyAccess(config))
    monkeypatch.setattr(module, "ensure_auth_db", lambda: (_ for _ in ()).throw(AssertionError("browser auth reached")))
    client = TestClient(module.app, base_url="https://testserver")
    assert client.get(PREFIX + "/health").status_code == 401
    assert client.post(PREFIX + "/health", headers={"Authorization": f"Bearer {token}"}).status_code == 405
    import core.infrastructure.shared_state as shared_state

    backend = ReadOnlyBackend()
    backend.locked = True
    monkeypatch.setattr(shared_state, "get_shared_state_backend", lambda: backend)
    result = client.get(PREFIX + "/health", headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 200 and result.json()["read_only"] is True
    # The ordinary browser route still goes through browser authentication.
    with pytest.raises(Exception, match="browser auth reached"):
        client.get("/api/config", headers={"Authorization": f"Bearer {token}"})


def test_bearer_cannot_authenticate_browser_or_mobile_routes(tmp_path, monkeypatch):
    import web.app as module

    config = tmp_path / "readonly.json"
    token_file = tmp_path / "token.txt"
    configure(config, token_file=token_file)
    monkeypatch.setattr(module, "_READONLY_ACCESS", ReadonlyAccess(config))
    monkeypatch.setattr(module, "ensure_auth_db", lambda: None)
    monkeypatch.setattr(module, "validate_session", lambda *_: (None, None))
    client = TestClient(module.app, base_url="https://testserver")
    headers = {"Authorization": "Bearer " + token_file.read_text().strip()}
    assert client.get("/api/config", headers=headers).status_code == 401


def test_stale_source_time_not_hidden_by_fresh_receipt(setup_api):
    client, _, _, _, backend = setup_api
    backend.set_json(
        "realtime:quotes:latest:000001",
        {
            "code": "000001",
            "date": "20261026",
            "time": "09:30:00",
            "last_price": 10,
            "received_at": "2026-10-26T09:59:59+08:00",
        },
    )
    row = client.get(PREFIX + "/quotes?codes=000001").json()["data"]["rows"][0]
    assert row["data_status"] == "stale"


def test_gzip_expansion_is_bounded(setup_api, monkeypatch):
    import core.application.readonly_service as module

    client, service, *_ = setup_api
    folder = service.cache / "stock" / "tick"
    folder.mkdir(parents=True)
    with gzip.open(folder / "000001.SZ_20261023.csv.gz", "wb") as stream:
        stream.write(b"time,close\n" + b"09:31,10\n" * 100)
    monkeypatch.setattr(module, "MAX_MINUTES", 64)
    assert client.get(PREFIX + "/minutes/000001?date=20261023").status_code == 503


def test_client_does_not_send_token_over_http_or_follow_redirects():
    from types import SimpleNamespace

    from scripts.query_readonly_api import QueryError, query

    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=302)

    session = SimpleNamespace(get=get)
    with pytest.raises(QueryError, match="HTTPS origin"):
        query("http://astockreview.cn", "health", token=TOKEN, session=session)
    assert calls == []
    with pytest.raises(QueryError, match="302"):
        query("https://astockreview.cn", "health", token=TOKEN, session=session)
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["headers"]["Authorization"] == "Bearer " + TOKEN


def test_concurrent_reads_are_limited_without_blocking_the_event_loop(setup_api):
    import asyncio

    import httpx

    _, _, access, *_ = setup_api
    app = FastAPI()
    app.middleware("http")(access)

    async def exercise():
        entered = 0
        ready = asyncio.Event()
        release = asyncio.Event()

        @app.get("/slow")
        async def slow():
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await release.wait()
            return {"ok": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as client:
            tasks = [asyncio.create_task(client.get("/slow")) for _ in range(2)]
            try:
                await asyncio.wait_for(ready.wait(), 5)
                result = await client.get("/slow")
                assert result.status_code == 429 and result.json()["error"] == "readonly_api_busy"
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
            assert all(result.status_code == 200 for result in results)
            assert (await client.get("/slow")).status_code == 200

    asyncio.run(exercise())


def test_invalid_quote_price_is_not_reported_fresh(setup_api):
    client, _, _, _, backend = setup_api
    backend.set_json(
        "realtime:quotes:latest:000001",
        {
            "code": "000001",
            "date": "20261026",
            "time": "09:59:59",
            "last_price": float("nan"),
            "received_at": "2026-10-26T09:59:59+08:00",
        },
    )
    row = client.get(PREFIX + "/quotes?codes=000001").json()["data"]["rows"][0]
    assert row["data_status"] == "invalid" and row["last_price"] is None


def test_signal_analysis_includes_stored_condition_checks_but_honors_stale_flag(setup_api):
    client, _, _, _, backend = setup_api
    value = {
        "generated_at": "2026-10-26T09:59:59+08:00",
        "execution_health": {"ready": False, "reasons": ["板块证据不足"], "private_token": "SECRET"},
        "rows": [
            {
                "code": "000001",
                "confirm_status": "confirmed",
                "received_at": "2026-10-26T09:59:59+08:00",
                "is_stale": True,
                "condition_checks": {"sector_confirmed": False},
                "confirmation_reason": "板块证据不足",
                "reason": "原始判断",
            }
        ],
    }
    backend.set_json("realtime:strategy:result:20261026", value)
    result = client.get(PREFIX + "/signals?date=20261026").json()["data"]
    row = result["rows"][0]
    assert row["confirm_status"] == "observe"
    assert row["stored_reason"] == "原始判断" and "过期" in row["reason"]
    assert row["condition_checks"] == {"sector_confirmed": False}
    assert row["confirmation_reason"] == "板块证据不足"
    assert result["execution_health"] == {"ready": False, "reasons": ["板块证据不足"]}
