from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, QByteArray, QSize
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QStackedWidget,
    QPushButton,
    QStatusBar,
    QFrame,
    QSplitter,
    QFileDialog,
    QToolButton,
    QStyle,
    QSizePolicy,
)

from desktop.widgets.session_manager import SessionManagerWidget
from desktop.widgets.session_settings import SessionSettingsWidget
from desktop.widgets.chat_view import ChatViewWidget
from desktop.widgets.log_viewer import LogViewerWidget
from desktop.widgets.files_panel import FilesPanelWidget
from desktop.widgets.git_panel import GitPanelWidget
from desktop.widgets.mode_panel import ModePanelWidget
from desktop.widgets.mode_menu import ModeMenuWidget
from desktop.widgets.manage_tasks_progress import format_manage_tasks_progress
from desktop.widgets.task_progress import TaskProgressWidget
from desktop.widgets.config_editor import ConfigEditorWidget
from desktop.widgets.report_viewer import ReportViewerWidget
from desktop.widgets.run_operations_panel import RunOperationsPanelWidget
from desktop.widgets.task_queue import TaskQueueWidget
from desktop.widgets.plugin_menu import PluginMenuWidget
from desktop.widgets.admin_panel import AdminPanel
from desktop.widgets.scheduler_panel import SchedulerPanelWidget
from desktop.widgets.status_panel import StatusPanelWidget
from desktop.widgets.command_palette import CommandPaletteDialog, CommandPaletteItem
from modes.sdk.services.tooling import ModeToolingService
from session import session_runtime_uid
from sessions.session_state_access import get_active_mode
from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade, AppNotification
    from desktop.services.desktop_state_service import DesktopUiStateService


class MainWindow(QMainWindow):
    """Главное окно приложения с навигацией."""

    _CLOSE_BACKGROUND_TIMEOUT_S = 1.0

    def __init__(
        self,
        facade: ApplicationFacade,
        ui_state_service: DesktopUiStateService,
        logger: Optional[logging.Logger] = None
    ):
        super().__init__()
        self.facade = facade
        self.ui_state_service = ui_state_service
        self.logger = logger or logging.getLogger(__name__)
        self._background_tasks = set()
        self._close_task = None
        self._close_finalized = False
        self._closing_in_progress = False
        self._active_session_uid: Optional[str] = None
        self._active_run_task = None
        self._pending_ask_by_session: dict[str, dict] = {}
        self._ask_option_parser = ModeToolingService()
        self._tab_widgets: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QPushButton] = {}
        self.nav_admin: Optional[QPushButton] = None
        self.admin_page: Optional[AdminPanel] = None

        self.setWindowTitle("Gemini CLI")
        self.setMinimumSize(1000, 700)

        self._setup_ui()
        self._restore_state()

        # Подписка на уведомления фасада
        self._facade_unsubscribe = self.facade.subscribe(self._on_facade_notification)

    def _setup_ui(self):
        """Инициализация каркаса UI."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QHBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Компактная боковая панель навигации
        self.sidebar = QFrame()
        self.sidebar.setFixedWidth(64)
        self.sidebar.setObjectName("sidebar")

        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(8, 10, 8, 10)
        sidebar_layout.setSpacing(6)

        self.nav_chat = QPushButton("")
        self.nav_chat.setToolTip("Chat")
        self.nav_config = QPushButton("")
        self.nav_config.setToolTip("Config")
        self.nav_files = QPushButton("")
        self.nav_files.setToolTip("Files")
        self.nav_reports = QPushButton("")
        self.nav_reports.setToolTip("Reports")
        self.nav_logs = QPushButton("")
        self.nav_logs.setToolTip("Logs")
        self.nav_status = QPushButton("")
        self.nav_status.setToolTip("Status")
        self.nav_scheduler = QPushButton("")
        self.nav_scheduler.setToolTip("Scheduler")
        self.nav_session_settings = QPushButton("")
        self.nav_session_settings.setToolTip("Settings")
        self.nav_plugins = QPushButton("")
        self.nav_plugins.setToolTip("Plugins")
        admin_tab_enabled = self._is_admin_tab_enabled()
        if admin_tab_enabled:
            self.nav_admin = QPushButton("")
            self.nav_admin.setToolTip("Admin")

        self._nav_buttons = {
            "chat": self.nav_chat,
            "settings": self.nav_config,
            "files": self.nav_files,
            "logs": self.nav_logs,
            "status": self.nav_status,
            "scheduler": self.nav_scheduler,
            "session_settings": self.nav_session_settings,
            "reports": self.nav_reports,
            "plugins": self.nav_plugins,
        }
        if self.nav_admin is not None:
            self._nav_buttons["admin"] = self.nav_admin
        for btn in self._nav_buttons.values():
            btn.setCheckable(True)
            btn.setFixedSize(40, 40)
            btn.setIconSize(QSize(18, 18))
            sidebar_layout.addWidget(btn)
        self._apply_sidebar_icons()

        sidebar_layout.addStretch()
        layout.addWidget(self.sidebar)

        # Основная область контента
        self.content_stack = QStackedWidget()

        # Страница чата с менеджером сессий
        self.chat_page = QWidget()
        chat_layout = QVBoxLayout(self.chat_page)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        # Левая часть: Менеджер сессий
        self.session_manager = SessionManagerWidget(self.facade)
        self.session_manager.sessionSelected.connect(self._on_session_selected)

        # Верхняя контекстная полоска (быстрые действия)
        self.top_context = QFrame()
        self.top_context.setObjectName("top_context_strip")
        self.top_context.setMinimumHeight(42)
        self.top_context.setMaximumHeight(42)
        self.top_context.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_context_layout = QHBoxLayout(self.top_context)
        top_context_layout.setContentsMargins(8, 6, 8, 6)
        top_context_layout.setSpacing(6)

        self.toggle_sessions_btn = QToolButton()
        self.toggle_sessions_btn.setText("Sessions")
        self.toggle_sessions_btn.setObjectName("top_context_button")
        self.toggle_sessions_btn.setCheckable(True)
        self.toggle_sessions_btn.setChecked(True)
        self.toggle_sessions_btn.setMinimumHeight(30)
        self.toggle_sessions_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle_sessions_btn.clicked.connect(lambda checked=False: self._toggle_session_panel())
        top_context_layout.addWidget(self.toggle_sessions_btn)

        self.toggle_git_btn = QToolButton()
        self.toggle_git_btn.setText("Git")
        self.toggle_git_btn.setObjectName("top_context_button")
        self.toggle_git_btn.setCheckable(True)
        self.toggle_git_btn.setMinimumHeight(30)
        self.toggle_git_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle_git_btn.clicked.connect(lambda checked: self._toggle_context_panel("git", checked))
        top_context_layout.addWidget(self.toggle_git_btn)

        self.toggle_tasks_btn = QToolButton()
        self.toggle_tasks_btn.setText("Tasks")
        self.toggle_tasks_btn.setObjectName("top_context_button")
        self.toggle_tasks_btn.setCheckable(True)
        self.toggle_tasks_btn.setMinimumHeight(30)
        self.toggle_tasks_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle_tasks_btn.clicked.connect(lambda checked: self._toggle_context_panel("tasks", checked))
        top_context_layout.addWidget(self.toggle_tasks_btn)

        self.toggle_runs_btn = QToolButton()
        self.toggle_runs_btn.setText("Runs")
        self.toggle_runs_btn.setObjectName("top_context_button")
        self.toggle_runs_btn.setCheckable(True)
        self.toggle_runs_btn.setMinimumHeight(30)
        self.toggle_runs_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle_runs_btn.clicked.connect(lambda checked: self._toggle_context_panel("runs", checked))
        top_context_layout.addWidget(self.toggle_runs_btn)

        self.toggle_session_settings_btn = QToolButton()
        self.toggle_session_settings_btn.setText("Session")
        self.toggle_session_settings_btn.setObjectName("top_context_button")
        self.toggle_session_settings_btn.setCheckable(True)
        self.toggle_session_settings_btn.setMinimumHeight(30)
        self.toggle_session_settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle_session_settings_btn.clicked.connect(lambda checked: self._toggle_context_panel("session", checked))
        top_context_layout.addWidget(self.toggle_session_settings_btn)

        self.open_palette_btn = QToolButton()
        self.open_palette_btn.setText("Cmd")
        self.open_palette_btn.setObjectName("top_context_button")
        self.open_palette_btn.setMinimumHeight(30)
        self.open_palette_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.open_palette_btn.clicked.connect(self._open_command_palette)
        top_context_layout.addWidget(self.open_palette_btn)
        top_context_layout.addStretch(1)

        chat_layout.addWidget(self.top_context, 0)

        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Центральная часть: Чат
        self.chat_container = QWidget()
        chat_v_layout = QVBoxLayout(self.chat_container)
        chat_v_layout.setContentsMargins(0, 0, 0, 0)
        chat_v_layout.setSpacing(0)

        # Панель режима
        self.mode_panel = ModePanelWidget(self.facade, )
        chat_v_layout.addWidget(self.mode_panel)

        # Панель прогресса задач
        self.task_progress = TaskProgressWidget(self.facade)
        chat_v_layout.addWidget(self.task_progress)

        # Панель меню режима (Telegram-style inline keyboard renderer)
        self.mode_menu = ModeMenuWidget(self.facade, )
        chat_v_layout.addWidget(self.mode_menu)

        # Разделитель под панелью режима
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setObjectName("mode_separator")
        line.setVisible(False)
        self.mode_menu_separator = line
        chat_v_layout.addWidget(line)

        self.chat_view = ChatViewWidget()
        self.chat_view.messageSentWithAttachments.connect(self._on_message_sent)
        self.chat_view.taskCancelled.connect(self._on_task_cancelled)
        self.chat_view.askOptionSelected.connect(self._on_ask_option_selected)
        self.chat_view.setEnabled(False)  # Disabled until session is selected
        chat_v_layout.addWidget(self.chat_view)
        # В тестах ModeMenuWidget может быть пропатчен до обычного QWidget без этого сигнала.
        if hasattr(self.mode_menu, "visibilityChanged"):
            self.mode_menu.visibilityChanged.connect(self._on_mode_menu_visibility_changed)

        # Правая контекстная панель
        self.context_panel = QFrame()
        self.context_panel.setObjectName("context_panel")
        context_layout = QVBoxLayout(self.context_panel)
        context_layout.setContentsMargins(0, 0, 0, 0)
        context_layout.setSpacing(0)
        self.context_stack = QStackedWidget()
        self.git_panel = GitPanelWidget(self.facade)
        self.context_task_queue = TaskQueueWidget(self.facade)
        self.context_run_operations = RunOperationsPanelWidget(self.facade)
        self.session_settings_panel = SessionSettingsWidget(self.facade)
        self.context_stack.addWidget(self.git_panel)
        self.context_stack.addWidget(self.context_task_queue)
        self.context_stack.addWidget(self.context_run_operations)
        self.context_stack.addWidget(self.session_settings_panel)
        context_layout.addWidget(self.context_stack)

        self.workspace_splitter.addWidget(self.session_manager)
        self.workspace_splitter.addWidget(self.chat_container)
        self.workspace_splitter.addWidget(self.context_panel)
        self.workspace_splitter.setStretchFactor(1, 5)
        self.workspace_splitter.setSizes([250, 900, 0])
        self.context_panel.hide()

        chat_layout.addWidget(self.workspace_splitter, 1)

        # Другие страницы
        self.settings_page = ConfigEditorWidget(self.facade.config_service)
        self.settings_page.load_config()
        self.settings_page.configSaved.connect(self._on_config_saved)

        self.files_page = FilesPanelWidget(self.facade)

        self.logs_page = QWidget()
        logs_layout = QVBoxLayout(self.logs_page)
        logs_layout.setContentsMargins(0, 0, 0, 0)
        logs_layout.setSpacing(0)
        self.task_queue = TaskQueueWidget(self.facade)
        self.task_queue.hide()  # доступен через Tasks в контекстной панели
        logs_layout.addWidget(self.task_queue, 0)
        self.log_viewer = LogViewerWidget(self.facade.task_service)
        logs_layout.addWidget(self.log_viewer, 1)

        self.status_page = StatusPanelWidget(self.facade)
        self.scheduler_page = SchedulerPanelWidget(self.facade, actor_id="desktop")
        self.session_settings_page = SessionSettingsWidget(self.facade)
        self.reports_page = ReportViewerWidget(self.facade)

        self.plugins_page = PluginMenuWidget(self.facade)
        if admin_tab_enabled:
            self.admin_page = AdminPanel(self.facade, )

        self._tab_widgets = {
            "chat": self.chat_page,
            "settings": self.settings_page,
            "files": self.files_page,
            "logs": self.logs_page,
            "status": self.status_page,
            "scheduler": self.scheduler_page,
            "session_settings": self.session_settings_page,
            "reports": self.reports_page,
            "plugins": self.plugins_page,
        }
        if self.admin_page is not None:
            self._tab_widgets["admin"] = self.admin_page
        self.content_stack.addWidget(self.chat_page)
        self.content_stack.addWidget(self.settings_page)
        self.content_stack.addWidget(self.files_page)
        self.content_stack.addWidget(self.logs_page)
        self.content_stack.addWidget(self.status_page)
        self.content_stack.addWidget(self.scheduler_page)
        self.content_stack.addWidget(self.session_settings_page)
        self.content_stack.addWidget(self.reports_page)
        self.content_stack.addWidget(self.plugins_page)
        if self.admin_page is not None:
            self.content_stack.addWidget(self.admin_page)

        layout.addWidget(self.content_stack)

        # Статус-бар
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")

        # Палитра команд
        self.command_palette = CommandPaletteDialog(self)
        self.command_palette.commandTriggered.connect(self._handle_palette_command)
        self._refresh_command_palette()

        # Connect signals
        for tab_name, btn in self._nav_buttons.items():
            btn.clicked.connect(lambda checked=False, t=tab_name: self._switch_tab(t))
        if self.admin_page is not None:
            self.admin_page.enableRequested.connect(lambda session_id: self._trigger_admin_action("enable", session_id))
            self.admin_page.disableRequested.connect(lambda session_id: self._trigger_admin_action("disable", session_id))
            self.admin_page.rescanRequested.connect(lambda session_id: self._trigger_admin_action("rescan", session_id))

        # Горячие клавиши
        shortcut_palette = QAction(self)
        shortcut_palette.setShortcut(QKeySequence("Ctrl+K"))
        shortcut_palette.triggered.connect(self._open_command_palette)
        self.addAction(shortcut_palette)

        shortcut_sidebar = QAction(self)
        shortcut_sidebar.setShortcut(QKeySequence("Ctrl+B"))
        shortcut_sidebar.triggered.connect(lambda checked=False: self._toggle_session_panel())
        self.addAction(shortcut_sidebar)

        shortcut_git = QAction(self)
        shortcut_git.setShortcut(QKeySequence("Ctrl+G"))
        shortcut_git.triggered.connect(
            lambda checked=False: self._toggle_context_panel("git", not self.toggle_git_btn.isChecked())
        )
        self.addAction(shortcut_git)

        for idx, tab_name in enumerate(tuple(self._nav_buttons.keys()), start=1):
            tab_action = QAction(self)
            if idx <= 9:
                tab_action.setShortcut(QKeySequence(f"Ctrl+{idx}"))
            tab_action.triggered.connect(lambda checked=False, t=tab_name: self._switch_tab(t))
            self.addAction(tab_action)

    def _on_config_saved(self):
        """Обработка сохранения конфигурации."""
        async def _reload():
            try:
                await self.facade.reload()
            except Exception:
                self.logger.exception("Failed to reload facade after config save")
                return

            try:
                self._reapply_current_theme()
            except Exception:
                self.logger.exception("Failed to re-apply theme after config save")

            try:
                self.mode_panel.load_modes()
            except Exception:
                self.logger.exception("Failed to refresh mode list after config save")

            try:
                self._refresh_active_session_after_reload()
            except Exception:
                self.logger.exception("Failed to refresh active session after config save")

        ensure_async(_reload(), parent=self)

    def _on_mode_menu_visibility_changed(self, visible: bool) -> None:
        # Синхронизируем separator с видимостью меню,
        # чтобы при закрытом меню не оставался "висящий" визуальный блок.
        self.mode_menu_separator.setVisible(bool(visible))

    def _resolve_session(self, session_uid: Optional[str]) -> Optional[object]:
        token = str(session_uid or "").strip()
        if not token:
            return None
        service = getattr(self.facade, "session_service", None)
        if service is None:
            return None
        getter = getattr(service, "get_session_by_uid", None)
        if not callable(getter):
            return None
        return getter(token)

    @property
    def _current_session_uid(self) -> str:
        return str(self._active_session_uid or "").strip()

    def _trigger_admin_action(self, action: str, session_uid: str) -> None:
        async def _run() -> None:
            ok = await self.facade.run_admin_session_action(
                str(session_uid),
                action=str(action),
            )
            if not ok:
                self.statusBar().showMessage(f"Admin action failed: {action} ({session_uid})")
                return
            self.statusBar().showMessage(f"Admin action: {action} ({session_uid})")
            if self.admin_page is not None and self.admin_page.active_session_uid == str(session_uid):
                self.admin_page.refresh_status_payload()

        ensure_async(_run(), parent=self)

    def _on_session_selected(self, session_uid: str):
        selected_token = str(session_uid or "").strip()
        session = self._resolve_session(selected_token)
        canonical_uid = ""
        if session is not None:
            runtime_uid = str(session_runtime_uid(session) or "").strip()
            if runtime_uid.startswith(("chat:", "thread:", "desktop:", "miniapp:", "forum:")):
                canonical_uid = runtime_uid
        self._active_session_uid = canonical_uid or selected_token or None
        current_session_uid = self._current_session_uid
        self.statusBar().showMessage(f"Active session: {current_session_uid}")
        self.chat_view.setEnabled(True)
        self.chat_view.hide_ask_options()
        self.chat_view.clear_assistant_preview()
        # Restore persisted history (if any).
        self.chat_view.clear_history()
        restored = False
        try:
            history = getattr(self.ui_state_service.state, "chat_history", {}) or {}
            items = history.get(current_session_uid) or []
            for entry in items:
                role = str(entry.get("role") or "").strip()
                text = str(entry.get("text") or "")
                attachments = entry.get("attachments")
                if role in ("user", "agent") and (text or attachments):
                    self.chat_view.append_message(role, text, attachments=attachments)
                    restored = True
        except Exception:
            self.logger.exception("failed to restore chat history session_uid=%s", current_session_uid)
        if not restored:
            self.chat_view.append_message("agent", f"Session **{current_session_uid}** is now active.")

        # Update git panel
        session = session or self._resolve_session(current_session_uid)
        self.git_panel.set_session(session)
        self.mode_panel.set_session(session)
        self.mode_menu.set_session(current_session_uid)
        if self.admin_page is not None:
            self.admin_page.set_session(current_session_uid)
        self.files_page.set_session(session)
        self.status_page.set_session(session, current_session_uid)
        self.scheduler_page.set_context_session(current_session_uid)
        # При выборе сессии в desktop ожидаем, что меню режима будет синхронизировано через facade.show_mode_menu.
        self._refresh_mode_menu_for_session(session, current_session_uid, force_open=True)
        self.reports_page.set_session(current_session_uid)
        self.task_queue.set_session_id(current_session_uid)
        self.context_task_queue.set_session_id(current_session_uid)
        self.context_run_operations.set_session_id(current_session_uid)
        self.session_settings_panel.set_session(session)
        self.session_settings_page.set_session(session)
        self.task_progress.set_session_id(current_session_uid)
        self._show_pending_ask_for_session(current_session_uid)

        # Check if there are active tasks for this session
        active_tasks = self.facade.task_service.list_active(session_id=current_session_uid)
        if active_tasks:
            self.chat_view.set_loading(True)
            self.chat_view.append_message("agent", "_Session has active tasks running..._")

    def _on_message_sent(self, text: str, attachments: Optional[list[str]] = None):
        if not self._active_session_uid:
            return

        session_uid = self._current_session_uid

        async def _run() -> None:
            try:
                prepared = None
                if attachments:
                    prepared = await self.facade.prepare_attachments(
                        session_uid,
                        list(attachments),
                    )

                # Show user message (with attachments meta stored for Phase 1 history).
                self.chat_view.append_message("user", str(text or ""), attachments=(prepared.meta if prepared else None))
                self._persist_chat_message(session_uid, "user", str(text or ""), attachments=(prepared.meta if prepared else None))

                if self._try_resolve_pending_ask(session_uid, str(text or "")):
                    return

                # Dialogs (audit flows, etc.) must intercept user input when active.
                dialog_out = await self.facade.handle_dialog_message(session_uid, text=text)
                if dialog_out is not None:
                    if dialog_out:
                        self.chat_view.append_message("agent", dialog_out)
                        self._persist_chat_message(session_uid, "agent", dialog_out)
                    return
                await self.facade.stage_session_input(
                    session_uid,
                    text,
                    prepared_attachments=prepared,
                )
            except Exception:
                # Ошибка уже отражается через task:failed; здесь важно не оставить
                # "Task exception was never retrieved".
                self.logger.exception("desktop input dispatch failed session_uid=%s", session_uid)

        self._active_run_task = ensure_async(_run(), parent=self)

    @staticmethod
    def _format_ask_message(question: str, options: list[str]) -> str:
        """Форматирует текст вопроса ask_user.

        Варианты ответа отображаются отдельными кнопками (см. show_ask_options),
        здесь выводится только сам вопрос.
        """
        return str(question or "").strip() or "Нужно уточнение от пользователя."

    def _show_pending_ask_for_session(self, session_id: str) -> None:
        pending = self._pending_ask_by_session.get(str(session_id))
        if not isinstance(pending, dict):
            return
        options = [str(x).strip() for x in (pending.get("options") or []) if str(x).strip()]
        if not bool(pending.get("shown", False)):
            text = self._format_ask_message(
                str(pending.get("question") or ""),
                options,
            )
            self.chat_view.append_message("agent", text)
            self._persist_chat_message(str(session_id), "agent", text)
            pending["shown"] = True
        # Показываем кнопки с вариантами ответа при каждом входе в сессию,
        # пока вопрос не закрыт.
        if options:
            self.chat_view.show_ask_options(options)

    def _on_ask_option_selected(self, option_text: str):
        """Обработка клика по кнопке варианта ответа ask_user."""
        sid = self._current_session_uid
        pending = self._pending_ask_by_session.get(sid)
        if not isinstance(pending, dict):
            return
        question_id = str(pending.get("question_id") or "")
        resolved = bool(self.facade.resolve_analyst_question(question_id, option_text))
        self._pending_ask_by_session.pop(sid, None)
        # Показываем выбранный вариант как сообщение пользователя
        self.chat_view.append_message("user", option_text)
        self._persist_chat_message(sid, "user", option_text)
        msg = f"Принял ответ: {option_text}" if resolved else "Вопрос уже закрыт."
        self.chat_view.append_message("agent", msg)
        self._persist_chat_message(sid, "agent", msg)
        # Восстанавливаем loading — задача продолжает выполняться
        self.chat_view.set_loading(True)

    def _try_resolve_pending_ask(self, session_id: str, text: str) -> bool:
        sid = str(session_id or "")
        pending = self._pending_ask_by_session.get(sid)
        if not isinstance(pending, dict):
            return False
        raw = str(text or "").strip()
        if not raw:
            msg = "Нужен ответ на уточняющий вопрос (номер варианта или текст)."
            self.chat_view.append_message("agent", msg)
            self._persist_chat_message(sid, "agent", msg)
            return True
        options = [str(x).strip() for x in (pending.get("options") or []) if str(x).strip()]
        allow_custom = bool(pending.get("allow_custom", True))
        answer = raw
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                answer = options[idx]
        elif options:
            try:
                answer = self._ask_option_parser.parse_selected_option(raw, allowed_options=options)
            except (ValueError, RuntimeError):
                if not allow_custom:
                    msg = "Ответ не распознан. Выберите кнопку или введите полный текст варианта."
                    self.chat_view.append_message("agent", msg)
                    self._persist_chat_message(sid, "agent", msg)
                    return True
        question_id = str(pending.get("question_id") or "")
        resolved = bool(self.facade.resolve_analyst_question(question_id, answer))
        self._pending_ask_by_session.pop(sid, None)
        # Скрываем кнопки вариантов, т.к. пользователь ответил текстом
        self.chat_view.hide_ask_options()
        msg = f"Принял ответ: {answer}" if resolved else "Вопрос уже закрыт."
        self.chat_view.append_message("agent", msg)
        self._persist_chat_message(sid, "agent", msg)
        # Восстанавливаем loading — задача продолжает выполняться
        self.chat_view.set_loading(True)
        return True

    def _on_task_cancelled(self):
        if not self._active_session_uid:
            return

        async def _do_cancel():
            await self.facade.task_service.cancel_session(self._current_session_uid)

        ensure_async(_do_cancel(), parent=self)

    def _persist_chat_message(self, session_id: str, role: str, text: str, attachments: Optional[list[dict]] = None) -> None:
        sid = str(session_id)
        r = str(role or "").strip()
        if r not in ("user", "agent"):
            return
        t = str(text or "")
        if not t and not attachments:
            return
        try:
            state = self.ui_state_service.state
            history = getattr(state, "chat_history", None)
            if not isinstance(history, dict):
                history = {}
                setattr(state, "chat_history", history)
            items = history.get(sid)
            if not isinstance(items, list):
                items = []
                history[sid] = items
            entry: dict = {"role": r, "text": t}
            if attachments:
                entry["attachments"] = list(attachments)
            items.append(entry)
            # Cap history size per session to keep desktop_state.json bounded.
            limit = 200
            if len(items) > limit:
                history[sid] = items[-limit:]
            ensure_async(self.ui_state_service.save(chat_history=history), parent=self)
        except Exception:
            self.logger.exception("failed to persist chat message session_id=%s", sid)

    def _on_facade_notification(self, note: "AppNotification"):
        """Обработка уведомлений от фасада."""
        event = note.event
        payload = note.payload
        session_uid = payload.get("session_id") or payload.get("session_uid")

        # UI-level messaging from modes (menus, dialogs, etc.)
        if event == "ui:theme_changed":
            self._apply_theme()
            return

        if event in ("ui:session_updated", "ui:session_settings_changed"):
            if session_uid != self._active_session_uid:
                return
            session = self._resolve_session(self._active_session_uid)
            self.session_settings_panel.set_session(session)
            self.session_settings_page.set_session(session)
            self.files_page.set_session(session)
            self.files_page.refresh()
            self.scheduler_page.set_context_session(self._current_session_uid)
            return

        if event == "ui:mode_changed":
            if session_uid != self._active_session_uid:
                return
            mode_id = str(payload.get("mode_id") or "").strip()
            session = self._resolve_session(self._active_session_uid)
            self.mode_panel.set_session(session)
            self.status_page.refresh_mode(session)
            if not mode_id:
                self.mode_menu.clear()
                return
            self._refresh_mode_menu_for_session(session)
            return

        if event == "ui:ask_question":
            sid = str(payload.get("session_uid") or payload.get("session_id") or "")
            if not sid:
                return
            self._pending_ask_by_session[sid] = {
                "question_id": str(payload.get("question_id") or ""),
                "question": str(payload.get("question") or ""),
                "options": list(payload.get("options") or []),
                "allow_custom": bool(payload.get("allow_custom", True)),
                "shown": False,
            }
            if sid == self._active_session_uid:
                self._show_pending_ask_for_session(sid)
            return

        if event == "ui:message":
            if session_uid != self._active_session_uid:
                return
            role = str(payload.get("role") or "agent")
            text = str(payload.get("text") or "")
            attachments = payload.get("attachments")
            if text or attachments:
                self.chat_view.append_message(
                    role if role in ("user", "agent") else "agent",
                    text,
                    attachments=attachments
                )
                if role in ("user", "agent") and self._active_session_uid:
                    self._persist_chat_message(self._active_session_uid, role, text, attachments=attachments)
            return

        if event == "ui:analyst_progress":
            if session_uid != self._active_session_uid:
                return
            phase = str(payload.get("phase") or "Анализ")
            elapsed = int(payload.get("elapsed_seconds") or 0)
            mins, secs = divmod(elapsed, 60)
            text = f"🧠 Аналитик: {phase}\n⏱ {mins}:{secs:02d}"
            if self.chat_view._progress_message_id is None:
                self.chat_view.append_progress_message("agent", text)
            else:
                self.chat_view.update_progress_message("agent", text)
            return

        if event == "ui:analyst_progress_clear":
            if session_uid != self._active_session_uid:
                return
            self.chat_view.clear_progress_message()
            return

        if event == "ui:manage_tasks_progress":
            if session_uid != self._active_session_uid:
                return
            raw_tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
            text = format_manage_tasks_progress(raw_tasks)
            if not text:
                self.chat_view.clear_progress_message()
            elif self.chat_view._progress_message_id is None:
                self.chat_view.append_progress_message("agent", text)
            else:
                self.chat_view.update_progress_message("agent", text)
            return

        if event == "ui:manage_tasks_progress_clear":
            if session_uid != self._active_session_uid:
                return
            self.chat_view.clear_progress_message()
            return

        if event == "ui:assistant_preview":
            if session_uid != self._active_session_uid:
                return
            text = str(payload.get("text") or "")
            if text:
                self.chat_view.set_assistant_preview(text)
            else:
                self.chat_view.clear_assistant_preview()
            return

        if event == "ui:assistant_preview_clear":
            if session_uid != self._active_session_uid:
                return
            self.chat_view.clear_assistant_preview()
            return

        if event == "ui:v2_event":
            if session_uid != self._active_session_uid:
                return
            v2_type = str(payload.get("event_type") or "").strip()
            status = str(payload.get("status") or "").strip()
            if not v2_type:
                return

            icon = "ℹ️"
            if v2_type == "retry":
                icon = "🔄"
            elif v2_type == "reroute":
                icon = "🔀"
            elif v2_type == "needs_input":
                icon = "💬"

            msg = f"{icon} **[v2] {v2_type}:** {status or 'unknown'}"
            self.chat_view.append_message("agent", msg)
            if self._active_session_uid:
                self._persist_chat_message(self._active_session_uid, "agent", msg)
            return

        if event == "ui:validation_status":
            if session_uid != self._active_session_uid:
                return
            status = str(payload.get("status") or "").strip().lower()
            if status == "not_run":
                msg = "⚠️ **[validation] not_run:** часть тулчейнов недоступна"
                self.chat_view.append_message("agent", msg)
                if self._active_session_uid:
                    self._persist_chat_message(self._active_session_uid, "agent", msg)
            return

        if event == "ui:dirs_flow:start":
            # Mode requests selecting a path.
            chat_id = int(payload.get("chat_id") or 1)
            if chat_id != int(self.session_manager.actor_id):
                return
            root = str(payload.get("root") or "")
            mode_token = str(payload.get("mode_token") or "")
            # Decide whether files are allowed (mode can request file selection).
            allow_files = False
            try:
                from modes.sdk.dirs_mode import decode_mode_dirs

                mode_id, flow = decode_mode_dirs(mode_token)
                if mode_id and flow and self.facade.mode_registry_service:
                    plugin = self.facade.mode_registry_service.get(mode_id)
                    if plugin is not None and hasattr(plugin, "include_files_in_dirs"):
                        allow_files = bool(plugin.include_files_in_dirs(flow))
            except Exception:
                allow_files = False

            caption = "Выберите файл" if allow_files else "Выберите папку"
            try:
                if allow_files:
                    path, _filter = QFileDialog.getOpenFileName(self, caption, root or "")
                else:
                    path = QFileDialog.getExistingDirectory(self, caption, root or "")
            except Exception:
                path = ""

            async def _route() -> None:
                if path:
                    await self.facade.handle_dirs_flow_event(self._current_session_uid, event="selected", path=path)
                else:
                    await self.facade.handle_dirs_flow_event(self._current_session_uid, event="cancelled", path="")

            ensure_async(_route(), parent=self)
            return

        if session_uid != self._active_session_uid:
            # Мы обрабатываем только события для текущей активной сессии в чате
            return

        if event == "task:started":
            self.chat_view.set_loading(True)
        elif event == "task:completed":
            self.chat_view.set_loading(False)
            self.chat_view.clear_assistant_preview()
        elif event == "task:cancelled":
            self.chat_view.set_loading(False)
            self.chat_view.clear_assistant_preview()
            self._pending_ask_by_session.pop(str(session_uid), None)
            reason = payload.get("reason", "cancelled")
            msg = f"_Task {reason}_"
            self.chat_view.append_message("agent", msg)
            if self._active_session_uid:
                self._persist_chat_message(self._active_session_uid, "agent", msg)
        elif event == "task:failed":
            self.chat_view.set_loading(False)
            self.chat_view.clear_assistant_preview()
            self._pending_ask_by_session.pop(str(session_uid), None)
            error = payload.get("error", "unknown error")
            msg = f"**Error:** {error}"
            self.chat_view.append_message("agent", msg)
            if self._active_session_uid:
                self._persist_chat_message(self._active_session_uid, "agent", msg)

    def _switch_tab(self, tab_name: str, *, persist: bool = True):
        """Переключение вкладок."""
        widget = self._tab_widgets.get(tab_name)
        if widget is None:
            return
        self.content_stack.setCurrentWidget(widget)
        for key, btn in self._nav_buttons.items():
            btn.setChecked(key == tab_name)

        if persist:
            # Сохраняем активную вкладку в состоянии только для пользовательского переключения
            ensure_async(self.ui_state_service.save(active_tab=tab_name), parent=self)

    def _toggle_session_panel(self, *, persist: bool = True) -> None:
        sizes = self.workspace_splitter.sizes()
        if sizes and sizes[0] == 0:
            sizes[0] = 250
            sizes[1] = max(sizes[1], 700)
            self.toggle_sessions_btn.setChecked(True)
            self.session_manager.show()
            visible = True
        else:
            sizes[0] = 0
            self.toggle_sessions_btn.setChecked(False)
            visible = False
        self.session_manager.setVisible(visible)
        self.workspace_splitter.setSizes(sizes)
        if persist:
            ensure_async(self.ui_state_service.save(session_panel_visible=visible), parent=self)

    def _toggle_context_panel(self, tool: str, checked: bool, *, persist: bool = True) -> None:
        if not checked:
            self._hide_context_panel(persist=persist)
            return
        self._show_context_panel(tool, persist=persist)

    def _show_context_panel(self, tool: str, *, persist: bool = True) -> None:
        index_map = {"git": 0, "tasks": 1, "runs": 2, "session": 3}
        index = index_map.get(str(tool or "").strip(), 0)
        self.context_stack.setCurrentIndex(index)
        self.context_panel.show()
        sizes = self.workspace_splitter.sizes()
        if len(sizes) >= 3 and sizes[2] < 260:
            sizes[2] = 360
            if sizes[1] > 500:
                sizes[1] -= 200
        self.workspace_splitter.setSizes(sizes)
        self.toggle_git_btn.setChecked(tool == "git")
        self.toggle_tasks_btn.setChecked(tool == "tasks")
        self.toggle_runs_btn.setChecked(tool == "runs")
        self.toggle_session_settings_btn.setChecked(tool == "session")
        if persist:
            ensure_async(
                self.ui_state_service.save(context_panel_visible=True, context_panel_tool=tool),
                parent=self,
            )

    def _hide_context_panel(self, *, persist: bool = True) -> None:
        sizes = self.workspace_splitter.sizes()
        if len(sizes) >= 3:
            sizes[2] = 0
            self.workspace_splitter.setSizes(sizes)
        self.context_panel.hide()
        self.toggle_git_btn.setChecked(False)
        self.toggle_tasks_btn.setChecked(False)
        self.toggle_runs_btn.setChecked(False)
        self.toggle_session_settings_btn.setChecked(False)
        if persist:
            ensure_async(
                self.ui_state_service.save(context_panel_visible=False, context_panel_tool="none"),
                parent=self,
            )

    def _refresh_command_palette(self) -> None:
        commands = [
            CommandPaletteItem("tab:chat", "Open Chat", "Main workspace", ("chat", "workspace"), "Navigation", "Ctrl+1"),
            CommandPaletteItem("tab:settings", "Open Config", "Configuration editor", ("settings", "config"), "Navigation", "Ctrl+2"),
            CommandPaletteItem("tab:files", "Open Files", "Session file browser and editor", ("files", "editor"), "Navigation", "Ctrl+3"),
            CommandPaletteItem("tab:logs", "Open Logs", "Task queue and logs", ("logs", "tasks"), "Navigation", "Ctrl+4"),
            CommandPaletteItem("tab:status", "Open Status", "Session status, queue, and runs", ("status", "runs"), "Navigation", "Ctrl+5"),
            CommandPaletteItem("tab:scheduler", "Open Scheduler", "Scheduled jobs", ("scheduler", "jobs"), "Navigation", "Ctrl+6"),
            CommandPaletteItem(
                "tab:session_settings",
                "Open Settings",
                "Session and SSH settings",
                ("settings", "session", "ssh"),
                "Navigation",
                "Ctrl+7",
            ),
            CommandPaletteItem("tab:reports", "Open Reports", "Reports viewer", ("reports", "viewer"), "Navigation", "Ctrl+8"),
            CommandPaletteItem("tab:plugins", "Open Plugins", "Plugin management", ("plugins",), "Navigation", "Ctrl+9"),
        ]
        if self.admin_page is not None:
            commands.append(
                CommandPaletteItem("tab:admin", "Open Admin", "Admin panel", ("admin",), "Navigation", "")
            )
        commands.extend(
            [
                CommandPaletteItem(
                    "panel:sessions",
                    "Toggle Sessions Panel",
                    "Show/hide left panel",
                    ("sidebar", "sessions"),
                    "Panels",
                    "Ctrl+B",
                ),
                CommandPaletteItem("panel:git", "Open Git Panel", "Show git operations", ("git", "status", "commit"), "Panels", "Ctrl+G"),
                CommandPaletteItem("panel:tasks", "Open Tasks Panel", "Show active tasks", ("tasks", "queue"), "Panels", ""),
                CommandPaletteItem(
                    "panel:runs",
                    "Open Runs Panel",
                    "Show run recovery controls",
                    ("runs", "doctor", "recover", "resume"),
                    "Panels",
                    "",
                ),
                CommandPaletteItem(
                    "session:new",
                    "New Session",
                    "Create session from selected CLI",
                    ("session", "new"),
                    "Session",
                    "",
                ),
                CommandPaletteItem(
                    "session:limits",
                    "Show CLI Limits",
                    "Show limits/usage for active desktop CLI",
                    ("limits", "quota", "tokens", "usage"),
                    "Session",
                    "",
                ),
                CommandPaletteItem(
                    "git:refresh",
                    "Refresh Git",
                    "Refresh git status/history",
                    ("git", "refresh"),
                    "Git",
                    "",
                ),
            ]
        )
        self.command_palette.set_commands(commands)

    def _apply_sidebar_icons(self) -> None:
        icon_map = {
            "chat": "SP_MessageBoxInformation",
            "settings": "SP_FileDialogDetailedView",
            "files": "SP_DirIcon",
            "reports": "SP_FileDialogInfoView",
            "logs": "SP_FileDialogListView",
            "status": "SP_DialogApplyButton",
            "scheduler": "SP_BrowserReload",
            "session_settings": "SP_FileDialogContentsView",
            "plugins": "SP_DesktopIcon",
            "admin": "SP_ComputerIcon",
        }
        fallback = QStyle.StandardPixmap.SP_FileIcon
        style = self.style()
        for key, btn in self._nav_buttons.items():
            icon_name = icon_map.get(key, "")
            pixmap_enum = getattr(QStyle.StandardPixmap, icon_name, fallback)
            btn.setIcon(style.standardIcon(pixmap_enum))

    def _open_command_palette(self) -> None:
        last_query = str(getattr(self.ui_state_service.state, "command_palette_last_query", "") or "")
        self.command_palette.set_recent_commands(
            list(getattr(self.ui_state_service.state, "command_palette_recent", []) or [])
        )
        self.command_palette.open_with_query(last_query)

    def _handle_palette_command(self, command_id: str) -> None:
        command_id = str(command_id or "").strip()
        if not command_id:
            return
        self._record_palette_command(command_id)
        ensure_async(self.ui_state_service.save(command_palette_last_query=self.command_palette.search_input.text()), parent=self)
        if command_id.startswith("tab:"):
            self._switch_tab(command_id.split(":", 1)[1])
            return
        if command_id == "panel:sessions":
            self._toggle_session_panel(persist=False)
            return
        if command_id == "panel:git":
            self._show_context_panel("git")
            return
        if command_id == "panel:tasks":
            self._show_context_panel("tasks")
            return
        if command_id == "panel:runs":
            self._show_context_panel("runs")
            return
        if command_id == "session:new":
            self.session_manager.start_new_session()
            return
        if command_id == "session:limits":
            if not self._current_session_uid:
                self.statusBar().showMessage("Сначала выберите desktop-сессию.")
                return

            async def _show_limits() -> None:
                text = await self.facade.describe_active_cli_limits()
                if text:
                    self.chat_view.append_message("agent", text)
                    self._persist_chat_message(self._current_session_uid, "agent", text)

            ensure_async(_show_limits(), parent=self)
            return
        if command_id == "git:refresh":
            self.git_panel.refresh_status()
            self.git_panel.refresh_history()

    def _record_palette_command(self, command_id: str) -> None:
        existing = list(getattr(self.ui_state_service.state, "command_palette_recent", []) or [])
        normalized = [str(x).strip() for x in existing if str(x).strip()]
        if command_id in normalized:
            normalized.remove(command_id)
        normalized.insert(0, command_id)
        normalized = normalized[:12]
        ensure_async(
            self.ui_state_service.save(command_palette_recent=normalized),
            parent=self,
        )

    def _restore_state(self):
        """Восстановление состояния окна из сервиса."""
        state = self.ui_state_service.state
        self.facade.set_theme(self._current_theme_name())
        self._apply_theme()

        if state.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(state.window_geometry.encode()))
        if state.window_state:
            self.restoreState(QByteArray.fromBase64(state.window_state.encode()))

        if state.splitter_sizes and len(state.splitter_sizes) >= 3:
            self.workspace_splitter.setSizes(state.splitter_sizes)

        active_tab = str(getattr(state, "active_tab", "") or "chat")
        if active_tab not in self._tab_widgets:
            active_tab = "chat"
        self._switch_tab(active_tab, persist=False)

        session_panel_visible = bool(getattr(state, "session_panel_visible", True))
        if not session_panel_visible:
            self._toggle_session_panel(persist=False)

        context_visible = bool(getattr(state, "context_panel_visible", False))
        context_tool = str(getattr(state, "context_panel_tool", "none") or "none")
        if context_visible and context_tool in ("git", "tasks", "runs", "session"):
            self._show_context_panel(context_tool, persist=False)
        else:
            self._hide_context_panel(persist=False)

        # Восстановить выбор последней активной сессии (setCurrentItem → sessionSelected → _on_session_selected)
        if state.last_session_id:
            restore = getattr(self.session_manager, "restore_selection", None)
            if callable(restore):
                restore(state.last_session_id)

    def _current_theme_name(self) -> str:
        state = getattr(self.ui_state_service, "state", None)
        state_theme = getattr(state, "theme", None)
        if isinstance(state_theme, str):
            token = state_theme.strip().lower()
            if token:
                return token

        theme_service = getattr(self.facade, "theme_service", None)
        getter = getattr(theme_service, "get_current_theme_name", None)
        if callable(getter):
            current_theme = getter()
            if isinstance(current_theme, str):
                token = current_theme.strip().lower()
                if token:
                    return token

        current_theme = getattr(theme_service, "_current_theme_name", None)
        if isinstance(current_theme, str):
            token = current_theme.strip().lower()
            if token:
                return token

        return "system"

    def _reapply_current_theme(self) -> None:
        self.facade.set_theme(self._current_theme_name())
        self._apply_theme()

    def _refresh_active_session_after_reload(self) -> None:
        if not self._active_session_uid:
            return
        self._on_session_selected(self._active_session_uid)

    def _apply_theme(self):
        """Применяет текущую тему оформления."""
        colors = self.facade.theme_service.get_theme_colors()
        self.setStyleSheet(self.facade.theme_service.get_main_stylesheet())

        # Уведомляем компоненты, которым может потребоваться внутренняя отрисовка
        widgets = [
            self.chat_view,
            self.log_viewer,
            self.mode_panel,
            self.task_progress,
            self.reports_page,
        ]
        for w in widgets:
            if hasattr(w, "set_theme_colors"):
                w.set_theme_colors(colors)

    def _refresh_mode_menu_for_session(
        self,
        session: Optional[object],
        session_uid: Optional[str] = None,
        *,
        force_open: bool = False,
    ) -> None:
        """
        Универсальный рендер mode UI: берем описание из active mode (build_menu)
        и отображаем в ModeMenuWidget без привязки к конкретным mode_id.
        """
        if not session:
            self.mode_menu.clear()
            return
        mode_id = str(get_active_mode(session, "") or "").strip()
        if not mode_id:
            self.mode_menu.clear()
            return
        try:
            self.facade._ensure_modes_ready()
        except Exception:
            self.logger.exception("mode warmup failed for session_id=%s", getattr(session, "id", ""))
        if not self._mode_supports_menu(mode_id):
            self.mode_menu.clear()
            return
        # В тестовой среде (и при первом выборе сессии) окно может быть не "visible" в Qt смысле,
        # но синхронизацию меню через facade.show_mode_menu все равно нужно выполнить.
        if not force_open and not self.mode_menu.isVisible():
            return
        ensure_async(
            self.facade.show_mode_menu(
                session_uid or str(getattr(getattr(session, "conversation_scope", None), "session_uid", "") or "")
            ),
            parent=self,
        )

    def _mode_supports_menu(self, mode_id: str) -> bool:
        svc = getattr(self.facade, "mode_registry_service", None)
        if not svc:
            return False
        plugin = svc.get(str(mode_id or "").strip())
        if plugin is None:
            return False
        try:
            from modes.sdk.base import BaseMode

            return plugin.__class__.build_menu is not BaseMode.build_menu
        except Exception:
            self.logger.exception("mode supports_menu check failed mode=%s", mode_id)
            return False

    def _is_admin_tab_enabled(self) -> bool:
        svc = getattr(self.facade, "mode_registry_service", None)
        if not svc:
            return False
        try:
            return svc.get("admin") is not None
        except Exception:
            self.logger.exception("admin tab availability check failed")
            return False

    async def _drain_background_tasks(self, *, timeout_s: Optional[float] = None) -> None:
        current = asyncio.current_task()
        pending = [
            task
            for task in list(self._background_tasks or set())
            if task is not None and task is not current and not task.done()
        ]
        if not pending:
            return
        timeout = float(timeout_s or self._CLOSE_BACKGROUND_TIMEOUT_S)
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                continue
            except Exception:
                self.logger.exception("main window background task failed during close")
        if not still_pending:
            return
        for task in still_pending:
            task.cancel()
        done, abandoned = await asyncio.wait(still_pending, timeout=timeout)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                continue
            except Exception:
                self.logger.exception("main window background task failed after cancellation during close")
        if abandoned:
            self.logger.warning("main window close abandoned %s background tasks", len(abandoned))

    async def _finalize_close(self, state_payload: dict[str, object], *, trigger_close: bool = True) -> None:
        save_failed = False
        try:
            try:
                await self.ui_state_service.save(**state_payload)
            except Exception:
                save_failed = True
                self.logger.exception("main window close state save failed")
            try:
                await self._drain_background_tasks()
            except Exception:
                self.logger.exception("main window background task drain failed before shutdown")
            try:
                await self.facade.shutdown()
            except Exception:
                self.logger.exception("main window facade shutdown failed")
            try:
                await self._drain_background_tasks()
            except Exception:
                self.logger.exception("main window background task drain failed after shutdown")
        finally:
            self._closing_in_progress = False
            self._close_finalized = True
            self._close_task = None
            if trigger_close:
                self.close()
        if save_failed:
            return

    def closeEvent(self, event):
        """Сохранение состояния при закрытии."""
        if self._close_finalized:
            super().closeEvent(event)
            return
        if self._closing_in_progress:
            event.ignore()
            return

        geometry = self.saveGeometry().toBase64().data().decode()
        window_state = self.saveState().toBase64().data().decode()
        splitter_sizes = self.workspace_splitter.sizes()
        context_tool = "none"
        if self.toggle_git_btn.isChecked():
            context_tool = "git"
        elif self.toggle_tasks_btn.isChecked():
            context_tool = "tasks"
        elif self.toggle_runs_btn.isChecked():
            context_tool = "runs"
        elif self.toggle_session_settings_btn.isChecked():
            context_tool = "session"

        state_payload = dict(
            window_geometry=geometry,
            window_state=window_state,
            splitter_sizes=splitter_sizes,
            context_panel_visible=bool(self.context_panel.isVisible()),
            context_panel_tool=context_tool,
            session_panel_visible=bool(self.toggle_sessions_btn.isChecked()),
            command_palette_last_query=self.command_palette.search_input.text(),
        )
        self._closing_in_progress = True
        self._close_task = ensure_async(self._finalize_close(state_payload), parent=self)
        if self._close_task is not None:
            event.ignore()
            return

        self._closing_in_progress = False
        try:
            fallback_loop = asyncio.get_event_loop()
        except RuntimeError:
            fallback_loop = None
        try:
            if fallback_loop is not None and not fallback_loop.is_running() and not fallback_loop.is_closed():
                fallback_loop.run_until_complete(self._finalize_close(state_payload, trigger_close=False))
            else:
                self.logger.error("main window close fallback unavailable: no reusable event loop")
                event.ignore()
                return
        except Exception:
            self.logger.exception("main window close fallback failed")
            event.ignore()
            return
        super().closeEvent(event)
