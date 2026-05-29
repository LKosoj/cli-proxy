from __future__ import annotations

from modes.sdk import BaseMode


class _LazyAnalystMode(BaseMode):
    """Delay AnalystMode import to avoid circular imports during bootstrap."""

    def __new__(cls, *args, **kwargs):
        from .mode import AnalystMode

        return AnalystMode(*args, **kwargs)


def __getattr__(name: str):
    if name == "AnalystMode":
        from .mode import AnalystMode

        return AnalystMode
    raise AttributeError(name)


PLUGIN = _LazyAnalystMode

__all__ = ["AnalystMode", "PLUGIN"]
