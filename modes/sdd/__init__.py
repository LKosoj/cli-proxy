from __future__ import annotations

from modes.sdk import BaseMode


class _LazySddMode(BaseMode):
    """
    Lazy proxy that defers SddMode import until instantiation.
    Prevents circular import issues at module load time.
    """

    def __new__(cls, *args, **kwargs):
        from .mode import SddMode

        return SddMode(*args, **kwargs)


def __getattr__(name: str):
    if name == "SddMode":
        from .mode import SddMode

        return SddMode
    raise AttributeError(name)


PLUGIN = _LazySddMode

__all__ = ["SddMode", "PLUGIN"]
