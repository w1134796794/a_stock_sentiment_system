"""Database-backed authentication and subscription control for the web app."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.settings import WEB_DATA_DIR

AUTH_DB_PATH = Path(WEB_DATA_DIR) / "auth.sqlite"
SESSION_COOKIE_NAME = "a_stock_sentiment_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7
PASSWORD_ITERATIONS = 260_000


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date_after(days: int) -> str:
    return (datetime.now() + timedelta(days=int(days))).strftime("%Y-%m-%d")


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt in ("%Y-%m-%d", "%Y%m%d"):
                return dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    return None


def _connect() -> sqlite3.Connection:
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iter_text, salt_text, digest_text = str(encoded or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_text)
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        digest = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_auth_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL DEFAULT '',
              password_hash TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('admin','viewer')),
              status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
              expire_at TEXT,
              max_sessions INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              token_hash TEXT NOT NULL UNIQUE,
              ip TEXT NOT NULL DEFAULT '',
              user_agent TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              last_seen_at TEXT,
              revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_active
              ON sessions(user_id, revoked_at, expires_at);
            CREATE TABLE IF NOT EXISTS login_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER,
              username TEXT NOT NULL DEFAULT '',
              ip TEXT NOT NULL DEFAULT '',
              user_agent TEXT NOT NULL DEFAULT '',
              success INTEGER NOT NULL DEFAULT 0,
              reason TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            now = _now()
            admin_user = os.getenv("APP_ADMIN_USER", "admin")
            admin_password = os.getenv("APP_ADMIN_PASSWORD", "admin123")
            conn.execute(
                """
                INSERT INTO users(username, display_name, password_hash, role, status,
                                  expire_at, max_sessions, created_at, updated_at)
                VALUES(?, ?, ?, 'admin', 'active', NULL, 2, ?, ?)
                """,
                (admin_user, "超级管理员", hash_password(admin_password), now, now),
            )
            viewer_user = os.getenv("APP_VIEWER_USER", "").strip()
            viewer_password = os.getenv("APP_VIEWER_PASSWORD", "").strip()
            if viewer_user and viewer_password and viewer_user != admin_user:
                conn.execute(
                    """
                    INSERT INTO users(username, display_name, password_hash, role, status,
                                      expire_at, max_sessions, created_at, updated_at)
                    VALUES(?, ?, ?, 'viewer', 'active', ?, 1, ?, ?)
                    """,
                    (viewer_user, "只读账号", hash_password(viewer_password), _date_after(30), now, now),
                )


def _row_to_user(row: sqlite3.Row, *, include_password: bool = False) -> Dict[str, Any]:
    user = dict(row)
    if not include_password:
        user.pop("password_hash", None)
    expire_dt = _parse_dt(user.get("expire_at"))
    user["is_expired"] = bool(expire_dt and expire_dt < datetime.now())
    user["expire_at_display"] = user.get("expire_at") or "长期"
    user["label"] = "超级账号" if user.get("role") == "admin" else "只读账号"
    return user


def get_user_by_username(username: str, *, include_password: bool = False) -> Optional[Dict[str, Any]]:
    ensure_auth_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _row_to_user(row, include_password=include_password) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    ensure_auth_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()
    return _row_to_user(row) if row else None


def record_login(
    *,
    username: str,
    user_id: Optional[int],
    ip: str,
    user_agent: str,
    success: bool,
    reason: str,
) -> None:
    ensure_auth_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO login_logs(user_id, username, ip, user_agent, success, reason, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, ip, user_agent[:500], 1 if success else 0, reason, _now()),
        )


def _active_session_count(conn: sqlite3.Connection, user_id: int) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM sessions
            WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?
            """,
            (user_id, _now()),
        ).fetchone()[0]
    )


def login(
    *,
    username: str,
    password: str,
    ip: str,
    user_agent: str,
) -> Tuple[bool, str, Optional[str], Optional[Dict[str, Any]], int]:
    """Validate credentials and create a session.

    Returns: (ok, message, raw_token, user, max_age_seconds)
    """
    ensure_auth_db()
    username = str(username or "").strip()
    user = get_user_by_username(username, include_password=True)
    if not user:
        record_login(username=username, user_id=None, ip=ip, user_agent=user_agent, success=False, reason="用户不存在")
        return False, "账号或密码不正确", None, None, 0
    if not verify_password(password, user.get("password_hash", "")):
        record_login(username=username, user_id=user["id"], ip=ip, user_agent=user_agent, success=False, reason="密码错误")
        return False, "账号或密码不正确", None, None, 0
    if user.get("status") != "active":
        record_login(username=username, user_id=user["id"], ip=ip, user_agent=user_agent, success=False, reason="账号已禁用")
        return False, "账号已被禁用，请联系管理员", None, user, 0
    if user.get("is_expired") and user.get("role") != "admin":
        record_login(username=username, user_id=user["id"], ip=ip, user_agent=user_agent, success=False, reason="服务已到期")
        return False, "服务已到期，请联系管理员续费", None, user, 0

    now_dt = datetime.now()
    session_expire = now_dt + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
    user_expire = _parse_dt(user.get("expire_at"))
    if user_expire and user_expire < session_expire:
        session_expire = user_expire
    max_age = max(60, int((session_expire - now_dt).total_seconds()))
    token = secrets.token_urlsafe(36)
    token_hash = _token_hash(token)
    with _connect() as conn:
        max_sessions = max(1, int(user.get("max_sessions") or 1))
        if _active_session_count(conn, user["id"]) >= max_sessions:
            policy = os.getenv("APP_SESSION_POLICY", "kick_oldest").strip().lower()
            if policy == "reject_new":
                conn.execute(
                    """
                    INSERT INTO login_logs(user_id, username, ip, user_agent, success, reason, created_at)
                    VALUES(?, ?, ?, ?, 0, ?, ?)
                    """,
                    (user["id"], username, ip, user_agent[:500], "在线设备数已达上限", _now()),
                )
                return False, "在线设备数已达上限，请先退出其他设备", None, user, 0
            rows = conn.execute(
                """
                SELECT id FROM sessions
                WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?
                ORDER BY COALESCE(last_seen_at, created_at), created_at
                """,
                (user["id"], _now()),
            ).fetchall()
            for row in rows[: max(0, len(rows) - max_sessions + 1)]:
                conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (_now(), row["id"]))

        conn.execute(
            """
            INSERT INTO sessions(user_id, token_hash, ip, user_agent, created_at, expires_at, last_seen_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"], token_hash, ip, user_agent[:500], _now(),
                session_expire.strftime("%Y-%m-%d %H:%M:%S"), _now(),
            ),
        )
        conn.execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?", (_now(), _now(), user["id"]))
    record_login(username=username, user_id=user["id"], ip=ip, user_agent=user_agent, success=True, reason="登录成功")
    return True, "登录成功", token, get_user_by_id(user["id"]), max_age


def validate_session(token: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_auth_db()
    if not token:
        return None, "not_authenticated"
    hashed = _token_hash(token)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT s.id AS session_id, s.expires_at, s.revoked_at, u.*
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash=?
            """,
            (hashed,),
        ).fetchone()
        if not row:
            return None, "not_authenticated"
        data = dict(row)
        if data.get("revoked_at"):
            return None, "session_revoked"
        if (_parse_dt(data.get("expires_at")) or datetime.min) < datetime.now():
            return None, "session_expired"
        if data.get("status") != "active":
            return None, "user_disabled"
        user = _row_to_user(row)
        user["session_id"] = data["session_id"]
        if user.get("is_expired") and user.get("role") != "admin":
            return user, "subscription_expired"
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (_now(), data["session_id"]))
    return user, None


def revoke_session(token: Optional[str]) -> None:
    if not token:
        return
    ensure_auth_db()
    with _connect() as conn:
        conn.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (_now(), _token_hash(token)))


def revoke_user_sessions(user_id: int) -> None:
    ensure_auth_db()
    with _connect() as conn:
        conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (_now(), int(user_id)))


def list_users() -> List[Dict[str, Any]]:
    ensure_auth_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT u.*,
              (SELECT COUNT(*) FROM sessions s
               WHERE s.user_id=u.id AND s.revoked_at IS NULL AND s.expires_at > ?) AS active_sessions
            FROM users u
            ORDER BY CASE u.role WHEN 'admin' THEN 0 ELSE 1 END, u.id
            """,
            (_now(),),
        ).fetchall()
    return [_row_to_user(row) | {"active_sessions": row["active_sessions"]} for row in rows]


def create_user(
    *,
    username: str,
    password: str,
    role: str = "viewer",
    display_name: str = "",
    expire_at: Optional[str] = None,
    days: Optional[int] = None,
    max_sessions: int = 1,
) -> Dict[str, Any]:
    ensure_auth_db()
    username = str(username or "").strip()
    if not username:
        raise ValueError("账号不能为空")
    if not password:
        raise ValueError("密码不能为空")
    role = role if role in {"admin", "viewer"} else "viewer"
    if role == "admin":
        expire = None
        max_sessions = max(1, int(max_sessions or 2))
    else:
        expire = str(expire_at or "").strip() or _date_after(int(days or 30))
        max_sessions = max(1, min(5, int(max_sessions or 1)))
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO users(username, display_name, password_hash, role, status,
                              expire_at, max_sessions, created_at, updated_at)
            VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (username, display_name or username, hash_password(password), role, expire, max_sessions, now, now),
        )
        user_id = int(cur.lastrowid)
    return get_user_by_id(user_id) or {}


def update_user_status(user_id: int, status: str) -> Dict[str, Any]:
    status = "active" if status == "active" else "disabled"
    ensure_auth_db()
    with _connect() as conn:
        conn.execute("UPDATE users SET status=?, updated_at=? WHERE id=?", (status, _now(), int(user_id)))
        if status == "disabled":
            conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (_now(), int(user_id)))
    return get_user_by_id(user_id) or {}


def extend_user(user_id: int, *, days: Optional[int] = None, expire_at: Optional[str] = None) -> Dict[str, Any]:
    ensure_auth_db()
    user = get_user_by_id(user_id)
    if not user:
        raise ValueError("用户不存在")
    if user.get("role") == "admin":
        expire = None
    elif expire_at:
        expire = str(expire_at).strip()
    else:
        base = _parse_dt(user.get("expire_at"))
        if not base or base < datetime.now():
            base = datetime.now()
        expire = (base + timedelta(days=int(days or 30))).strftime("%Y-%m-%d")
    with _connect() as conn:
        conn.execute("UPDATE users SET expire_at=?, status='active', updated_at=? WHERE id=?", (expire, _now(), int(user_id)))
    return get_user_by_id(user_id) or {}


def reset_password(user_id: int, password: str) -> Dict[str, Any]:
    if not password:
        raise ValueError("密码不能为空")
    ensure_auth_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (hash_password(password), _now(), int(user_id)),
        )
        conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (_now(), int(user_id)))
    return get_user_by_id(user_id) or {}


def update_user_limits(user_id: int, *, max_sessions: int, display_name: str = "") -> Dict[str, Any]:
    ensure_auth_db()
    max_sessions = max(1, min(5, int(max_sessions or 1)))
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET max_sessions=?, display_name=?, updated_at=? WHERE id=?",
            (max_sessions, display_name, _now(), int(user_id)),
        )
    return get_user_by_id(user_id) or {}


def recent_login_logs(limit: int = 80) -> List[Dict[str, Any]]:
    ensure_auth_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM login_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(300, int(limit or 80))),),
        ).fetchall()
    return [dict(row) for row in rows]
