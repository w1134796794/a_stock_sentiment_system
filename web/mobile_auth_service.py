"""Application-facing mobile authentication flow."""

from __future__ import annotations

from typing import Any, Dict

from web.mobile_auth_store import (
    MobileAuthError,
    bind_account,
    create_binding_ticket,
    find_binding,
    issue_tokens_for_binding,
    refresh_tokens,
    revoke_access_token,
)
from web.wechat_client import WeChatCodeClient, WeChatIdentityError


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "display_name": user.get("display_name") or user.get("username"),
        "role": user.get("role"),
        "expire_at": user.get("expire_at"),
        "expire_at_display": user.get("expire_at_display"),
        "max_sessions": user.get("max_sessions"),
    }


class MobileAuthService:
    def __init__(self, identity_client: WeChatCodeClient | None = None) -> None:
        self.identity_client = identity_client or WeChatCodeClient()

    def wechat_login(
        self,
        *,
        js_code: str,
        device_id: str,
        device_name: str,
        platform: str,
        ip: str,
        user_agent: str,
    ) -> Dict[str, Any]:
        try:
            identity = self.identity_client.exchange(js_code)
        except WeChatIdentityError as exc:
            raise MobileAuthError(exc.code, exc.message, 503 if exc.code.endswith("UNAVAILABLE") else 400) from exc
        binding = find_binding(identity.appid, identity.openid)
        if not binding:
            ticket = create_binding_ticket(
                appid=identity.appid,
                openid=identity.openid,
                unionid=identity.unionid,
            )
            return {
                "status": "binding_required",
                "binding_ticket": ticket,
                "binding_ticket_expires_in": 600,
            }
        user, tokens = issue_tokens_for_binding(
            binding=binding,
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            ip=ip,
            user_agent=user_agent,
        )
        return {
            "status": "authenticated",
            "user": public_user(user),
            **tokens,
        }

    def bind(
        self,
        *,
        binding_ticket: str,
        username: str,
        password: str,
        device_id: str,
        device_name: str,
        platform: str,
        ip: str,
        user_agent: str,
    ) -> Dict[str, Any]:
        user, tokens = bind_account(
            binding_ticket=binding_ticket,
            username=username,
            password=password,
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            ip=ip,
            user_agent=user_agent,
        )
        return {
            "status": "authenticated",
            "user": public_user(user),
            **tokens,
        }

    @staticmethod
    def refresh(*, refresh_token: str, ip: str, user_agent: str) -> Dict[str, Any]:
        user, tokens = refresh_tokens(
            refresh_token=refresh_token,
            ip=ip,
            user_agent=user_agent,
        )
        return {
            "status": "authenticated",
            "user": public_user(user),
            **tokens,
        }

    @staticmethod
    def logout(*, access_token: str, ip: str, user_agent: str) -> Dict[str, Any]:
        revoke_access_token(access_token, ip=ip, user_agent=user_agent)
        return {"status": "logged_out"}


__all__ = ["MobileAuthService", "public_user"]
