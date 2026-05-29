import datetime as dt
import contextvars
import logging
import os
import sys
import threading
from contextlib import contextmanager
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, Iterator

from session import session_runtime_uid


class LogBusHandler(logging.Handler):
    """Обработчик логов, пересылающий записи в шину (например, для UI)."""

    def __init__(self, bus):
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        self.bus.emit(record)


_LOG_CONTEXT: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar("log_context", default={})


def _normalize_field(value: Any, *, fallback: str = "-", max_len: int = 256) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = text.replace("\r", " ").replace("\n", " ").replace("]", ")")
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def build_session_log_context(*, session: Any | None = None, chat_id: Any | None = None) -> Dict[str, str]:
    sid = _normalize_field(getattr(session, "id", ""), fallback="-", max_len=128)
    scope = getattr(session, "conversation_scope", None)
    scope_chat_id = getattr(scope, "chat_id", None)
    cid_source = chat_id if chat_id is not None else scope_chat_id
    cid = _normalize_field(cid_source, fallback="-", max_len=64)
    suid = _normalize_field(session_runtime_uid(session), fallback="-", max_len=256)
    sname = _normalize_field(getattr(session, "name", ""), fallback="-", max_len=200)
    return {
        "chat_id": cid,
        "session_id": sid,
        "session_uid": suid,
        "session_name": sname,
    }


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    current = dict(_LOG_CONTEXT.get() or {})
    updated = dict(current)
    for key in ("chat_id", "session_id", "session_uid", "session_name"):
        if key in fields:
            updated[key] = _normalize_field(fields.get(key))
    token = _LOG_CONTEXT.set(updated)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def bind_session_log_context(*, session: Any | None = None, chat_id: Any | None = None):
    return bind_log_context(**build_session_log_context(session=session, chat_id=chat_id))


class LogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = dict(_LOG_CONTEXT.get() or {})
        chat_id = _normalize_field(getattr(record, "chat_id", ctx.get("chat_id")))
        session_id = _normalize_field(getattr(record, "session_id", ctx.get("session_id")))
        session_uid = _normalize_field(getattr(record, "session_uid", ctx.get("session_uid")))
        session_name = _normalize_field(getattr(record, "session_name", ctx.get("session_name")))

        record.chat_id = chat_id
        record.session_id = session_id
        record.session_uid = session_uid
        record.session_name = session_name
        return True


def resolve_log_paths(log_path: str) -> Dict[str, str]:
    log_dir = os.path.dirname(log_path)
    log_base = os.path.basename(log_path)
    base_root, base_ext = os.path.splitext(log_base)

    if base_root:
        error_log_name = f"{base_root}_error{base_ext or '.log'}"
        agent_log_name = f"{base_root}_agent{base_ext or '.log'}"
        cli_dialog_log_name = f"{base_root}_cli_dialog{base_ext or '.log'}"
        miniapp_log_name = f"{base_root}_miniapp{base_ext or '.log'}"
    else:
        error_log_name = "bot_error.log"
        agent_log_name = "agent.log"
        cli_dialog_log_name = "bot_cli_dialog.log"
        miniapp_log_name = "miniapp.log"

    return {
        "main": log_path,
        "error": os.path.join(log_dir, error_log_name),
        "agent": os.path.join(log_dir, agent_log_name),
        "cli_dialog": os.path.join(log_dir, cli_dialog_log_name),
        "miniapp": os.path.join(log_dir, miniapp_log_name),
    }


def register_log_bus(bus) -> None:
    """Регистрирует шину логов в корневом логгере и логгерах без пропагации."""
    handler = LogBusHandler(bus)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(LogContextFilter())

    # Корень
    logging.getLogger().addHandler(handler)

    # Логгеры с propagate=False из setup_logging
    for name in ["agent", "bot.cli_dialog", "miniapp"]:
        logging.getLogger(name).addHandler(handler)


def _remove_and_close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def setup_logging(config) -> None:
    log_path = config.defaults.log_path
    paths = resolve_log_paths(log_path)
    error_log_path = paths["error"]
    agent_log_path = paths["agent"]
    cli_dialog_log_path = paths["cli_dialog"]
    miniapp_log_path = paths["miniapp"]
    default_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] [sid=%(session_id)s] [suid=%(session_uid)s] %(message)s"
    )
    context_filter = LogContextFilter()

    root = logging.getLogger()
    _remove_and_close_handlers(root)
    root.setLevel(logging.INFO)

    handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=dt.time(3, 0),
        encoding="utf-8",
    )

    def _namer(default_name: str) -> str:
        return f"{log_path}.1"

    def _rotator(source: str, dest: str) -> None:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        os.replace(source, dest)

    handler.namer = _namer
    handler.rotator = _rotator
    handler.setFormatter(default_formatter)
    handler.addFilter(context_filter)
    root.addHandler(handler)

    error_handler = TimedRotatingFileHandler(
        error_log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=dt.time(3, 0),
        encoding="utf-8",
    )

    def _error_namer(default_name: str) -> str:
        return f"{error_log_path}.1"

    def _error_rotator(source: str, dest: str) -> None:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        os.replace(source, dest)

    error_handler.namer = _error_namer
    error_handler.rotator = _error_rotator
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(default_formatter)
    error_handler.addFilter(context_filter)
    root.addHandler(error_handler)

    agent_handler = TimedRotatingFileHandler(
        agent_log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=dt.time(3, 0),
        encoding="utf-8",
    )

    def _agent_namer(default_name: str) -> str:
        return f"{agent_log_path}.1"

    def _agent_rotator(source: str, dest: str) -> None:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        os.replace(source, dest)

    agent_handler.namer = _agent_namer
    agent_handler.rotator = _agent_rotator
    agent_handler.setFormatter(default_formatter)
    agent_handler.addFilter(context_filter)
    agent_logger = logging.getLogger("agent")
    _remove_and_close_handlers(agent_logger)
    agent_logger.addHandler(agent_handler)
    agent_logger.propagate = False

    cli_dialog_handler = TimedRotatingFileHandler(
        cli_dialog_log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=dt.time(3, 0),
        encoding="utf-8",
    )

    def _cli_dialog_namer(default_name: str) -> str:
        return f"{cli_dialog_log_path}.1"

    def _cli_dialog_rotator(source: str, dest: str) -> None:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        os.replace(source, dest)

    cli_dialog_handler.namer = _cli_dialog_namer
    cli_dialog_handler.rotator = _cli_dialog_rotator
    cli_dialog_handler.setFormatter(default_formatter)
    cli_dialog_handler.addFilter(context_filter)
    cli_dialog_logger = logging.getLogger("bot.cli_dialog")
    _remove_and_close_handlers(cli_dialog_logger)
    cli_dialog_logger.setLevel(logging.INFO)
    cli_dialog_logger.addHandler(cli_dialog_handler)
    cli_dialog_logger.propagate = False

    miniapp_handler = TimedRotatingFileHandler(
        miniapp_log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=dt.time(3, 0),
        encoding="utf-8",
    )

    def _miniapp_namer(default_name: str) -> str:
        return f"{miniapp_log_path}.1"

    def _miniapp_rotator(source: str, dest: str) -> None:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        os.replace(source, dest)

    miniapp_handler.namer = _miniapp_namer
    miniapp_handler.rotator = _miniapp_rotator
    miniapp_handler.setFormatter(default_formatter)
    miniapp_handler.addFilter(context_filter)
    miniapp_logger = logging.getLogger("miniapp")
    _remove_and_close_handlers(miniapp_logger)
    miniapp_logger.setLevel(logging.INFO)
    miniapp_logger.addHandler(miniapp_handler)
    miniapp_logger.propagate = False

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    prev_excepthook = sys.excepthook
    prev_threading_excepthook = threading.excepthook

    def _log_unhandled_exception(exc_type, exc_value, exc_traceback):
        logging.getLogger().error("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
        if prev_excepthook and prev_excepthook is not sys.__excepthook__:
            prev_excepthook(exc_type, exc_value, exc_traceback)

    def _log_thread_exception(args):
        logging.getLogger().error(
            "Unhandled thread exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        if prev_threading_excepthook and prev_threading_excepthook is not threading.__excepthook__:
            prev_threading_excepthook(args)

    sys.excepthook = _log_unhandled_exception
    threading.excepthook = _log_thread_exception
