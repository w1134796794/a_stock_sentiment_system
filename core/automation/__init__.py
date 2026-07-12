"""In-process automation owned by the web application lifecycle."""

from core.automation.internal_scheduler import AUTOMATION_SCHEDULER, InternalScheduler

__all__ = ["AUTOMATION_SCHEDULER", "InternalScheduler"]
