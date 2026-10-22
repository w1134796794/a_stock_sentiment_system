"""Coalesced market-data revision; events are hints, storage is authoritative."""
from __future__ import annotations

from uuid import uuid4


REVISION_KEY = "realtime:market:revision"
CHANNEL = "realtime:market:changed"


def publish_revision(backend, date, *, kind):
    value = {"id": uuid4().hex, "market_date": date, "kind": kind}
    backend.set_json(REVISION_KEY, value, 86400)
    client = getattr(backend, "client", None)
    if client is not None:
        try:
            client.publish(backend._key(CHANNEL), value["id"])
        except Exception:
            from loguru import logger
            logger.warning("Market wake-up publish failed; persisted revision remains available")
    return value
