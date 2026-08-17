from __future__ import annotations

import logging
from typing import Any, Callable, Optional


class ModeScopedPreRunResetService:
    """Applies a minimal pre-run reset for modes that require it."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def apply(
        self,
        *,
        session: Any,
        mode_id: Optional[str],
        clear_runtime_cache: Callable[[str], None],
        clear_pending_questions: Callable[[str], int],
    ) -> bool:
        return False
