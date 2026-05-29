from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional, Tuple

# Ensure repo root is on sys.path when running as desktop/main.py
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from qasync import QEventLoop  # noqa: E402

from desktop.services.application_facade import ApplicationFacade  # noqa: E402
from app.services.config_service import ConfigService, FileConfigProvider  # noqa: E402
from desktop.services.desktop_git_service import DesktopGitService  # noqa: E402
from desktop.services.desktop_state_service import DesktopUiStateService  # noqa: E402
from app.services.session_service import SessionService  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402
from desktop.services.theme_service import ThemeService  # noqa: E402
from modes.sdk.services.mode_registry import ModeRegistryService  # noqa: E402
from session import SessionManager  # noqa: E402

from desktop.main_window import MainWindow  # noqa: E402


REPO_ROOT = _REPO_ROOT
CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")


async def bootstrap_facade(
    *,
    config_path: str = CONFIG_PATH,
    logger: Optional[logging.Logger] = None,
) -> Tuple[ApplicationFacade, DesktopUiStateService]:
    """
    Desktop bootstrap in required order:
    1) Registry
    2) Services
    3) Facade
    4) UI state service (then UI)
    """
    log = logger or logging.getLogger(__name__)

    # 1) Registry
    mode_registry_service = ModeRegistryService()
    mode_registry_service.load_modes()

    # 2) Services
    config_provider = FileConfigProvider(str(config_path))
    config_service = ConfigService(config_provider)
    cfg = await config_service.load()

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    git_service = DesktopGitService()
    theme_service = ThemeService(logger=log)

    # 3) Facade
    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        git_service=git_service,  # type: ignore[arg-type]
        theme_service=theme_service,
        mode_registry_service=mode_registry_service,
        ui_state_service=None,
        logger=log,
    )

    # 4) UI
    ui_state_service = DesktopUiStateService(facade=facade, logger=log)
    facade.ui_state_service = ui_state_service

    return facade, ui_state_service


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger("desktop")

    facade, ui_state_service = await bootstrap_facade(config_path=CONFIG_PATH, logger=logger)

    # Start facade (loads config/runtime params and triggers UI state load on startup:ready).
    await facade.start(validate_secrets=False)
    await ui_state_service.wait_ready()

    window = MainWindow(facade, ui_state_service, logger=logger)
    window.show()

    # Keep async main alive while the window exists.
    while window.isVisible():
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    # Enable High-DPI scaling support
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Gemini CLI")
    app.setApplicationVersion("1.0.0")

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        loop.run_until_complete(main())
