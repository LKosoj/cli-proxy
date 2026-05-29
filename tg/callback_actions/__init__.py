"""Composable callback action mixins."""

from .dirs import DirsActionsMixin
from .files import FileActionsMixin
from .preset import PresetActionsMixin
from .protocol import ProtocolActionsMixin
from .session import SessionActionsMixin


class CallbackActionsMixin(
    ProtocolActionsMixin,
    SessionActionsMixin,
    DirsActionsMixin,
    FileActionsMixin,
    PresetActionsMixin,
):
    """Unified callbacks mixin assembled from domain-specific modules."""


__all__ = ["CallbackActionsMixin"]
