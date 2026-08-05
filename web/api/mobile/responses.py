"""Response helpers shared by mobile routes and middleware."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from uuid import uuid4

from fastapi.responses import JSONResponse


def mobile_payload(
    data: Any = None,
    *,
    trade_date: str = "",
    generated_at: str = "",
    source_status: str = "ready",
    is_realtime: bool = False,
    cache_age_seconds: float | None = None,
    request_id: str = "",
) -> Dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": {
            "trade_date": str(trade_date or ""),
            "generated_at": str(generated_at or datetime.now().isoformat(timespec="seconds")),
            "source_status": source_status,
            "is_realtime": bool(is_realtime),
            "cache_age_seconds": cache_age_seconds,
            "request_id": request_id or uuid4().hex,
        },
        "error": None,
    }


def mobile_error_response(
    code: str,
    message: str,
    *,
    status_code: int,
    details: Dict[str, Any] | None = None,
    request_id: str = "",
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "data": None,
            "meta": {
                "trade_date": "",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source_status": "error",
                "is_realtime": False,
                "cache_age_seconds": None,
                "request_id": request_id or uuid4().hex,
            },
            "error": {"code": code, "message": message, "details": details},
        },
        status_code=status_code,
    )


__all__ = ["mobile_error_response", "mobile_payload"]
