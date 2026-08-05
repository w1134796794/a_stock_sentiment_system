"""Server-side WeChat Mini Program identity exchange."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

import requests


class WeChatIdentityError(RuntimeError):
    """Raised when a js_code cannot be exchanged for a WeChat identity."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WeChatIdentity:
    appid: str
    openid: str
    unionid: str = ""


class WeChatCodeClient:
    endpoint = "https://api.weixin.qq.com/sns/jscode2session"

    def __init__(
        self,
        *,
        appid: str | None = None,
        secret: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.appid = str(appid or os.getenv("WX_MINIPROGRAM_APP_ID") or "").strip()
        self.secret = str(secret or os.getenv("WX_MINIPROGRAM_APP_SECRET") or "").strip()
        self.timeout_seconds = max(float(timeout_seconds), 1.0)

    def exchange(self, js_code: str) -> WeChatIdentity:
        code = str(js_code or "").strip()
        if not self.appid or not self.secret:
            raise WeChatIdentityError(
                "WECHAT_NOT_CONFIGURED",
                "服务器尚未配置微信小程序 AppID 或 AppSecret",
            )
        if not code:
            raise WeChatIdentityError("INVALID_JS_CODE", "微信登录凭证不能为空")

        try:
            response = requests.get(
                self.endpoint,
                params={
                    "appid": self.appid,
                    "secret": self.secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload: Dict[str, Any] = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WeChatIdentityError(
                "WECHAT_UPSTREAM_UNAVAILABLE",
                "微信登录服务暂时不可用，请稍后重试",
            ) from exc

        if payload.get("errcode"):
            raise WeChatIdentityError(
                "WECHAT_CODE_REJECTED",
                f"微信登录凭证无效：{payload.get('errmsg') or payload.get('errcode')}",
            )
        openid = str(payload.get("openid") or "").strip()
        if not openid:
            raise WeChatIdentityError(
                "WECHAT_IDENTITY_MISSING",
                "微信登录响应缺少 openid",
            )
        return WeChatIdentity(
            appid=self.appid,
            openid=openid,
            unionid=str(payload.get("unionid") or "").strip(),
        )


__all__ = [
    "WeChatCodeClient",
    "WeChatIdentity",
    "WeChatIdentityError",
]
