from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QPlainTextEdit,
    QLineEdit,
    QLabel,
    QMessageBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QHeaderView,
    QGroupBox,
)

from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade
    from session import Session


class GitPanelWidget(QWidget):
    """
    Улучшенный виджет для операций Git: статус, история, коммит, получение и отправка изменений.
    Операции выполняются асинхронно через основной цикл событий приложения.
    """

    def __init__(
        self,
        facade: ApplicationFacade,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.facade = facade
        self.logger = logging.getLogger(__name__)
        self._active_session: Optional[Session] = None
        self._is_git_repo: bool = False

        self._setup_ui()

    def _setup_ui(self):
        self.setObjectName("enhanced_git_integration")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Remote mode banner
        from desktop.widgets.remote_mode_banner import RemoteModeBanner
        self.remote_banner = RemoteModeBanner()
        layout.addWidget(self.remote_banner)

        # Заголовок
        title = QLabel("Enhanced Git Integration")
        title.setObjectName("git_title")
        layout.addWidget(title)

        # Создаем вкладки для разных функций Git
        self.tabs = QTabWidget()

        # Вкладка статуса
        self.status_tab = QWidget()
        self._setup_status_tab()
        self.tabs.addTab(self.status_tab, "Status")

        # Вкладка истории
        self.history_tab = QWidget()
        self._setup_history_tab()
        self.tabs.addTab(self.history_tab, "History")

        # Вкладка коммитов
        self.commit_tab = QWidget()
        self._setup_commit_tab()
        self.tabs.addTab(self.commit_tab, "Commit")

        # Вкладка операций
        self.operations_tab = QWidget()
        self._setup_operations_tab()
        self.tabs.addTab(self.operations_tab, "Operations")

        layout.addWidget(self.tabs)

        # Изначально панель отключена до выбора сессии
        self.setEnabled(False)

    def _setup_status_tab(self):
        """Настройка вкладки статуса."""
        layout = QVBoxLayout(self.status_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Поле вывода статуса
        self.status_display = QPlainTextEdit()
        self.status_display.setReadOnly(True)
        self.status_display.setPlaceholderText("Select a session to see git status...")
        layout.addWidget(self.status_display)

        # Кнопка обновления статуса
        self.refresh_btn = QPushButton("Refresh Status")
        self.refresh_btn.clicked.connect(self.refresh_status)
        layout.addWidget(self.refresh_btn)

    def _setup_history_tab(self):
        """Настройка вкладки истории."""
        layout = QVBoxLayout(self.history_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Дерево истории коммитов
        self.history_tree = QTreeWidget()
        self.history_tree.setHeaderLabels(["Commit", "Author", "Date", "Message"])
        self.history_tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.history_tree)

        # Кнопки управления историей
        btn_layout = QHBoxLayout()
        self.refresh_history_btn = QPushButton("Refresh History")
        self.refresh_history_btn.clicked.connect(self.refresh_history)
        btn_layout.addWidget(self.refresh_history_btn)

        self.show_diff_btn = QPushButton("Show Diff")
        self.show_diff_btn.clicked.connect(self.show_commit_diff)
        btn_layout.addWidget(self.show_diff_btn)

        layout.addLayout(btn_layout)

    def _setup_commit_tab(self):
        """Настройка вкладки коммита."""
        layout = QVBoxLayout(self.commit_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Секция коммита
        commit_group_layout = QVBoxLayout()
        commit_group_layout.addWidget(QLabel("Commit Message:"))
        self.commit_msg_input = QLineEdit()
        self.commit_msg_input.setPlaceholderText("Optional — LLM сгенерирует сообщение автоматически")
        commit_group_layout.addWidget(self.commit_msg_input)

        self.commit_btn = QPushButton("Commit (Add All)")
        self.commit_btn.clicked.connect(self._on_commit_clicked)
        commit_group_layout.addWidget(self.commit_btn)
        layout.addLayout(commit_group_layout)

        # Кнопка для генерации сообщения
        self.generate_msg_btn = QPushButton("Generate Message with AI")
        self.generate_msg_btn.clicked.connect(self._generate_commit_message_ui)
        layout.addWidget(self.generate_msg_btn)

    def _setup_operations_tab(self):
        """Настройка вкладки операций."""
        layout = QVBoxLayout(self.operations_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Группа удаленных операций
        remote_group = QGroupBox("Remote Operations")
        remote_layout = QHBoxLayout(remote_group)

        self.pull_btn = QPushButton("Pull")
        self.pull_btn.clicked.connect(self._on_pull_clicked)
        remote_layout.addWidget(self.pull_btn)

        self.push_btn = QPushButton("Push")
        self.push_btn.clicked.connect(self._on_push_clicked)
        remote_layout.addWidget(self.push_btn)

        layout.addWidget(remote_group)

        # Группа других операций
        other_group = QGroupBox("Other Operations")
        other_layout = QVBoxLayout(other_group)

        stash_layout = QHBoxLayout()
        self.stash_btn = QPushButton("Stash")
        self.stash_btn.clicked.connect(self._on_stash_clicked)
        stash_layout.addWidget(self.stash_btn)

        self.stash_pop_btn = QPushButton("Stash Pop")
        self.stash_pop_btn.clicked.connect(self._on_stash_pop_clicked)
        stash_layout.addWidget(self.stash_pop_btn)
        other_layout.addLayout(stash_layout)

        branch_layout = QHBoxLayout()
        self.branch_create_btn = QPushButton("Create Branch")
        self.branch_create_btn.clicked.connect(self._on_branch_create_clicked)
        branch_layout.addWidget(self.branch_create_btn)

        self.branch_switch_btn = QPushButton("Switch Branch")
        self.branch_switch_btn.clicked.connect(self._on_branch_switch_clicked)
        branch_layout.addWidget(self.branch_switch_btn)
        other_layout.addLayout(branch_layout)

        layout.addWidget(other_group)

        layout.addStretch()

    def set_session(self, session: Optional[Session]):
        """Устанавливает активную сессию и обновляет статус Git."""
        self._active_session = session
        self._is_git_repo = False
        self._update_remote_banner()
        if session:
            self.setEnabled(True)
            self._refresh_all()
        else:
            self.setEnabled(False)
            self.status_display.clear()
            self.history_tree.clear()

    def _update_remote_banner(self) -> None:
        """Update remote mode banner from session effective state."""
        if not self._active_session:
            self.remote_banner.update_state("local")
            return
        from session import session_runtime_uid
        uid = session_runtime_uid(self._active_session)
        rc = self.facade.get_remote_control_settings(uid)
        if rc:
            eff = rc.get("effective", {})
            self.remote_banner.update_state(
                eff.get("execution_target", "local"),
                eff.get("host_alias"),
                eff.get("remote_project_root"),
            )
        else:
            self.remote_banner.update_state("local")

    def _refresh_all(self):
        """Обновляет статус и (если это git-репо) историю."""
        if not self._active_session:
            return

        self.status_display.setPlainText("Fetching git status...")

        async def _do():
            try:
                status_res = await self.facade.git_service.status_text(self._active_session)
                if isinstance(status_res, tuple) and len(status_res) == 2:
                    code, text = status_res
                    if str(text or "").strip() == "git unavailable for this target":
                        self._is_git_repo = False
                        self._on_status_received("git unavailable for this target")
                        self.history_tree.clear()
                        return
                    if int(code) == 0:
                        self._is_git_repo = True
                        self._on_status_received(str(text or ""))
                    else:
                        self._is_git_repo = False
                        self._on_status_received(f"Not a git repository or git error (code {code}):\n{text}")
                        return
                else:
                    self._is_git_repo = True
                    self._on_status_received(str(status_res or ""))
            except Exception as e:
                self._is_git_repo = False
                self._on_error(str(e))
                return

            # Загружаем историю только если это git-репо
            if self._is_git_repo:
                try:
                    history_res = await self.facade.git_service.log(self._active_session)
                    code, output = history_res
                    if int(code) == 0:
                        self._on_history_received(str(output or ""))
                    else:
                        self.history_tree.clear()
                except Exception as e:
                    self.logger.warning("Failed to load git history: %s", e)
                    self.history_tree.clear()

        ensure_async(_do(), parent=self)

    def refresh_status(self):
        """Асинхронно запрашивает и отображает статус Git."""
        if not self._active_session:
            return
        self._refresh_all()

    def refresh_history(self):
        """Асинхронно запрашивает и отображает историю Git."""
        if not self._active_session or not self._is_git_repo:
            return

        async def _do_history():
            try:
                history_res = await self.facade.git_service.log(self._active_session)
                code, output = history_res
                if int(code) == 0:
                    self._on_history_received(str(output or ""))
                else:
                    self.history_tree.clear()
            except Exception as e:
                self.logger.warning("Failed to load git history: %s", e)
                self.history_tree.clear()

        ensure_async(_do_history(), parent=self)

    @Slot(str)
    def _on_status_received(self, status: str):
        self.status_display.setPlainText(status)

    @Slot(str)
    def _on_history_received(self, history: str):
        # Очищаем дерево
        self.history_tree.clear()

        # Разбираем вывод git log и заполняем дерево
        lines = history.strip().split('\n')
        for line in lines:
            if line.strip():
                parts = line.split('|')  # Предполагаем формат "hash|author|date|message"
                if len(parts) >= 4:
                    item = QTreeWidgetItem(self.history_tree, [parts[0][:8], parts[1], parts[2], parts[3]])
                    self.history_tree.addTopLevelItem(item)

    def show_commit_diff(self):
        """Показывает diff выбранного коммита."""
        selected_items = self.history_tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Git", "Please select a commit to view diff")
            return

        commit_hash = selected_items[0].text(0)  # Первый столбец содержит хеш

        async def _do_show_diff():
            try:
                diff_res = await self.facade.git_service.show(self._active_session, commit_hash)
                code, output = diff_res
                if int(code) == 0:
                    # Показываем diff в новом окне или диалоге
                    diff_dialog = QMessageBox(self)
                    diff_dialog.setWindowTitle(f"Diff for commit {commit_hash}")
                    diff_dialog.setText(output)
                    diff_dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
                    diff_dialog.exec()
                else:
                    self._on_error(f"Git show failed with code {code}:\n{output}")
            except Exception as e:
                self._on_error(str(e))

        ensure_async(_do_show_diff(), parent=self)

    def _on_commit_clicked(self):
        msg = self.commit_msg_input.text().strip()
        self._set_busy(True)

        async def _do_commit():
            try:
                commit_msg = msg
                commit_body = None
                if not commit_msg:
                    commit_msg, commit_body = await self._generate_commit_message()
                    if not commit_msg:
                        self._set_busy(False)
                        QTimer.singleShot(0, lambda: QMessageBox.warning(
                            self, "Git",
                            "Не удалось сгенерировать сообщение. Введите его вручную или настройте openai_api_key в config.yaml."
                        ))
                        return
                res = await self.facade.git_service.commit(
                    self._active_session, commit_msg, body=commit_body
                )
                self._on_operation_finished(res)
            except Exception as e:
                self._on_error(str(e))
            finally:
                self._set_busy(False)

        ensure_async(_do_commit(), parent=self)

    def _generate_commit_message_ui(self):
        """Генерирует сообщение коммита с помощью ИИ и заполняет поле ввода."""
        async def _do_generate():
            try:
                commit_msg, commit_body = await self._generate_commit_message()
                if commit_msg:
                    full_msg = commit_msg
                    if commit_body:
                        full_msg += "\n\n" + commit_body
                    self.commit_msg_input.setText(full_msg)
                else:
                    QMessageBox.warning(
                        self, "Git",
                        "Не удалось сгенерировать сообщение. Проверьте настройки openai_api_key в config.yaml."
                    )
            except Exception as e:
                self._on_error(str(e))

        ensure_async(_do_generate(), parent=self)

    async def _generate_commit_message(self) -> tuple[str, Optional[str]]:
        """Генерация сообщения коммита через LLM (как в боте)."""
        cfg = self.facade.config_service.config
        if not cfg or not (os.getenv("OPENAI_API_KEY") or getattr(cfg.defaults, "openai_api_key", None)):
            return "", None
        ctx = None
        if hasattr(self.facade.git_service, "get_commit_context"):
            ctx = await self.facade.git_service.get_commit_context(self._active_session)
        if not ctx:
            return "", None
        from summary import suggest_commit_message_detailed_async
        detailed = await suggest_commit_message_detailed_async(ctx, cfg)
        if not detailed:
            return "", None
        summary, body = detailed
        summary = (summary or "").strip()[:100]
        return summary, (body or "").strip() or None

    def _on_pull_clicked(self):
        self._set_busy(True)

        async def _do_pull():
            try:
                res = await self.facade.git_service.pull(self._active_session)
                self._on_operation_finished(res)
            except Exception as e:
                self._on_error(str(e))
            finally:
                self._set_busy(False)

        ensure_async(_do_pull(), parent=self)

    def _on_push_clicked(self):
        self._set_busy(True)

        async def _do_push():
            try:
                res = await self.facade.git_service.push(self._active_session)
                self._on_operation_finished(res)
            except Exception as e:
                self._on_error(str(e))
            finally:
                self._set_busy(False)

        ensure_async(_do_push(), parent=self)

    def _on_stash_clicked(self):
        self._set_busy(True)

        async def _do_stash():
            try:
                res = await self.facade.git_service.stash(self._active_session)
                self._on_operation_finished(res)
            except Exception as e:
                self._on_error(str(e))
            finally:
                self._set_busy(False)

        ensure_async(_do_stash(), parent=self)

    def _on_stash_pop_clicked(self):
        self._set_busy(True)

        async def _do_stash_pop():
            try:
                res = await self.facade.git_service.stash_pop(self._active_session)
                self._on_operation_finished(res)
            except Exception as e:
                self._on_error(str(e))
            finally:
                self._set_busy(False)

        ensure_async(_do_stash_pop(), parent=self)

    def _on_branch_create_clicked(self):
        # Запрашиваем имя новой ветки у пользователя
        from PySide6.QtWidgets import QInputDialog
        branch_name, ok = QInputDialog.getText(
            self, "Create Branch", "Enter new branch name:"
        )
        if ok and branch_name:
            self._set_busy(True)

            async def _do_branch_create():
                try:
                    res = await self.facade.git_service.branch_create(self._active_session, branch_name)
                    self._on_operation_finished(res)
                except Exception as e:
                    self._on_error(str(e))
                finally:
                    self._set_busy(False)

            ensure_async(_do_branch_create(), parent=self)

    def _on_branch_switch_clicked(self):
        # Запрашиваем имя ветки для переключения
        from PySide6.QtWidgets import QInputDialog
        branch_name, ok = QInputDialog.getText(
            self, "Switch Branch", "Enter branch name to switch to:"
        )
        if ok and branch_name:
            self._set_busy(True)

            async def _do_branch_switch():
                try:
                    res = await self.facade.git_service.checkout(self._active_session, branch_name)
                    self._on_operation_finished(res)
                except Exception as e:
                    self._on_error(str(e))
                finally:
                    self._set_busy(False)

            ensure_async(_do_branch_switch(), parent=self)

    @Slot(object)
    def _on_operation_finished(self, result: tuple[int, str]):
        code, output = result
        # Отложить UI-обновление, чтобы избежать конфликта с qasync (Cannot enter into task)

        def _show():
            if code == 0:
                QMessageBox.information(self, "Git Success", output if output else "Operation completed successfully.")
                self.commit_msg_input.clear()
                self.refresh_status()
                self.refresh_history()
            else:
                QMessageBox.critical(self, "Git Error", f"Operation failed with code {code}:\n{output}")
        QTimer.singleShot(0, _show)

    @Slot(str)
    def _on_error(self, error_msg: str):
        self.logger.error(f"Git operation error: {error_msg}")
        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Git System Error", f"An unexpected error occurred:\n{error_msg}"))

    def _set_busy(self, busy: bool):
        """Блокирует/разблокирует кнопки во время операций."""
        # Блокируем все кнопки на всех вкладках
        self.refresh_btn.setEnabled(not busy)
        self.refresh_history_btn.setEnabled(not busy)
        self.show_diff_btn.setEnabled(not busy)
        self.commit_btn.setEnabled(not busy)
        self.generate_msg_btn.setEnabled(not busy)
        self.pull_btn.setEnabled(not busy)
        self.push_btn.setEnabled(not busy)
        self.stash_btn.setEnabled(not busy)
        self.stash_pop_btn.setEnabled(not busy)
        self.branch_create_btn.setEnabled(not busy)
        self.branch_switch_btn.setEnabled(not busy)

        if busy:
            self.setCursor(Qt.CursorShape.WaitCursor)
        else:
            self.unsetCursor()
