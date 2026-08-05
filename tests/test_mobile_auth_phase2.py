"""Phase 2 WeChat binding, token rotation, and session controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from web import auth_store
from web.mobile_auth_service import MobileAuthService
from web.mobile_auth_store import (
    MobileAuthError,
    refresh_tokens,
    validate_access_token,
)
from web.wechat_client import WeChatIdentity


class FakeWeChatClient:
    def __init__(self, openid: str = "openid-test") -> None:
        self.openid = openid

    def exchange(self, js_code: str) -> WeChatIdentity:
        assert js_code
        return WeChatIdentity(
            appid="wx-test-app",
            openid=self.openid,
            unionid="union-test",
        )


@pytest.fixture
def auth_db(tmp_path: Path, monkeypatch) -> Path:
    database = tmp_path / "auth.sqlite"
    monkeypatch.setattr(auth_store, "AUTH_DB_PATH", database)
    monkeypatch.setenv("APP_ADMIN_USER", "phase2-admin")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "phase2-admin-password")
    monkeypatch.setenv("APP_SESSION_POLICY", "kick_oldest")
    monkeypatch.setenv("MOBILE_ACCESS_TOKEN_MINUTES", "15")
    monkeypatch.setenv("MOBILE_REFRESH_TOKEN_DAYS", "30")
    auth_store.ensure_auth_db()
    return database


def _create_viewer(username: str = "viewer", max_sessions: int = 1):
    return auth_store.create_user(
        username=username,
        password="viewer-password",
        display_name="测试用户",
        role="viewer",
        days=30,
        max_sessions=max_sessions,
    )


def _bind(
    service: MobileAuthService,
    *,
    device_id: str = "device-a",
    username: str = "viewer",
):
    first = service.wechat_login(
        js_code="temporary-code",
        device_id=device_id,
        device_name="测试手机",
        platform="wechat_miniprogram",
        ip="127.0.0.1",
        user_agent="pytest",
    )
    assert first["status"] == "binding_required"
    return service.bind(
        binding_ticket=first["binding_ticket"],
        username=username,
        password="viewer-password",
        device_id=device_id,
        device_name="测试手机",
        platform="wechat_miniprogram",
        ip="127.0.0.1",
        user_agent="pytest",
    )


def test_wechat_login_bind_access_and_refresh_rotation(auth_db):
    _create_viewer()
    service = MobileAuthService(FakeWeChatClient())

    authenticated = _bind(service)
    assert authenticated["status"] == "authenticated"
    assert authenticated["user"]["username"] == "viewer"
    assert authenticated["access_token"]
    assert authenticated["refresh_token"]

    user, error, context = validate_access_token(authenticated["access_token"])
    assert error is None
    assert user["username"] == "viewer"
    assert context["mobile_device_session_id"]

    refreshed_user, refreshed = refresh_tokens(
        refresh_token=authenticated["refresh_token"],
        ip="127.0.0.2",
        user_agent="pytest-refresh",
    )
    assert refreshed_user["username"] == "viewer"
    assert refreshed["access_token"] != authenticated["access_token"]
    assert refreshed["refresh_token"] != authenticated["refresh_token"]
    assert validate_access_token(authenticated["access_token"])[1] == "not_authenticated"
    assert validate_access_token(refreshed["access_token"])[1] is None
    with pytest.raises(MobileAuthError, match="刷新令牌无效"):
        refresh_tokens(
            refresh_token=authenticated["refresh_token"],
            ip="127.0.0.1",
            user_agent="pytest",
        )


def test_new_device_kicks_old_mobile_session(auth_db):
    _create_viewer(max_sessions=1)
    first_service = MobileAuthService(FakeWeChatClient())
    first = _bind(first_service, device_id="device-a")

    second = first_service.wechat_login(
        js_code="new-code",
        device_id="device-b",
        device_name="第二台手机",
        platform="wechat_miniprogram",
        ip="127.0.0.2",
        user_agent="pytest-second-device",
    )
    assert second["status"] == "authenticated"
    assert validate_access_token(first["access_token"])[1] == "mobile_session_revoked"
    assert validate_access_token(second["access_token"])[1] is None


def test_web_and_mobile_share_same_session_limit(auth_db):
    _create_viewer(max_sessions=1)
    service = MobileAuthService(FakeWeChatClient())
    mobile = _bind(service)

    ok, _, web_token, _, _ = auth_store.login(
        username="viewer",
        password="viewer-password",
        ip="127.0.0.3",
        user_agent="pytest-web",
    )
    assert ok is True
    assert auth_store.validate_session(web_token)[1] is None
    assert validate_access_token(mobile["access_token"])[1] == "mobile_session_revoked"


def test_admin_disable_and_kick_revoke_mobile_tokens(auth_db):
    viewer = _create_viewer()
    service = MobileAuthService(FakeWeChatClient())
    authenticated = _bind(service)

    auth_store.revoke_user_sessions(int(viewer["id"]))
    assert validate_access_token(authenticated["access_token"])[1] == "mobile_session_revoked"

    relogin = service.wechat_login(
        js_code="another-code",
        device_id="device-a",
        device_name="测试手机",
        platform="wechat_miniprogram",
        ip="127.0.0.1",
        user_agent="pytest",
    )
    assert relogin["status"] == "authenticated"
    auth_store.update_user_status(int(viewer["id"]), "disabled")
    assert validate_access_token(relogin["access_token"])[1] == "mobile_session_revoked"


def test_expired_account_cannot_bind(auth_db):
    auth_store.create_user(
        username="expired-viewer",
        password="viewer-password",
        role="viewer",
        expire_at="2020-01-01",
    )
    service = MobileAuthService(FakeWeChatClient("expired-openid"))
    first = service.wechat_login(
        js_code="expired-code",
        device_id="expired-device",
        device_name="旧设备",
        platform="wechat_miniprogram",
        ip="127.0.0.1",
        user_agent="pytest",
    )
    with pytest.raises(MobileAuthError) as error:
        service.bind(
            binding_ticket=first["binding_ticket"],
            username="expired-viewer",
            password="viewer-password",
            device_id="expired-device",
            device_name="旧设备",
            platform="wechat_miniprogram",
            ip="127.0.0.1",
            user_agent="pytest",
        )
    assert error.value.code == "SUBSCRIPTION_EXPIRED"
