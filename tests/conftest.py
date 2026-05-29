import os
import sys
import weakref

import pytest

# Allow importing top-level modules (bot.py, config.py, etc.) from tests/.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# Track BotApp instances created during tests so we can shut down their
# async services (notification_queue_service, system_event_bus) before the
# event loop closes.  Without this, orphaned worker coroutines raise
# "RuntimeError: Event loop is closed" during garbage collection.
_tracked_bot_apps: list[weakref.ref] = []
_original_bot_app_init = None


def _patch_bot_app_init() -> None:
    global _original_bot_app_init
    if _original_bot_app_init is not None:
        return
    try:
        from bot import BotApp
    except Exception:
        return

    _original_bot_app_init = BotApp.__init__

    def _tracked_init(self, *args, **kwargs):
        _original_bot_app_init(self, *args, **kwargs)
        _tracked_bot_apps.append(weakref.ref(self))

    BotApp.__init__ = _tracked_init


_patch_bot_app_init()


def _shutdown_tracked_bot_apps() -> None:
    """Shut down async services on all BotApp instances created during the test."""
    while _tracked_bot_apps:
        ref = _tracked_bot_apps.pop()
        app = ref()
        if app is None:
            continue
        for svc_name in ("notification_queue_service", "system_event_bus"):
            svc = getattr(app, svc_name, None)
            if svc is None:
                continue
            # Cancel worker tasks directly instead of awaiting shutdown(),
            # since we may not have a running event loop at this point.
            workers = getattr(svc, "_workers", None)
            if isinstance(workers, dict):
                for task in workers.values():
                    try:
                        if hasattr(task, "cancel"):
                            task.cancel()
                    except RuntimeError:
                        pass  # Event loop already closed.
                workers.clear()
            worker_task = getattr(svc, "_worker_task", None)
            if worker_task is not None and hasattr(worker_task, "cancel"):
                try:
                    worker_task.cancel()
                except RuntimeError:
                    pass  # Event loop already closed.
                svc._worker_task = None
            # Clear queues to prevent dangling references.
            queues = getattr(svc, "_queues", None)
            if isinstance(queues, dict):
                queues.clear()
            queue = getattr(svc, "_queue", None)
            if queue is not None:
                svc._queue = None
            # Detach loop reference so GC doesn't try to use closed loop.
            if hasattr(svc, "_loop"):
                svc._loop = None
            if hasattr(svc, "_started"):
                svc._started = False


@pytest.fixture(autouse=True)
def _reset_singletons_between_tests():
    """Reset process-wide singletons and shut down BotApp async services."""
    yield
    _shutdown_tracked_bot_apps()
    # ToolRegistry singleton.
    try:
        import modes.sdk.runtime.tooling.registry as _reg
        _reg._REGISTRY_SINGLETON = None
    except Exception:
        pass
