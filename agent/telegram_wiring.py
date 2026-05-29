import asyncio
import logging

from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from modes.sdk.runtime.profiles import build_default_profile
from modes.sdk.runtime.tooling.registry import get_tool_registry


def _policy_allows_chat(bot_app, chat_id: int) -> bool:
    return bool(bot_app.access_policy_service.is_allowed(int(chat_id)))


def install_plugin_handlers(app, bot_app, config, core_command_names: set[str]) -> None:
    """Register plugin-provided Telegram handlers into the application."""
    try:
        tool_registry = get_tool_registry(config)
        bot_app._tool_registry = tool_registry
        profile = build_default_profile(config, tool_registry)
        runtime = None
        getter = getattr(bot_app, "get_runtime_by_capability", None)
        if callable(getter):
            runtime = getter("plugin_ui")
        if runtime is None:
            return
        ui = runtime.get_plugin_ui(profile)
        message_handlers = ui.get("message_handlers") or []
        inline_handlers = ui.get("inline_handlers") or []

        for cfg in inline_handlers:
            pattern = cfg.get("pattern")
            handler_fn = cfg.get("handler")
            kwargs = cfg.get("handler_kwargs") or {}
            if not isinstance(pattern, str) or not pattern or not callable(handler_fn):
                continue

            async def _inline_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, _fn=handler_fn, _kw=kwargs) -> None:
                chat_id = update.effective_chat.id
                if not _policy_allows_chat(bot_app, chat_id):
                    return
                try:
                    res = _fn(update, context, **(_kw or {}))
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
            app.add_handler(CallbackQueryHandler(_inline_wrap, pattern=pattern))

        _PLUGIN_GROUP = -1

        class _ModePluginEnabledFilter(filters.MessageFilter):
            def filter(self, message) -> bool:
                chat_id = getattr(getattr(message, "chat", None), "id", None)
                if not chat_id:
                    return False
                thread_id = getattr(message, "message_thread_id", None)
                resolver = getattr(bot_app, "resolve_telegram_scope_session", None)
                session = (
                    resolver(reply_chat_id=int(chat_id), message_thread_id=thread_id, owner_chat_id=int(chat_id))
                    if callable(resolver)
                    else None
                )
                return bool(bot_app._mode_allows_plugin_ui(session))

        _agent_filter = _ModePluginEnabledFilter()

        for cfg in message_handlers:
            if "filters" not in cfg:
                continue
            filter_obj = cfg.get("filters")
            handler_fn = cfg.get("handler")
            if not callable(handler_fn):
                continue
            kwargs = cfg.get("handler_kwargs") or {}

            async def _msg_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, _fn=handler_fn, _kw=kwargs) -> None:
                chat_id = update.effective_chat.id if update.effective_chat else None
                if not chat_id or not _policy_allows_chat(bot_app, chat_id):
                    return
                handled = False
                try:
                    res = _fn(update, context, **(_kw or {}))
                    if asyncio.iscoroutine(res):
                        handled = await res
                    else:
                        handled = res
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                if handled:
                    raise ApplicationHandlerStop()

            app.add_handler(MessageHandler(_agent_filter & filter_obj, _msg_wrap), group=_PLUGIN_GROUP)

        _ = core_command_names
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
