"""Coalesced market-data revision; events are hints, storage is authoritative."""
from __future__ import annotations

from uuid import uuid4


REVISION_KEY = "realtime:market:revision"
CHANNEL = "realtime:market:changed"


def version_key(date):
    return f"realtime:market:code-versions:{date}"


def code_versions(backend, date):
    client = getattr(backend, "client", None)
    return (client.hgetall(backend._key(version_key(date))) if client is not None
            else backend.get_json(version_key(date)) or {})


def publish_revision(backend, date, *, kind, codes=None):
    value = {"id": uuid4().hex, "market_date": date, "kind": kind}
    versions = {str(code): value["id"] for code in codes or []}
    client = getattr(backend, "client", None)
    if versions:
        key = version_key(date)
        if client is not None:
            pipe = client.pipeline(transaction=True)
            pipe.hset(backend._key(key), mapping=versions)
            pipe.expire(backend._key(key), 172800)
            pipe.execute()
        else:
            backend.set_json(key, {**code_versions(backend, date), **versions}, 172800)
    value["scoped"] = bool(versions)
    backend.set_json(REVISION_KEY, value, 86400)
    if client is not None:
        try:
            client.publish(backend._key(CHANNEL), value["id"])
        except Exception:
            from loguru import logger
            logger.warning("Market wake-up publish failed; persisted revision remains available")
    return value
