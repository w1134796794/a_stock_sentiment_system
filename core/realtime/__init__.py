"""Realtime service layer.

Service classes are resolved lazily so importing one realtime submodule does not
eagerly import every other service.  This matters for FastAPI sync endpoints,
which may initialize different realtime services concurrently in worker threads.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.realtime.models import QuoteSnapshot, SectorSnapshot

if TYPE_CHECKING:
    from core.realtime.overlay_service import RealtimeOverlayService
    from core.realtime.quote_service import RealtimeQuoteService
    from core.realtime.sector_service import RealtimeSectorService

__all__ = [
    "QuoteSnapshot",
    "SectorSnapshot",
    "RealtimeOverlayService",
    "RealtimeQuoteService",
    "RealtimeSectorService",
]


def __getattr__(name: str) -> Any:
    if name == "RealtimeOverlayService":
        from core.realtime.overlay_service import RealtimeOverlayService

        value = RealtimeOverlayService
    elif name == "RealtimeQuoteService":
        from core.realtime.quote_service import RealtimeQuoteService

        value = RealtimeQuoteService
    elif name == "RealtimeSectorService":
        from core.realtime.sector_service import RealtimeSectorService

        value = RealtimeSectorService
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
