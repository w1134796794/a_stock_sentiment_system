"""Persistent WeChat bindings and opaque mobile access tokens."""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

from web import auth_store


class MobileAuthError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _format_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _user_expiry(user: Dict[str, Any]) -> datetime | None:
    if user.get("role") == "admin":
        return None
    return auth_store._parse_dt(user.get("expire_at"))


def _validate_user(user: Dict[str, Any] | None) -> Dict[str, Any]:
    if not user:
        raise MobileAuthError("USER_NOT_FOUND", "账号不存在", 404)
    if user.get("status") != "active":
        raise MobileAuthError("USER_DISABLED", "账号已被禁用，请联系管理员", 403)
    if user.get("is_expired") and user.get("role") != "admin":
        raise MobileAuthError("SUBSCRIPTION_EXPIRED", "服务已到期，请联系管理员续费", 403)
    return user


def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    success: bool,
    reason: str = "",
    user_id: int | None = None,
    device_session_id: int | None = None,
    path: str = "",
    ip: str = "",
    user_agent: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO mobile_audit_logs(
          user_id, device_session_id, action, path, ip, user_agent,
          success, reason, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            device_session_id,
            action,
            path,
            ip,
            user_agent[:500],
            1 if success else 0,
            reason,
            auth_store._now(),
        ),
    )


def _clean_expired(conn: sqlite3.Connection) -> None:
    now = auth_store._now()
    conn.execute(
        """
        UPDATE mobile_tokens
        SET revoked_at=COALESCE(revoked_at, ?)
        WHERE revoked_at IS NULL AND refresh_expires_at <= ?
        """,
        (now, now),
    )


def _active_session_rows(
    conn: sqlite3.Connection,
    user_id: int,
) -> list[tuple[str, int, str]]:
    now = auth_store._now()
    web_rows = conn.execute(
        """
        SELECT id, COALESCE(last_seen_at, created_at) AS touched_at
        FROM sessions
        WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?
        """,
        (user_id, now),
    ).fetchall()
    mobile_rows = conn.execute(
        """
        SELECT id, COALESCE(last_seen_at, created_at) AS touched_at
        FROM mobile_tokens
        WHERE user_id=? AND revoked_at IS NULL AND refresh_expires_at > ?
        """,
        (user_id, now),
    ).fetchall()
    rows = [("web", int(row["id"]), str(row["touched_at"])) for row in web_rows]
    rows.extend(("mobile", int(row["id"]), str(row["touched_at"])) for row in mobile_rows)
    return sorted(rows, key=lambda item: item[2])


def _enforce_session_limit(
    conn: sqlite3.Connection,
    user: Dict[str, Any],
) -> None:
    rows = _active_session_rows(conn, int(user["id"]))
    maximum = max(1, int(user.get("max_sessions") or 1))
    if len(rows) < maximum:
        return
    policy = os.getenv("APP_SESSION_POLICY", "kick_oldest").strip().lower()
    if policy == "reject_new":
        raise MobileAuthError(
            "SESSION_LIMIT_REACHED",
            "在线设备数已达上限，请先退出其他设备",
            409,
        )
    now = auth_store._now()
    for source, row_id, _ in rows[: len(rows) - maximum + 1]:
        table = "sessions" if source == "web" else "mobile_tokens"
        conn.execute(f"UPDATE {table} SET revoked_at=? WHERE id=?", (now, row_id))


def find_binding(appid: str, openid: str) -> Dict[str, Any] | None:
    auth_store.ensure_auth_db()
    with auth_store._connect() as conn:
        row = conn.execute(
            """
            SELECT wb.*, u.username, u.display_name, u.role, u.status,
                   u.expire_at, u.max_sessions
            FROM wechat_bindings wb
            JOIN users u ON u.id=wb.user_id
            WHERE wb.appid=? AND wb.openid=?
            """,
            (appid, openid),
        ).fetchone()
    return dict(row) if row else None


def create_binding_ticket(
    *,
    appid: str,
    openid: str,
    unionid: str = "",
    ttl_minutes: int = 10,
) -> str:
    auth_store.ensure_auth_db()
    raw = secrets.token_urlsafe(32)
    now = datetime.now()
    with auth_store._connect() as conn:
        conn.execute(
            """
            INSERT INTO wechat_login_tickets(
              token_hash, appid, openid, unionid, expires_at, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                auth_store._token_hash(raw),
                appid,
                openid,
                unionid or None,
                _format_dt(now + timedelta(minutes=max(1, ttl_minutes))),
                _format_dt(now),
            ),
        )
    return raw


def _upsert_device(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    binding_id: int,
    device_id: str,
    device_name: str,
    platform: str,
    ip: str,
    user_agent: str,
) -> int:
    now = auth_store._now()
    conn.execute(
        """
        INSERT INTO mobile_devices(
          user_id, wechat_binding_id, device_id, device_name, platform,
          ip, user_agent, created_at, last_seen_at, disabled_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(user_id, device_id) DO UPDATE SET
          wechat_binding_id=excluded.wechat_binding_id,
          device_name=excluded.device_name,
          platform=excluded.platform,
          ip=excluded.ip,
          user_agent=excluded.user_agent,
          last_seen_at=excluded.last_seen_at,
          disabled_at=NULL
        """,
        (
            user_id,
            binding_id,
            device_id,
            device_name,
            platform,
            ip,
            user_agent[:500],
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM mobile_devices WHERE user_id=? AND device_id=?",
        (user_id, device_id),
    ).fetchone()
    return int(row["id"])


def _issue_tokens_in_connection(
    conn: sqlite3.Connection,
    *,
    user: Dict[str, Any],
    binding_id: int,
    device_id: str,
    device_name: str,
    platform: str,
    ip: str,
    user_agent: str,
) -> Dict[str, Any]:
    now = datetime.now()
    access_minutes = max(5, int(os.getenv("MOBILE_ACCESS_TOKEN_MINUTES", "15")))
    refresh_days = max(1, int(os.getenv("MOBILE_REFRESH_TOKEN_DAYS", "30")))
    access_expiry = now + timedelta(minutes=access_minutes)
    refresh_expiry = now + timedelta(days=refresh_days)
    subscription_expiry = _user_expiry(user)
    if subscription_expiry:
        access_expiry = min(access_expiry, subscription_expiry)
        refresh_expiry = min(refresh_expiry, subscription_expiry)

    mobile_device_id = _upsert_device(
        conn,
        user_id=int(user["id"]),
        binding_id=binding_id,
        device_id=device_id,
        device_name=device_name,
        platform=platform,
        ip=ip,
        user_agent=user_agent,
    )
    conn.execute(
        """
        UPDATE mobile_tokens SET revoked_at=?
        WHERE device_session_id=? AND revoked_at IS NULL
        """,
        (_format_dt(now), mobile_device_id),
    )
    _clean_expired(conn)
    _enforce_session_limit(conn, user)

    access_token = secrets.token_urlsafe(36)
    refresh_token = secrets.token_urlsafe(48)
    cursor = conn.execute(
        """
        INSERT INTO mobile_tokens(
          user_id, device_session_id, access_token_hash, refresh_token_hash,
          created_at, access_expires_at, refresh_expires_at, last_seen_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user["id"]),
            mobile_device_id,
            auth_store._token_hash(access_token),
            auth_store._token_hash(refresh_token),
            _format_dt(now),
            _format_dt(access_expiry),
            _format_dt(refresh_expiry),
            _format_dt(now),
        ),
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": max(0, int((access_expiry - now).total_seconds())),
        "refresh_expires_in": max(0, int((refresh_expiry - now).total_seconds())),
        "mobile_token_id": int(cursor.lastrowid),
        "mobile_device_session_id": mobile_device_id,
    }


def issue_tokens_for_binding(
    *,
    binding: Dict[str, Any],
    device_id: str,
    device_name: str,
    platform: str,
    ip: str,
    user_agent: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    user = _validate_user(auth_store.get_user_by_id(int(binding["user_id"])))
    auth_store.ensure_auth_db()
    with auth_store._connect() as conn:
        tokens = _issue_tokens_in_connection(
            conn,
            user=user,
            binding_id=int(binding["id"]),
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            ip=ip,
            user_agent=user_agent,
        )
        conn.execute(
            "UPDATE wechat_bindings SET last_login_at=?, updated_at=? WHERE id=?",
            (auth_store._now(), auth_store._now(), int(binding["id"])),
        )
        conn.execute(
            "UPDATE users SET last_login_at=?, updated_at=? WHERE id=?",
            (auth_store._now(), auth_store._now(), int(user["id"])),
        )
        _audit(
            conn,
            action="wechat_login",
            success=True,
            user_id=int(user["id"]),
            device_session_id=tokens["mobile_device_session_id"],
            path="/api/v1/mobile/auth/wechat",
            ip=ip,
            user_agent=user_agent,
        )
    return user, tokens


def bind_account(
    *,
    binding_ticket: str,
    username: str,
    password: str,
    device_id: str,
    device_name: str,
    platform: str,
    ip: str,
    user_agent: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    user = auth_store.get_user_by_username(username, include_password=True)
    if not user or not auth_store.verify_password(password, user.get("password_hash", "")):
        raise MobileAuthError("INVALID_CREDENTIALS", "账号或密码不正确", 401)
    user = _validate_user(user)
    auth_store.ensure_auth_db()
    ticket_hash = auth_store._token_hash(binding_ticket)
    with auth_store._connect() as conn:
        ticket = conn.execute(
            """
            SELECT * FROM wechat_login_tickets
            WHERE token_hash=? AND used_at IS NULL
            """,
            (ticket_hash,),
        ).fetchone()
        if not ticket:
            raise MobileAuthError("BINDING_TICKET_INVALID", "绑定凭证无效或已使用", 401)
        if (auth_store._parse_dt(ticket["expires_at"]) or datetime.min) <= datetime.now():
            raise MobileAuthError("BINDING_TICKET_EXPIRED", "绑定凭证已过期，请重新微信登录", 401)

        existing_openid = conn.execute(
            "SELECT user_id FROM wechat_bindings WHERE appid=? AND openid=?",
            (ticket["appid"], ticket["openid"]),
        ).fetchone()
        if existing_openid and int(existing_openid["user_id"]) != int(user["id"]):
            raise MobileAuthError("WECHAT_ALREADY_BOUND", "该微信已绑定其他账号", 409)
        existing_user = conn.execute(
            "SELECT openid FROM wechat_bindings WHERE user_id=? AND appid=?",
            (int(user["id"]), ticket["appid"]),
        ).fetchone()
        if existing_user and str(existing_user["openid"]) != str(ticket["openid"]):
            raise MobileAuthError("ACCOUNT_ALREADY_BOUND", "该账号已绑定其他微信", 409)

        now = auth_store._now()
        conn.execute(
            """
            INSERT INTO wechat_bindings(
              user_id, appid, openid, unionid, created_at, updated_at, last_login_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(appid, openid) DO UPDATE SET
              unionid=COALESCE(excluded.unionid, wechat_bindings.unionid),
              updated_at=excluded.updated_at,
              last_login_at=excluded.last_login_at
            """,
            (
                int(user["id"]),
                ticket["appid"],
                ticket["openid"],
                ticket["unionid"],
                now,
                now,
                now,
            ),
        )
        binding = conn.execute(
            "SELECT * FROM wechat_bindings WHERE appid=? AND openid=?",
            (ticket["appid"], ticket["openid"]),
        ).fetchone()
        conn.execute(
            "UPDATE wechat_login_tickets SET used_at=? WHERE id=? AND used_at IS NULL",
            (now, int(ticket["id"])),
        )
        tokens = _issue_tokens_in_connection(
            conn,
            user=user,
            binding_id=int(binding["id"]),
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            ip=ip,
            user_agent=user_agent,
        )
        _audit(
            conn,
            action="bind_account",
            success=True,
            user_id=int(user["id"]),
            device_session_id=tokens["mobile_device_session_id"],
            path="/api/v1/mobile/auth/bind",
            ip=ip,
            user_agent=user_agent,
        )
    return auth_store.get_user_by_id(int(user["id"])) or {}, tokens


def validate_access_token(
    token: str | None,
) -> Tuple[Dict[str, Any] | None, str | None, Dict[str, Any] | None]:
    auth_store.ensure_auth_db()
    if not token:
        return None, "not_authenticated", None
    with auth_store._connect() as conn:
        row = conn.execute(
            """
            SELECT mt.id AS mobile_token_id,
                   mt.device_session_id,
                   mt.access_expires_at,
                   mt.refresh_expires_at,
                   mt.revoked_at,
                   md.disabled_at AS device_disabled_at,
                   u.*
            FROM mobile_tokens mt
            JOIN mobile_devices md ON md.id=mt.device_session_id
            JOIN users u ON u.id=mt.user_id
            WHERE mt.access_token_hash=?
            """,
            (auth_store._token_hash(token),),
        ).fetchone()
        if not row:
            return None, "not_authenticated", None
        data = dict(row)
        if data.get("revoked_at"):
            return None, "mobile_session_revoked", None
        if data.get("device_disabled_at"):
            return None, "mobile_device_disabled", None
        if data.get("status") != "active":
            return None, "user_disabled", None
        if (auth_store._parse_dt(data.get("access_expires_at")) or datetime.min) <= datetime.now():
            return None, "mobile_token_expired", None
        user = auth_store._row_to_user(row)
        context = {
            "mobile_token_id": int(data["mobile_token_id"]),
            "mobile_device_session_id": int(data["device_session_id"]),
        }
        if user.get("is_expired") and user.get("role") != "admin":
            return user, "subscription_expired", context
        cutoff = _format_dt(datetime.now() - timedelta(seconds=60))
        now = auth_store._now()
        conn.execute(
            """
            UPDATE mobile_tokens SET last_seen_at=?
            WHERE id=? AND (last_seen_at IS NULL OR last_seen_at < ?)
            """,
            (now, int(data["mobile_token_id"]), cutoff),
        )
        conn.execute(
            """
            UPDATE mobile_devices SET last_seen_at=?
            WHERE id=? AND (last_seen_at IS NULL OR last_seen_at < ?)
            """,
            (now, int(data["device_session_id"]), cutoff),
        )
    return user, None, context


def refresh_tokens(
    *,
    refresh_token: str,
    ip: str,
    user_agent: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    auth_store.ensure_auth_db()
    now = datetime.now()
    with auth_store._connect() as conn:
        row = conn.execute(
            """
            SELECT u.*,
                   mt.id AS mobile_token_id,
                   mt.device_session_id,
                   mt.refresh_expires_at,
                   mt.revoked_at,
                   md.disabled_at AS device_disabled_at
            FROM mobile_tokens mt
            JOIN mobile_devices md ON md.id=mt.device_session_id
            JOIN users u ON u.id=mt.user_id
            WHERE mt.refresh_token_hash=?
            """,
            (auth_store._token_hash(refresh_token),),
        ).fetchone()
        if not row or row["revoked_at"]:
            raise MobileAuthError("REFRESH_TOKEN_INVALID", "刷新令牌无效", 401)
        if row["device_disabled_at"]:
            raise MobileAuthError("MOBILE_DEVICE_DISABLED", "该设备会话已被禁用", 401)
        if (auth_store._parse_dt(row["refresh_expires_at"]) or datetime.min) <= now:
            raise MobileAuthError("REFRESH_TOKEN_EXPIRED", "登录已过期，请重新登录", 401)
        user = _validate_user(auth_store._row_to_user(row))

        access_minutes = max(5, int(os.getenv("MOBILE_ACCESS_TOKEN_MINUTES", "15")))
        refresh_days = max(1, int(os.getenv("MOBILE_REFRESH_TOKEN_DAYS", "30")))
        access_expiry = now + timedelta(minutes=access_minutes)
        refresh_expiry = now + timedelta(days=refresh_days)
        subscription_expiry = _user_expiry(user)
        if subscription_expiry:
            access_expiry = min(access_expiry, subscription_expiry)
            refresh_expiry = min(refresh_expiry, subscription_expiry)
        new_access = secrets.token_urlsafe(36)
        new_refresh = secrets.token_urlsafe(48)
        conn.execute(
            """
            UPDATE mobile_tokens
            SET access_token_hash=?, refresh_token_hash=?,
                access_expires_at=?, refresh_expires_at=?, last_seen_at=?
            WHERE id=?
            """,
            (
                auth_store._token_hash(new_access),
                auth_store._token_hash(new_refresh),
                _format_dt(access_expiry),
                _format_dt(refresh_expiry),
                _format_dt(now),
                int(row["mobile_token_id"]),
            ),
        )
        conn.execute(
            "UPDATE mobile_devices SET ip=?, user_agent=?, last_seen_at=? WHERE id=?",
            (ip, user_agent[:500], _format_dt(now), int(row["device_session_id"])),
        )
        _audit(
            conn,
            action="refresh_token",
            success=True,
            user_id=int(user["id"]),
            device_session_id=int(row["device_session_id"]),
            path="/api/v1/mobile/auth/refresh",
            ip=ip,
            user_agent=user_agent,
        )
    return user, {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "Bearer",
        "expires_in": max(0, int((access_expiry - now).total_seconds())),
        "refresh_expires_in": max(0, int((refresh_expiry - now).total_seconds())),
        "mobile_token_id": int(row["mobile_token_id"]),
        "mobile_device_session_id": int(row["device_session_id"]),
    }


def revoke_access_token(
    token: str | None,
    *,
    ip: str = "",
    user_agent: str = "",
) -> None:
    if not token:
        return
    auth_store.ensure_auth_db()
    with auth_store._connect() as conn:
        row = conn.execute(
            "SELECT id, user_id, device_session_id FROM mobile_tokens WHERE access_token_hash=?",
            (auth_store._token_hash(token),),
        ).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE mobile_tokens SET revoked_at=? WHERE id=?",
            (auth_store._now(), int(row["id"])),
        )
        _audit(
            conn,
            action="logout",
            success=True,
            user_id=int(row["user_id"]),
            device_session_id=int(row["device_session_id"]),
            path="/api/v1/mobile/auth/logout",
            ip=ip,
            user_agent=user_agent,
        )


__all__ = [
    "MobileAuthError",
    "bind_account",
    "create_binding_ticket",
    "find_binding",
    "issue_tokens_for_binding",
    "refresh_tokens",
    "revoke_access_token",
    "validate_access_token",
]
