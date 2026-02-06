import logging
import os


def setup_logging(log_path: str) -> None:
    """
    Configure rotating log files:
    - main log_path (INFO+)
    - <base>_error.log (ERROR+)
    - <base>_agent.log (agent.* loggers only)
    """
    import datetime as _dt
    import sys
    import threading
    from logging.handlers import TimedRotatingFileHandler

    log_dir = os.path.dirname(log_path)
    log_base = os.path.basename(log_path)
    base_root, base_ext = os.path.splitext(log_base)
    if base_root:
        error_log_name = f"{base_root}_error{base_ext or '.log'}"
        agent_log_name = f"{base_root}_agent{base_ext or '.log'}"
    else:
        error_log_name = "bot_error.log"
        agent_log_name = "agent.log"

    error_log_path = os.path.join(log_dir, error_log_name)
    agent_log_path = os.path.join(log_dir, agent_log_name)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO)

    handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=_dt.time(3, 0),
        encoding="utf-8",
    )

    def _namer(_default_name: str) -> str:
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
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)

    error_handler = TimedRotatingFileHandler(
        error_log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=_dt.time(3, 0),
        encoding="utf-8",
    )

    def _error_namer(_default_name: str) -> str:
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
    error_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(error_handler)

    # Dedicated agent log file (agent.orchestrator / agent.planner / ...)
    agent_handler = TimedRotatingFileHandler(
        agent_log_path,
        when="midnight",
        interval=1,
        backupCount=1,
        utc=True,
        atTime=_dt.time(3, 0),
        encoding="utf-8",
    )

    def _agent_namer(_default_name: str) -> str:
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
    agent_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    agent_logger = logging.getLogger("agent")
    agent_logger.addHandler(agent_handler)
    agent_logger.propagate = False

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

