from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from agent.telegram_wiring import install_plugin_handlers
from tg.command_policy import OUTSIDE_TOPIC_ALLOWED_COMMANDS
from tg.command_registry import build_command_registry


def _policy_allows_chat(bot_app, chat_id: int) -> bool:
    return bool(bot_app.access_policy_service.is_allowed(int(chat_id)))


def register_handlers(*, app: Application, bot_app, config) -> None:
    app.bot_data["bot_app"] = bot_app
    app.add_handler(MessageHandler(filters.COMMAND, bot_app.on_pre_command), group=-2)

    core_registry = build_command_registry(bot_app)
    core_command_names = {e["name"] for e in core_registry}
    for entry in core_registry:
        async def _wrap(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
            _handler=entry["handler"],
            _name=entry["name"],
        ) -> None:
            chat_id = update.effective_chat.id
            if _name != "start":
                route_authorizer = getattr(bot_app, "ensure_telegram_inbound_authorized", None)
                if callable(route_authorizer):
                    route = await route_authorizer(
                        update,
                        context,
                        allow_outside_topic=_name in OUTSIDE_TOPIC_ALLOWED_COMMANDS,
                    )
                    if route is None:
                        return
                elif not _policy_allows_chat(bot_app, chat_id):
                    return
            bot_app.metrics.inc("commands")
            await _handler(update, context)

        app.add_handler(CommandHandler(entry["name"], _wrap))

    install_plugin_handlers(
        app=app,
        bot_app=bot_app,
        config=config,
        core_command_names=core_command_names,
    )
    app.add_handler(CallbackQueryHandler(bot_app.on_callback))
    app.add_handler(MessageHandler(filters.COMMAND, bot_app.on_unknown_command))
    app.add_handler(MessageHandler(filters.PHOTO, bot_app.on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, bot_app.on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_app.on_message))
