from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from agent.tooling.spec import ToolSpec


class ToolPlugin(ABC):
    plugin_id: Optional[str] = None
    function_prefix: Optional[str] = None

    def get_plugin_id(self) -> str:
        return self.plugin_id or self.__class__.__name__

    def get_function_prefix(self) -> Optional[str]:
        """
        Optional function name prefix for ToolRegistry.

        IMPORTANT: OpenAI tool/function names are best kept simple (letters/digits/_/-)
        to avoid provider-side restrictions and reduce hallucinated name mismatches.
        Therefore, prefixing is opt-in: unless a plugin explicitly sets
        ``self.function_prefix``, no prefix is applied.
        """
        return self.function_prefix

    def initialize(self, config: Any = None, services: Optional[Dict[str, Any]] = None) -> None:
        self.config = config
        self.services = services or {}

    def close(self) -> None:
        return None

    def get_source_name(self) -> str:
        return self.get_plugin_id()

    def get_commands(self) -> List[Dict[str, Any]]:
        return []

    # Telegram UI integration (optional).
    #
    # These are intentionally lightweight and return either:
    # - ready-to-register telegram.ext handler objects (ConversationHandler, InlineQueryHandler, etc), or
    # - dict configs that bot.py can adapt into handlers (to avoid tight coupling in the agent layer).
    def get_message_handlers(self) -> List[Dict[str, Any]]:
        return []

    def get_inline_handlers(self) -> List[Dict[str, Any]]:
        return []

    def get_menu_label(self) -> Optional[str]:
        """Human-friendly name for the two-level plugin menu.

        Return a short string (e.g. "Задачи", "Документы") to appear as
        a button in the first level of the plugin menu.  Return ``None``
        (default) to hide the plugin from the menu entirely.
        """
        return None

    def get_menu_actions(self) -> List[Dict[str, str]]:
        """Actions shown as buttons in the plugin submenu.

        Each entry is ``{"label": "...", "action": "..."}``.
        The ``action`` string must correspond to a key returned by
        ``callback_handlers()`` (from ``DialogMixin``) so pressing the
        button routes to the correct handler automatically.

        Return ``[]`` (default) to hide from the menu.
        """
        return []

    def awaiting_input(self, chat_id: int) -> bool:
        """Return True if this plugin is waiting for free-text input from the user.

        When True, the plugin's message handler takes priority and the core
        ``on_message`` handler will not forward the text to the CLI session.
        Override in subclasses that use interactive dialogs.
        """
        return False

    def cancel_input(self, chat_id: int) -> bool:
        """Cancel a pending input dialog for the given chat.

        Returns True if there was an active dialog that was cancelled.
        Override in subclasses that use interactive dialogs.
        """
        return False

    @abstractmethod
    def get_spec(self) -> ToolSpec:
        raise NotImplementedError

    @abstractmethod
    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# DialogMixin — unified interactive dialog framework for plugins
# ---------------------------------------------------------------------------

# Step handler can be either a plain callable (message-only, backward compat)
# or a dict with optional "message" and "callback" keys.
StepHandler = Union[Callable, Dict[str, Callable]]


@dataclass
class DialogState:
    """State of an active dialog for a single chat."""
    step: str
    data: Dict[str, Any] = field(default_factory=dict)
    user_id: int = 0
    started_at: float = field(default_factory=time.time)


class DialogMixin:
    """Mixin that provides a standard multi-step dialog protocol.

    Plugins that need interactive text/media input from the user should
    inherit from both ``DialogMixin`` and ``ToolPlugin``::

        class MyPlugin(DialogMixin, ToolPlugin):
            def dialog_steps(self):
                return {"wait_input": self._on_input}

    **Important**: ``DialogMixin`` must come first in MRO so its
    ``get_message_handlers`` / ``awaiting_input`` / ``cancel_input``
    override the defaults from ``ToolPlugin``.

    The mixin handles:
    * Centralised cancel-word detection (``CANCEL_WORDS``).
    * Automatic timeout of forgotten dialogs (``DIALOG_TIMEOUT``).
    * Unified ``awaiting_input`` / ``cancel_input`` that satisfy the
      ``ToolPlugin`` protocol so the bot core can query dialog activity.
    * A single ``handle_message`` entry-point dispatched by step name.
    * A single ``handle_callback`` entry-point for inline buttons on steps.
    * ``callback_handlers()`` for autonomous inline-menu actions outside dialogs.
    * Default ``get_message_handlers`` returning a dict-config ready for
      the bot.py registration loop.
    * ``_dialog_callback_commands()`` returning a single CallbackQueryHandler
      for all callback_data belonging to this plugin (dlg:, cb:, dlg_cancel:).
    """

    CANCEL_WORDS: set = {"отмена", "отменить", "cancel", "выход", "exit", "-"}
    DIALOG_TIMEOUT: int = 300  # seconds

    # =====================================================================
    # State management
    # =====================================================================

    def _dialogs(self) -> Dict[int, DialogState]:
        # ToolPlugin.services is set by initialize(); rely on it for storage.
        services: Dict[str, Any] = getattr(self, "services", {})
        key = f"_dialog_mixin_{id(self)}"
        return services.setdefault(key, {})

    def start_dialog(self, chat_id: int, step: str, data: Optional[Dict[str, Any]] = None, user_id: int = 0) -> None:
        self._dialogs()[int(chat_id)] = DialogState(
            step=step,
            data=data or {},
            user_id=user_id,
            started_at=time.time(),
        )

    def end_dialog(self, chat_id: int) -> None:
        self._dialogs().pop(int(chat_id), None)

    def get_dialog(self, chat_id: int) -> Optional[DialogState]:
        state = self._dialogs().get(int(chat_id))
        if state is None:
            return None
        if time.time() - state.started_at > self.DIALOG_TIMEOUT:
            self._dialogs().pop(int(chat_id), None)
            return None
        return state

    def set_step(self, chat_id: int, step: str, data: Optional[Dict[str, Any]] = None) -> None:
        state = self.get_dialog(chat_id)
        if state is None:
            return
        state.step = step
        if data is not None:
            state.data.update(data)
        # Reset timeout on step change.
        state.started_at = time.time()

    # =====================================================================
    # Helpers — agent state, cancel words, button builders
    # =====================================================================

    def _ensure_agent_enabled(self, context: Any) -> bool:
        """Check whether the agent is currently active.

        Uses ``context.application.bot_data["bot_app"]`` to reach the
        session manager.  Returns ``False`` when the agent is off or the
        check fails for any reason.
        """
        try:
            bot_app = context.application.bot_data.get("bot_app")
            session = bot_app.manager.active() if bot_app else None
            return bool(session and getattr(session, "agent_enabled", False))
        except Exception:
            return False

    @classmethod
    def is_cancel_text(cls, text: str) -> bool:
        return (text or "").strip().lower() in cls.CANCEL_WORDS

    def _plugin_id_safe(self) -> str:
        return getattr(self, "get_plugin_id", lambda: type(self).__name__)()

    # -- cancel button ------------------------------------------------------

    def _cancel_callback_data(self) -> str:
        """Unique callback_data for this plugin's cancel button."""
        return f"dlg_cancel:{self._plugin_id_safe()}"

    def cancel_markup(self) -> Any:
        """Return an InlineKeyboardMarkup with a single 'Отмена' button.

        Use this in dialog invitation messages so the user can always
        cancel via a button tap in addition to text cancel words.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data=self._cancel_callback_data())]]
        )

    # -- dialog step buttons ------------------------------------------------

    def dialog_button(self, label: str, data: str) -> Any:
        """Create a button whose callback_data is scoped to this plugin's dialog.

        The resulting ``callback_data`` has the form ``dlg:{plugin_id}:{data}``.
        When pressed, the ``"callback"`` handler of the **current dialog step**
        receives the update, and the ``data`` part is available via
        ``parse_callback_payload(update)``.

        Example::

            keyboard = InlineKeyboardMarkup([
                [self.dialog_button("High", "high"),
                 self.dialog_button("Low", "low")],
                [InlineKeyboardButton("Отмена", callback_data=self._cancel_callback_data())],
            ])
        """
        from telegram import InlineKeyboardButton
        return InlineKeyboardButton(label, callback_data=f"dlg:{self._plugin_id_safe()}:{data}")

    # -- autonomous action buttons ------------------------------------------

    def action_button(self, label: str, action: str, payload: str = "") -> Any:
        """Create a button for an autonomous callback handler (outside dialog).

        The resulting ``callback_data`` has the form
        ``cb:{plugin_id}:{action}`` or ``cb:{plugin_id}:{action}:{payload}``.

        When pressed, ``callback_handlers()[action]`` is called with
        ``(update, context, payload)``.
        """
        from telegram import InlineKeyboardButton
        cbd = f"cb:{self._plugin_id_safe()}:{action}"
        if payload:
            cbd += f":{payload}"
        return InlineKeyboardButton(label, callback_data=cbd)

    @staticmethod
    def parse_callback_payload(update: Any) -> str:
        """Extract the payload portion from a dialog/action button callback_data.

        For ``dlg:{pid}:{payload}`` returns ``payload``.
        For ``cb:{pid}:{action}:{payload}`` returns ``payload``.
        If no payload, returns ``""``.
        """
        query = getattr(update, "callback_query", None)
        if not query or not query.data:
            return ""
        parts = query.data.split(":", 3)
        # dlg:{pid}:{payload}
        if parts[0] == "dlg" and len(parts) >= 3:
            return parts[2]
        # cb:{pid}:{action}:{payload}
        if parts[0] == "cb" and len(parts) >= 4:
            return parts[3]
        return ""

    # =====================================================================
    # ToolPlugin protocol overrides
    # =====================================================================

    def awaiting_input(self, chat_id: int) -> bool:  # type: ignore[override]
        return self.get_dialog(chat_id) is not None

    def cancel_input(self, chat_id: int) -> bool:  # type: ignore[override]
        if self.get_dialog(chat_id) is not None:
            self.end_dialog(chat_id)
            return True
        return False

    # =====================================================================
    # Contract for subclasses
    # =====================================================================

    def dialog_steps(self) -> Dict[str, StepHandler]:
        """Return a mapping of step name -> handler.

        The handler can be:
        * A plain ``async callable(update, context)`` — handles text/media
          messages only (backward compatible).
        * A ``dict`` with optional keys ``"message"`` and ``"callback"``:
          - ``"message"``: ``async callable(update, context)`` for text/media.
          - ``"callback"``: ``async callable(update, context)`` for inline
            button presses (CallbackQuery).

        Example::

            def dialog_steps(self):
                return {
                    "choose_priority": {
                        "message": self._on_priority_text,
                        "callback": self._on_priority_button,
                    },
                    "wait_text": self._on_text,  # plain callable
                }
        """
        return {}

    def callback_handlers(self) -> Dict[str, Callable]:
        """Return a mapping of action -> async handler for autonomous buttons.

        These handle ``cb:{plugin_id}:{action}:{payload}`` callback_data
        and work **outside** of active dialogs (e.g. inline menus).

        Handler signature: ``async def handler(update, context, payload: str)``

        Example::

            def callback_handlers(self):
                return {
                    "refresh": self._on_refresh,
                    "view": self._on_view,
                    "del": self._on_delete,
                }
        """
        return {}

    def step_hint(self, step: str) -> Optional[str]:
        """Optional hint shown when the user sends an unexpected content type.

        Override to return a short message like "Жду изображение."
        for a given step.  Return ``None`` (default) for no hint.
        """
        return None

    # =====================================================================
    # Step handler resolution (supports callable and dict)
    # =====================================================================

    def _resolve_step_handler(self, step: str, kind: str = "message") -> Optional[Callable]:
        """Resolve a handler for the given step and kind ("message" or "callback").

        Returns None if the step is unknown or the requested kind is not provided.
        """
        entry = self.dialog_steps().get(step)
        if entry is None:
            return None
        if callable(entry):
            return entry if kind == "message" else None
        if isinstance(entry, dict):
            h = entry.get(kind)
            return h if callable(h) else None
        return None

    # =====================================================================
    # Unified message handler (text / media)
    # =====================================================================

    async def handle_message(self, update: Any, context: Any) -> bool:
        """Unified entry point called by the bot for every text message
        while the dialog is active.

        Returns ``True`` if the message was consumed by the dialog,
        ``False`` if there is no active dialog (message should propagate).

        Flow:
        1. Check for active dialog (with timeout).
        2. Check for cancel words -> end dialog.
        3. Dispatch to the handler registered for the current step.
        """
        from telegram import Update as _Update
        msg = update.effective_message if isinstance(update, _Update) else None
        chat_id = update.effective_chat.id if isinstance(update, _Update) and update.effective_chat else 0
        if not chat_id:
            return False

        state = self.get_dialog(chat_id)
        if state is None:
            return False

        # Agent-enabled guard: auto-cancel dialog if agent was turned off.
        if not self._ensure_agent_enabled(context):
            self.end_dialog(chat_id)
            if msg:
                await msg.reply_text("Агент не активен. Диалог отменён.")
            return True

        text = (msg.text or "") if msg else ""
        if self.is_cancel_text(text):
            self.end_dialog(chat_id)
            if msg:
                await msg.reply_text("Отменено.")
            return True

        handler = self._resolve_step_handler(state.step, "message")
        if handler is None:
            # Step exists but has no message handler — check if it's a known step at all.
            entry = self.dialog_steps().get(state.step)
            if entry is None:
                logging.warning("DialogMixin: unknown step %r for %s", state.step, type(self).__name__)
                self.end_dialog(chat_id)
                if msg:
                    await msg.reply_text("Диалог сброшен (неизвестный шаг).")
            else:
                # Step exists but only has callback handler — show hint.
                hint = self.step_hint(state.step)
                if hint and msg:
                    await msg.reply_text(hint)
            return True

        try:
            await handler(update, context)
        except Exception:
            logging.exception("DialogMixin: error in step %r for %s", state.step, type(self).__name__)
            self.end_dialog(chat_id)
            if msg:
                await msg.reply_text("Ошибка в диалоге, попробуйте заново.")
        return True

    # =====================================================================
    # Unified callback handler (inline buttons)
    # =====================================================================

    async def handle_callback(self, update: Any, context: Any) -> None:
        """Unified entry point for dialog step callback buttons.

        Called when a ``dlg:{plugin_id}:...`` button is pressed while a
        dialog is active.  Dispatches to the ``"callback"`` handler of the
        current step.
        """
        query = getattr(update, "callback_query", None)
        if not query:
            return
        chat_id = query.message.chat_id if query.message else 0
        if not chat_id:
            return

        state = self.get_dialog(chat_id)
        if state is None:
            try:
                await query.answer("Диалог не активен.", show_alert=True)
            except Exception:
                pass
            return

        handler = self._resolve_step_handler(state.step, "callback")
        if handler is None:
            try:
                await query.answer()
            except Exception:
                pass
            return

        try:
            await query.answer()
        except Exception:
            pass

        try:
            await handler(update, context)
        except Exception:
            logging.exception("DialogMixin: error in callback step %r for %s", state.step, type(self).__name__)
            self.end_dialog(chat_id)
            if query.message:
                await query.message.reply_text("Ошибка в диалоге, попробуйте заново.")

    # =====================================================================
    # Central callback dispatcher (dlg: / cb: / dlg_cancel:)
    # =====================================================================

    async def _dispatch_callback(self, update: Any, context: Any) -> None:
        """Single entry point registered by ``_dialog_callback_commands``.

        Parses ``callback_data`` and routes to the appropriate handler:
        - ``dlg_cancel:{pid}`` -> ``_on_cancel_button``
        - ``dlg:{pid}:...`` -> ``handle_callback`` (dialog step buttons)
        - ``cb:{pid}:{action}:...`` -> ``callback_handlers()[action]``
        """
        query = getattr(update, "callback_query", None)
        if not query or not query.data:
            return
        data = query.data

        # 0. Agent-enabled guard: silently reject if agent is off.
        if not self._ensure_agent_enabled(context):
            try:
                await query.answer("Агент не активен.", show_alert=True)
            except Exception:
                pass
            return

        # 1. Cancel button.
        if data.startswith("dlg_cancel:"):
            await self._on_cancel_button(update, context)
            return

        # 2. Dialog step button: dlg:{pid}:{payload}
        if data.startswith("dlg:"):
            await self.handle_callback(update, context)
            return

        # 3. Autonomous action button: cb:{pid}:{action}[:payload]
        if data.startswith("cb:"):
            parts = data.split(":", 3)
            # parts: ["cb", pid, action] or ["cb", pid, action, payload]
            action = parts[2] if len(parts) >= 3 else ""
            payload = parts[3] if len(parts) >= 4 else ""
            handlers = self.callback_handlers()
            handler = handlers.get(action)
            if handler is None:
                logging.warning("DialogMixin: unknown action %r for %s", action, type(self).__name__)
                try:
                    await query.answer("Неизвестное действие.", show_alert=True)
                except Exception:
                    pass
                return
            try:
                await query.answer()
            except Exception:
                pass
            try:
                await handler(update, context, payload)
            except Exception:
                logging.exception("DialogMixin: error in callback action %r for %s", action, type(self).__name__)
            return

    async def _on_cancel_button(self, update: Any, context: Any) -> None:
        """Callback handler for the cancel button produced by ``cancel_markup``."""
        query = getattr(update, "callback_query", None)
        if not query:
            return
        try:
            await query.answer()
        except Exception:
            pass
        chat_id = query.message.chat_id if query and query.message else None
        if chat_id:
            self.end_dialog(chat_id)
        if query and query.message:
            await query.message.reply_text("Отменено.")

    # =====================================================================
    # Registration: single CallbackQueryHandler for the plugin
    # =====================================================================

    def _dialog_callback_commands(self) -> List[Dict[str, Any]]:
        """Return a single callback-query command entry that handles **all**
        callback_data for this plugin: ``dlg_cancel:``, ``dlg:``, ``cb:``.

        Add the result to your ``get_commands()``::

            def get_commands(self):
                return [...] + self._dialog_callback_commands()
        """
        pid = re.escape(self._plugin_id_safe())
        pattern = f"^(dlg_cancel|dlg|cb):{pid}(:|$)"
        return [
            {
                "callback_query_handler": self._dispatch_callback,
                "callback_pattern": pattern,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
        ]

    # Backward compatibility alias.
    _base_cancel_commands = _dialog_callback_commands

    # =====================================================================
    # Default get_message_handlers
    # =====================================================================

    def _dialog_active_filter(self) -> Any:
        """Build a telegram.ext filter that matches only when this plugin
        has an active dialog for the message's chat_id."""
        from telegram.ext import filters as _filters

        mixin = self

        class _ActiveFilter(_filters.MessageFilter):
            def filter(self, message: Any) -> bool:
                try:
                    chat_id = getattr(message, "chat_id", None)
                    return bool(chat_id and mixin.awaiting_input(int(chat_id)))
                except Exception:
                    return False

        return _ActiveFilter()

    def get_message_handlers(self) -> List[Dict[str, Any]]:  # type: ignore[override]
        """Default implementation: returns a dict-config list for bot.py
        registration loop.  Handles TEXT and (optionally) PHOTO/DOCUMENT
        via ``extra_message_filters``.
        """
        from telegram.ext import filters as _filters

        active = self._dialog_active_filter()
        base_filter = active & ~_filters.COMMAND

        handlers: List[Dict[str, Any]] = []

        # Primary handler for text + any extra filters.
        combined = base_filter & (_filters.TEXT | self.extra_message_filters())
        handlers.append({
            "filters": combined,
            "handler": self.handle_message,
        })

        return handlers

    def extra_message_filters(self) -> Any:
        """Override to add extra content-type filters (e.g. PHOTO).

        The returned filter is OR-combined with ``filters.TEXT``, so the
        ``handle_message`` entry-point will also be called for matching
        non-text updates.
        """
        from telegram.ext import filters as _filters
        # By default, match nothing extra (empty UpdateFilter).

        class _NothingFilter(_filters.BaseFilter):
            def filter(self, message: Any) -> bool:
                return False

        return _NothingFilter()
