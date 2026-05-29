from __future__ import annotations

from modes.sdk import BaseMode


class _LazyManagerMode(BaseMode):
    """
    Lazy proxy that defers ManagerMode import until instantiation.
    Prevents circular import between `agent.manager` and `modes.manager.mode`.
    """

    def __new__(cls, *args, **kwargs):
        from .mode import ManagerMode

        return ManagerMode(*args, **kwargs)


def __getattr__(name: str):
    if name == "ManagerMode":
        from .mode import ManagerMode

        return ManagerMode
    raise AttributeError(name)


PLUGIN = _LazyManagerMode

__all__ = ["ManagerMode", "PLUGIN"]
