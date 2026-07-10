import asyncio
import logging
from types import SimpleNamespace
from typing import Callable

from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

from agent.plugins.task_management import run_task_deadline_checker
from app.services.thread_mode_capability_checker import ThreadModeCapabilityChecker


def build_post_init(bot_app) -> Callable[[Application], asyncio.Future]:
    async def _post_init(application: Application) -> None:
        await ThreadModeCapabilityChecker(bot_app.config).ensure_supported(application.bot)
        await bot_app.set_bot_commands(application)
        await bot_app.mcp.start()
        session_thread_manager = getattr(bot_app, "session_thread_manager", None)
        if session_thread_manager is not None:
            await session_thread_manager.reconcile()
            await session_thread_manager.backfill_session_topics(bot=application.bot)
            await session_thread_manager.start_repair_job(bot=application.bot)
        notification_queue_service = getattr(bot_app, "notification_queue_service", None)
        if notification_queue_service is not None:
            await notification_queue_service.start()
        session_management = getattr(bot_app, "session_management", None)
        if session_management is not None:
            try:
                recovered = await session_management.recover_tmux_sessions(SimpleNamespace(bot=application.bot))
                if recovered:
                    logging.getLogger(__name__).info("restored %d active tmux request(s)", recovered)
            except Exception:
                logging.getLogger(__name__).exception("tmux startup reconciliation failed")
        scheduler_service = getattr(bot_app, "scheduler_service", None)
        if scheduler_service is not None:
            await scheduler_service.start()
        mode_launch_adapter = getattr(bot_app, "mode_launch_adapter", None)
        if mode_launch_adapter is not None:
            await mode_launch_adapter.start(application=application)
        webhook_ingress_service = getattr(bot_app, "webhook_ingress_service", None)
        if webhook_ingress_service is not None:
            await webhook_ingress_service.start()
        miniapp_server = getattr(bot_app, "miniapp_server", None)
        if miniapp_server is not None:
            await miniapp_server.start()
        shared_http_ingress = getattr(bot_app, "shared_http_ingress", None)
        if shared_http_ingress is not None:
            await shared_http_ingress.start()
        try:
            await bot_app.handlers.notify_pending_selfupdate(application)
        except Exception as e:
            logging.getLogger(__name__).exception("selfupdate startup notify failed: %s", e)
        if not bot_app._task_deadline_checker_task:
            bot_app._task_deadline_checker_task = asyncio.create_task(
                run_task_deadline_checker(application, bot_app.is_allowed),
                name="task_deadline_checker",
            )

    return _post_init


def build_post_shutdown(bot_app) -> Callable[[Application], asyncio.Future]:
    async def _post_shutdown(application: Application) -> None:
        if bot_app._task_deadline_checker_task:
            bot_app._task_deadline_checker_task.cancel()
            bot_app._task_deadline_checker_task = None
        try:
            session_thread_manager = getattr(bot_app, "session_thread_manager", None)
            if session_thread_manager is not None:
                await session_thread_manager.stop_repair_job()
        except Exception as e:
            logging.getLogger(__name__).exception("session thread repair stop failed: %s", e)
        try:
            await bot_app.shutdown_runtime()
        except Exception as e:
            logging.getLogger(__name__).exception("runtime shutdown failed: %s", e)
        try:
            await bot_app.mcp.stop()
        except Exception as e:
            logging.getLogger(__name__).exception("mcp stop failed: %s", e)
        try:
            scheduler_service = getattr(bot_app, "scheduler_service", None)
            if scheduler_service is not None:
                await scheduler_service.stop()
        except Exception as e:
            logging.getLogger(__name__).exception("scheduler stop failed: %s", e)
        try:
            mode_launch_adapter = getattr(bot_app, "mode_launch_adapter", None)
            if mode_launch_adapter is not None:
                await mode_launch_adapter.stop()
        except Exception as e:
            logging.getLogger(__name__).exception("mode launch adapter stop failed: %s", e)
        try:
            webhook_ingress_service = getattr(bot_app, "webhook_ingress_service", None)
            if webhook_ingress_service is not None:
                await webhook_ingress_service.stop()
        except Exception as e:
            logging.getLogger(__name__).exception("webhook ingress stop failed: %s", e)
        try:
            notification_queue_service = getattr(bot_app, "notification_queue_service", None)
            if notification_queue_service is not None:
                await notification_queue_service.shutdown()
        except Exception as e:
            logging.getLogger(__name__).exception("notification queue stop failed: %s", e)
        try:
            miniapp_server = getattr(bot_app, "miniapp_server", None)
            if miniapp_server is not None:
                await miniapp_server.stop()
        except Exception as e:
            logging.getLogger(__name__).exception("miniapp stop failed: %s", e)
        try:
            shared_http_ingress = getattr(bot_app, "shared_http_ingress", None)
            if shared_http_ingress is not None:
                await shared_http_ingress.stop()
        except Exception as e:
            logging.getLogger(__name__).exception("shared http ingress stop failed: %s", e)
        try:
            bot_app.shutdown_html_process_pool()
        except Exception as e:
            logging.getLogger(__name__).exception("html process pool shutdown failed: %s", e)

    return _post_shutdown


def build_error_handler() -> Callable[[object, ContextTypes.DEFAULT_TYPE], asyncio.Future]:
    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        msg = str(err)
        if (
            isinstance(err, (NetworkError, TimedOut))
            or "ConnectError" in msg
            or "NetworkError" in msg
            or "TimedOut" in msg
            or "Conflict" in msg
        ):
            logging.warning("Ошибка сети при отправке сообщения в Telegram: %s", msg)
            return
        if isinstance(err, BadRequest) or "Can't parse entities" in msg:
            logging.warning("Ошибка Telegram API (BadRequest): %s", msg)
            return
        logging.exception("Ошибка бота: %s", err)

    return _on_error
