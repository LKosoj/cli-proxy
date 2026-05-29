from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MenuItemModel:
    """
    Neutral menu item description.

    Core/UI layers may map this to transport-specific constructs
    (e.g. Telegram inline buttons) by encoding `action`+`payload`.
    """

    label: str
    action: str
    payload: Dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    hint: Optional[str] = None


@dataclass
class MenuModel:
    """Neutral menu description that can be rendered by a UI adapter."""

    title: str
    items: List[MenuItemModel] = field(default_factory=list)
    text: Optional[str] = None
    columns: int = 1

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("MenuModel.title is required")
        try:
            self.columns = int(self.columns)
        except Exception as e:
            raise ValueError("MenuModel.columns must be int") from e
        if self.columns < 1:
            raise ValueError("MenuModel.columns must be >= 1")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MessageModel:
    """Transport-neutral user input abstraction."""

    text: str
    chat_id: Any
    user_id: Optional[int] = None
    message_id: Optional[int] = None
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.text is None:
            raise ValueError("MessageModel.text must not be None")
        if self.chat_id is None:
            raise ValueError("MessageModel.chat_id is required")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CallbackModel:
    """Transport-neutral interaction abstraction (e.g. button presses)."""

    action: str
    chat_id: Any
    payload: Dict[str, Any] = field(default_factory=dict)
    user_id: Optional[int] = None
    message_id: Optional[int] = None
    raw: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("CallbackModel.action is required")
        if self.chat_id is None:
            raise ValueError("CallbackModel.chat_id is required")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolResult:
    """
    Generic result container for tool/mode execution.

    Convention matches existing tool plugins: success/output/error + optional structured data.
    """

    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if bool(self.success) and self.error:
            raise ValueError("ToolResult.error must be empty when success=True")
        if (not bool(self.success)) and not self.error:
            # Keep it strict: failures must provide an error string.
            raise ValueError("ToolResult.error is required when success=False")

    @classmethod
    def ok(cls, output: str = "", *, data: Optional[Dict[str, Any]] = None, artifacts: Optional[List[Dict[str, Any]]] = None) -> ToolResult:
        return cls(success=True, output=output, error=None, data=data or {}, artifacts=artifacts or [])

    @classmethod
    def fail(cls, error: str, *, output: str = "", data: Optional[Dict[str, Any]] = None) -> ToolResult:
        return cls(success=False, output=output, error=error, data=data or {}, artifacts=[])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
