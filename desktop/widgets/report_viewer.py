from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Optional, List, Dict, Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFrame,
    QListWidget, QListWidgetItem, QTextBrowser, QMessageBox,
    QSplitter
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
try:
    from PySide6.QtPrintSupport import QPrinter
except ImportError:
    QPrinter = None

from i18n import t
from sessions.session_state_access import get_active_mode
from utils.html_renderer import ansi_to_html
from utils.paths import cli_proxy_artifact_path

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade, AppNotification


class ReportViewerWidget(QWidget):
    """Виджет для генерации, просмотра и экспорта отчетов."""

    def __init__(
        self,
        facade: ApplicationFacade,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.facade = facade
        self.logger = logging.getLogger(__name__)
        self._active_session_uid: Optional[str] = None
        self._reports_dir: Optional[str] = None

        self._theme_colors: Dict[str, str] = {}
        self._setup_ui()

        # Подписка на уведомления фасада
        self._unsubscribe = self.facade.subscribe(self._on_facade_notification)

    def _resolve_session(self, session_uid: Optional[str]) -> Any:
        token = str(session_uid or "").strip()
        if not token:
            return None

        def _usable(session: Any) -> bool:
            workdir = getattr(session, "workdir", None)
            return isinstance(workdir, str) and bool(workdir.strip())

        getter = getattr(self.facade.session_service, "get_session_by_uid", None)
        if not callable(getter):
            return None
        session = getter(token)
        if _usable(session):
            resolved_id = str(getattr(session, "id", "") or "")
            raw_uid = getattr(getattr(session, "conversation_scope", None), "session_uid", None)
            resolved_uid = raw_uid.strip() if isinstance(raw_uid, str) else ""
            if token == resolved_uid or (not resolved_uid and token == resolved_id):
                return session
        return None

    def set_theme_colors(self, colors: Dict[str, str]):
        """Обновляет цвета темы для рендеринга HTML-отчётов."""
        self._theme_colors.update(colors)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Панель инструментов
        toolbar = QFrame()
        toolbar.setObjectName("report_toolbar")
        toolbar.setFixedHeight(50)
        toolbar_layout = QHBoxLayout(toolbar)

        lang = self.facade.ui_language
        self.gen_md_btn = QPushButton(t("desktop.report.btn.gen_md", lang))
        self.gen_md_btn.clicked.connect(lambda: self._generate_report("md"))

        self.gen_pdf_btn = QPushButton(t("desktop.report.btn.gen_pdf", lang))
        if QPrinter is None:
            self.gen_pdf_btn.setEnabled(False)
            self.gen_pdf_btn.setToolTip(t("desktop.report.tooltip.pdf_unavailable", lang))
        self.gen_pdf_btn.clicked.connect(lambda: self._generate_report("pdf"))

        self.refresh_btn = QPushButton(t("desktop.report.btn.refresh", lang))
        self.refresh_btn.clicked.connect(self.refresh_history)

        self.sys_info_btn = QPushButton(t("desktop.report.btn.sys_info", lang))
        self.sys_info_btn.clicked.connect(self._show_system_info)

        toolbar_layout.addWidget(self.gen_md_btn)
        toolbar_layout.addWidget(self.gen_pdf_btn)
        toolbar_layout.addStretch()
        toolbar_layout.addWidget(self.sys_info_btn)
        toolbar_layout.addWidget(self.refresh_btn)

        layout.addWidget(toolbar)

        # Основная область со сплиттером
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # Список отчетов
        self.history_list = QListWidget()
        self.history_list.setFixedWidth(250)
        self.history_list.itemSelectionChanged.connect(self._on_report_selected)

        # Просмотр отчета
        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(True)
        self.viewer.setAcceptRichText(True)

        self.splitter.addWidget(self.history_list)
        self.splitter.addWidget(self.viewer)

        layout.addWidget(self.splitter)

    def set_session(self, session_uid: Optional[str]):
        """Обновление активной сессии."""
        self._active_session_uid = session_uid
        if session_uid:
            # Определяем директорию отчетов (в workdir сессии)
            session = self._resolve_session(session_uid)
            if session:
                self._reports_dir = cli_proxy_artifact_path(session.workdir, ".manager_reports")
                os.makedirs(self._reports_dir, exist_ok=True)
                self.refresh_history()
                self.setEnabled(True)
            else:
                self.setEnabled(False)
        else:
            self.setEnabled(False)
            self.history_list.clear()
            self.viewer.clear()

    def refresh_history(self):
        """Загрузка списка файлов из директории отчетов."""
        if not self._reports_dir or not os.path.exists(self._reports_dir):
            return

        self.history_list.clear()
        try:
            files = []
            for entry in os.scandir(self._reports_dir):
                if entry.is_file() and entry.name.lower().endswith(('.md', '.pdf')):
                    files.append(entry)

            # Сортировка по времени изменения (новые сверху)
            files.sort(key=lambda e: e.stat().st_mtime, reverse=True)

            for entry in files:
                item = QListWidgetItem(entry.name)
                item.setData(Qt.ItemDataRole.UserRole, entry.path)
                self.history_list.addItem(item)
        except Exception:
            self.logger.exception("failed to refresh report history")

    def _on_report_selected(self):
        """Отображение содержимого выбранного отчета."""
        items = self.history_list.selectedItems()
        if not items:
            return

        path = items[0].data(Qt.ItemDataRole.UserRole)
        if not os.path.exists(path):
            self.viewer.setPlainText(t("desktop.report.msg.file_not_found", self.facade.ui_language))
            return

        if path.lower().endswith(".md"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                # Мы можем использовать markdown-it-py если он есть,
                # но для простоты воспользуемся тем, что QTextBrowser понимает базовый Markdown/HTML
                # utils.ansi_to_html превращает markdown в HTML через markdown-it-py
                self.viewer.setHtml(ansi_to_html(content, theme_colors=self._theme_colors or None))
            except Exception as e:
                self.viewer.setPlainText(
                    t("desktop.report.msg.read_error", self.facade.ui_language, error=e)
                )
        elif path.lower().endswith(".pdf"):
            self.viewer.setHtml(f"<b>PDF File:</b> {os.path.basename(path)}<br/><br/>"
                                f"Please open this file in an external viewer.")

    def _generate_report(self, fmt: str):
        """Генерация отчета из текущего плана Manager Mode."""
        if not self._active_session_uid:
            return

        lang = self.facade.ui_language
        plan = self.facade.get_manager_plan(self._active_session_uid)
        if not plan:
            QMessageBox.warning(self, t("common.error", lang), t("desktop.report.msg.no_plan", lang))
            return

        md_content = self._build_plan_markdown(plan)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"report_{timestamp}.md"
        path = os.path.join(self._reports_dir, filename)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(md_content)

            if fmt == "pdf" and QPrinter:
                pdf_path = path.replace(".md", ".pdf")
                self._export_to_pdf(md_content, pdf_path)
                msg = t("desktop.report.msg.saved_pdf", lang, filename=os.path.basename(pdf_path))
            else:
                msg = t("desktop.report.msg.saved_md", lang, filename=os.path.basename(path))

            self.refresh_history()
            QMessageBox.information(self, t("desktop.report.msg.success_title", lang), msg)
        except Exception as e:
            self.logger.exception("failed to generate report")
            QMessageBox.critical(
                self, t("common.error", lang),
                t("desktop.report.msg.gen_error", lang, error=e)
            )

    def _build_plan_markdown(self, plan: Any) -> str:
        """Сборка Markdown-текста на основе ProjectPlan."""
        lines = []
        lines.append(f"# Project Report: {plan.project_goal}")
        lines.append(f"**Created:** {plan.created_at}")
        lines.append(f"**Updated:** {plan.updated_at}")
        lines.append(f"**Status:** {plan.status.upper()}")
        lines.append("")

        if plan.analysis:
            lines.append("## Analysis")
            lines.append(f"**Current State:** {plan.analysis.current_state}")
            lines.append("")
            lines.append("### Remaining Work")
            for item in plan.analysis.remaining_work:
                lines.append(f"- {item}")
            lines.append("")

        lines.append("## Task Tree")

        # Строим дерево как в ManagerPanel
        tasks_dict = {task.id: task for task in plan.tasks}
        dependents: Dict[str, List[str]] = {task.id: [] for task in plan.tasks}
        roots = []
        for task in plan.tasks:
            if not task.depends_on:
                roots.append(task.id)
            else:
                for dep_id in task.depends_on:
                    if dep_id in dependents:
                        dependents[dep_id].append(task.id)
                    else:
                        if task.id not in roots:
                            roots.append(task.id)

        added_ids = set()

        def add_task_md(task_id: str, level: int):
            if task_id in added_ids:
                return
            task = tasks_dict.get(task_id)
            if not task:
                return

            indent = "  " * level
            lines.append(f"{indent}- **[{task.status.upper()}]** {task.title} `({task.id})`")
            if task.description:
                lines.append(f"{indent}  _{task.description}_")

            added_ids.add(task_id)
            for child_id in dependents.get(task_id, []):
                add_task_md(child_id, level + 1)

        for rid in roots:
            add_task_md(rid, 0)

        lines.append("")
        lines.append("---")
        lines.append("Generated by Gemini CLI Desktop")

        return "\n".join(lines)

    def _export_to_pdf(self, md_content: str, path: str):
        """Рендеринг Markdown в PDF через QTextDocument и QPrinter."""
        if not QPrinter:
            return

        doc = QTextDocument()
        doc.setHtml(ansi_to_html(md_content, theme_colors=self._theme_colors or None))

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(path)

        doc.print_(printer)

    def _on_facade_notification(self, note: AppNotification):
        """Обработка уведомлений от фасада."""
        # Можно автоматически обновлять историю при завершении задач экспорта,
        # если экспорт инициирован извне.
        if note.event in ("task:completed", "ui:mode_changed"):
            self.refresh_history()

    def _show_system_info(self):
        """Отображение системных метрик и состояния текущей сессии."""
        lang = self.facade.ui_language
        metrics = self.facade.get_metrics_snapshot()
        state_html = ""
        if self._active_session_uid:
            session = self._resolve_session(self._active_session_uid)
            if session:
                state_html = self._build_session_state_html(session)

        html = f"""
        <h1>{t("desktop.report.sysinfo.title", lang)}</h1>
        <pre>{metrics}</pre>
        {state_html}
        """
        self.viewer.setHtml(ansi_to_html(html, theme_colors=self._theme_colors or None))
        self.history_list.clearSelection()

    def _build_session_state_html(self, session: Any) -> str:
        """Сборка HTML с подробным состоянием сессии."""
        from utils.ui import format_session_title
        lang = self.facade.ui_language
        label = format_session_title(session)
        active_cli = getattr(getattr(session, "cli", None), "active_cli", getattr(session, "active_cli", None))
        active_cli = active_cli or session.tool.name

        html = f"""
        <h2>{t("desktop.report.sysinfo.active_session", lang)}: {session.id}</h2>
        <p><b>{t("desktop.report.sysinfo.label", lang)}:</b> {label}</p>
        <table border="1" cellpadding="5" style="border-collapse: collapse; width: 100%;">
            <tr><th align="left">{t("desktop.report.sysinfo.col_property", lang)}</th>
                <th align="left">{t("desktop.report.sysinfo.col_value", lang)}</th></tr>
            <tr><td>{t("desktop.report.sysinfo.row_name", lang)}</td><td>{session.name or "N/A"}</td></tr>
            <tr><td>{t("desktop.report.sysinfo.row_tool", lang)}</td><td>{session.tool.name}</td></tr>
            <tr><td>{t("desktop.report.sysinfo.row_active_cli", lang)}</td><td>{active_cli}</td></tr>
            <tr><td>{t("desktop.report.sysinfo.row_workdir", lang)}</td><td>{session.workdir}</td></tr>
            <tr><td>{t("desktop.report.sysinfo.row_resume_token", lang)}</td>
                <td>{session.resume_token or "None"}</td></tr>
            <tr><td>{t("desktop.report.sysinfo.row_active_mode", lang)}</td>
                <td>{get_active_mode(session, None) or "None"}</td></tr>
            <tr><td>{t("desktop.report.sysinfo.row_tokens_used", lang)}</td>
                <td>{getattr(session, "tokens_used", 0)}</td></tr>
            <tr><td>{t("desktop.report.sysinfo.row_busy", lang)}</td><td>{session.busy}</td></tr>
        </table>
        """

        if hasattr(session, "summary") and session.summary:
            html += f"<h3>{t('desktop.report.sysinfo.summary', lang)}</h3><p>{session.summary}</p>"

        return html

    def retranslate_ui(self, lang: str) -> None:
        self.gen_md_btn.setText(t("desktop.report.btn.gen_md", lang))
        self.gen_pdf_btn.setText(t("desktop.report.btn.gen_pdf", lang))
        if not self.gen_pdf_btn.isEnabled():
            self.gen_pdf_btn.setToolTip(t("desktop.report.tooltip.pdf_unavailable", lang))
        self.refresh_btn.setText(t("desktop.report.btn.refresh", lang))
        self.sys_info_btn.setText(t("desktop.report.btn.sys_info", lang))

    def closeEvent(self, event):
        self._unsubscribe()
        super().closeEvent(event)
