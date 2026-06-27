from __future__ import annotations

import pytest

from web import auth_store
from web.permissions import (
    can_access_path,
    permission_matrix,
    reset_permission_matrix,
    update_permission_matrix,
    visible_menu_groups,
)


@pytest.fixture()
def isolated_auth_db(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "AUTH_DB_PATH", tmp_path / "auth.sqlite")
    auth_store.get_role_permission_overrides.cache_clear()
    yield
    auth_store.get_role_permission_overrides.cache_clear()


def _visible_keys(user):
    return {
        item["key"]
        for group in visible_menu_groups(user)
        for item in group.get("items", [])
    }


def test_role_permission_matrix_controls_menu_and_route(isolated_auth_db):
    admin = {"role": "admin"}
    viewer = {"role": "viewer"}

    assert can_access_path(viewer, "/realtime") is True
    assert can_access_path(viewer, "/run") is False
    assert "realtime" in _visible_keys(viewer)
    assert "run" not in _visible_keys(viewer)

    update_permission_matrix(
        [
            {
                "role": "viewer",
                "permission_key": "realtime",
                "menu_visible": False,
                "can_access": False,
            }
        ]
    )
    assert can_access_path(viewer, "/realtime") is False
    assert can_access_path(viewer, "/api/realtime/market") is False
    assert "realtime" not in _visible_keys(viewer)

    update_permission_matrix(
        [
            {
                "role": "viewer",
                "permission_key": "realtime",
                "menu_visible": False,
                "can_access": True,
            }
        ]
    )
    assert can_access_path(viewer, "/realtime") is True
    assert "realtime" not in _visible_keys(viewer)
    assert can_access_path(admin, "/admin/permissions") is True


def test_forced_admin_permissions_cannot_be_relaxed(isolated_auth_db):
    viewer = {"role": "viewer"}
    auth_store.save_role_permission_overrides(
        [
            {
                "role": "viewer",
                "permission_key": "run",
                "menu_visible": True,
                "can_access": True,
            }
        ]
    )

    assert can_access_path(viewer, "/run") is False
    assert "run" not in _visible_keys(viewer)
    with pytest.raises(ValueError, match="固定管理员权限"):
        update_permission_matrix(
            [
                {
                    "role": "viewer",
                    "permission_key": "run",
                    "menu_visible": True,
                    "can_access": True,
                }
            ]
        )


def test_reset_restores_defaults_and_matrix_marks_locked_items(isolated_auth_db):
    update_permission_matrix(
        [
            {
                "role": "viewer",
                "permission_key": "about",
                "menu_visible": False,
                "can_access": False,
            }
        ]
    )
    assert can_access_path({"role": "viewer"}, "/about") is False

    data = reset_permission_matrix()
    assert can_access_path({"role": "viewer"}, "/about") is True
    users = next(
        item
        for group in data["groups"]
        for item in group["items"]
        if item["key"] == "users"
    )
    assert users["permissions"]["admin"] == {
        "menu_visible": True,
        "can_access": True,
        "locked": True,
    }
    assert users["permissions"]["viewer"] == {
        "menu_visible": False,
        "can_access": False,
        "locked": True,
    }
    assert permission_matrix()["roles"] == ["admin", "viewer"]
