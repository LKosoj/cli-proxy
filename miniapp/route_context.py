from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MiniAppRouteContext:
    """Stable dependencies shared by extracted MiniApp route modules."""

    bot_app: Any
    logger: logging.Logger
