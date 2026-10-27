"""Independent bearer authentication for the storage-only integration namespace."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

PREFIX = "/api/v1/readonly"


def is_readonly_path(path):
    return path == PREFIX or path.startswith(PREFIX + "/")


class ReadonlyAccess:
    """Fails closed; cookies never authenticate this namespace or gain API scope."""

    def __init__(self, config_path=None, *, limit=60, max_concurrent=2, clock=None):
        if config_path is None:
            from config.settings import WEB_DATA_DIR

            config_path = Path(WEB_DATA_DIR) / "settings" / "readonly_api.json"
        self.config_path = Path(config_path)
        self.limit = limit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._clients = OrderedDict()
        self._lock = Lock()
        self._slots = BoundedSemaphore(max_concurrent)

    def enabled_hash(self):
        try:
            with self.config_path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return None
            value = json.loads(raw)
            digest = value.get("token_sha256", "")
            expiry = datetime.fromisoformat(value["expires_at"])
            if (
                value.get("enabled") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or expiry.tzinfo is None
                or expiry <= self.clock()
            ):
                return None
            return digest
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return None

    def limited(self, client):
        now = monotonic()
        with self._lock:
            hits = self._clients.pop(client, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            limited = len(hits) >= self.limit
            if not limited:
                hits.append(now)
            self._clients[client] = hits
            while len(self._clients) > 4096:
                self._clients.popitem(last=False)
            return limited

    async def __call__(self, request, call_next):
        def error(code, status, **headers):
            return JSONResponse({"ok": False, "error": code}, status_code=status, headers=headers)

        if request.url.scheme != "https":
            response = error("https_required", 426)
        elif request.method != "GET":
            response = error("read_only_method", 405, Allow="GET")
        elif self.limited(request.client.host if request.client else "unknown"):
            response = error("rate_limited", 429, **{"Retry-After": "60"})
        else:
            expected = await run_in_threadpool(self.enabled_hash)
            authorization = request.headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            valid = (
                expected is not None
                and scheme.lower() == "bearer"
                and 32 <= len(token) <= 256
                and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)
            )
            if expected is None:
                response = error("readonly_api_disabled", 503)
            elif not valid:
                response = error("invalid_readonly_token", 401, **{"WWW-Authenticate": "Bearer"})
            elif not self._slots.acquire(blocking=False):
                response = error("readonly_api_busy", 429, **{"Retry-After": "1"})
            else:
                try:
                    response = await call_next(request)
                finally:
                    self._slots.release()
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
