"""WeChat login and token lifecycle endpoints for mobile clients."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

from web.api.mobile.responses import mobile_error_response, mobile_payload
from web.api.mobile.schemas import (
    MobileEnvelope,
    MobileRefreshRequest,
    WeChatBindRequest,
    WeChatLoginRequest,
)
from web.mobile_auth_service import MobileAuthService, public_user
from web.mobile_auth_store import MobileAuthError


def _client_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    return forwarded or (request.client.host if request.client else "")


def _bearer_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def create_mobile_auth_router(service: MobileAuthService) -> APIRouter:
    router = APIRouter(prefix="/auth")

    def execute(call):
        try:
            return mobile_payload(call())
        except MobileAuthError as exc:
            return mobile_error_response(
                exc.code,
                exc.message,
                status_code=exc.status_code,
            )

    @router.post("/wechat", response_model=MobileEnvelope)
    def wechat_login(payload: WeChatLoginRequest, request: Request) -> Dict[str, Any]:
        return execute(
            lambda: service.wechat_login(
                js_code=payload.js_code,
                device_id=payload.device_id,
                device_name=payload.device_name,
                platform=payload.platform,
                ip=_client_ip(request),
                user_agent=str(request.headers.get("user-agent") or ""),
            )
        )

    @router.post("/bind", response_model=MobileEnvelope)
    def bind(payload: WeChatBindRequest, request: Request) -> Dict[str, Any]:
        return execute(
            lambda: service.bind(
                binding_ticket=payload.binding_ticket,
                username=payload.username,
                password=payload.password,
                device_id=payload.device_id,
                device_name=payload.device_name,
                platform=payload.platform,
                ip=_client_ip(request),
                user_agent=str(request.headers.get("user-agent") or ""),
            )
        )

    @router.post("/refresh", response_model=MobileEnvelope)
    def refresh(payload: MobileRefreshRequest, request: Request) -> Dict[str, Any]:
        return execute(
            lambda: service.refresh(
                refresh_token=payload.refresh_token,
                ip=_client_ip(request),
                user_agent=str(request.headers.get("user-agent") or ""),
            )
        )

    @router.post("/logout", response_model=MobileEnvelope)
    def logout(request: Request) -> Dict[str, Any]:
        return execute(
            lambda: service.logout(
                access_token=_bearer_token(request),
                ip=_client_ip(request),
                user_agent=str(request.headers.get("user-agent") or ""),
            )
        )

    @router.get("/profile", response_model=MobileEnvelope)
    def profile(request: Request) -> Dict[str, Any]:
        return mobile_payload(
            {
                "status": "authenticated",
                "user": public_user(dict(request.state.user or {})),
                "device_session_id": getattr(
                    request.state,
                    "mobile_device_session_id",
                    None,
                ),
            }
        )

    return router


__all__ = ["create_mobile_auth_router"]
