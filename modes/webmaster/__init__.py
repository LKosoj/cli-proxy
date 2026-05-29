from __future__ import annotations

from .mode import WebmasterMode
from .runner_service import WebmasterModeRunnerService

PLUGIN = WebmasterMode

__all__ = ["WebmasterMode", "WebmasterModeRunnerService", "PLUGIN"]
