from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from i18n import t
from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade


_POLL_INTERVAL_MS = 2000


class AdminChatSection(QGroupBox):
    """Секция Chat админ-панели: диалог, pending approvals, MEMORY.md."""

    def __init__(
        self,
        facade: ApplicationFacade,
        *,
        get_session_uid: Callable[[], Optional[str]],
        get_status_payload: Optional[Callable[[], Mapping[str, Any]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(t("desktop.adminchat.title", facade.ui_language), parent)
        self.setObjectName("admin_chat_section")
        self.facade = facade
        self._get_session_uid = get_session_uid
        self._get_status_payload = get_status_payload
        self.logger = logging.getLogger(__name__)
        self._background_tasks: set = set()

        self._last_counters: Dict[str, Any] = {"messages_count": -1, "pending_count": -1, "last_message_ts": ""}
        self._pending_items: List[Dict[str, Any]] = []
        self._memory_dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.messages_view = QPlainTextEdit()
        self.messages_view.setObjectName("admin_chat_messages_view")
        self.messages_view.setReadOnly(True)
        self.messages_view.setMinimumHeight(180)
        self.messages_view.setPlaceholderText(t("desktop.adminchat.messages_placeholder", facade.ui_language))
        layout.addWidget(self.messages_view, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.input_edit = QLineEdit()
        self.input_edit.setObjectName("admin_chat_input_edit")
        self.input_edit.setPlaceholderText(t("desktop.adminchat.input_placeholder", facade.ui_language))
        self.input_edit.returnPressed.connect(self._on_send_clicked)
        input_row.addWidget(self.input_edit, 1)
        self.send_button = QPushButton(t("desktop.adminchat.btn_send", facade.ui_language))
        self.send_button.setObjectName("admin_chat_send_button")
        self.send_button.clicked.connect(self._on_send_clicked)
        input_row.addWidget(self.send_button)
        layout.addLayout(input_row)

        self.pending_label = QLabel(t("desktop.adminchat.pending_label", facade.ui_language, count=0))
        self.pending_label.setObjectName("admin_chat_pending_label")
        layout.addWidget(self.pending_label)

        self.pending_list = QListWidget()
        self.pending_list.setObjectName("admin_chat_pending_list")
        self.pending_list.setMinimumHeight(100)
        layout.addWidget(self.pending_list)

        pending_row = QHBoxLayout()
        pending_row.setSpacing(8)
        self.approve_button = QPushButton(t("desktop.adminchat.btn_approve", facade.ui_language))
        self.approve_button.setObjectName("admin_chat_approve_button")
        self.approve_button.clicked.connect(self._on_approve_clicked)
        pending_row.addWidget(self.approve_button)
        self.reject_button = QPushButton(t("desktop.adminchat.btn_reject", facade.ui_language))
        self.reject_button.setObjectName("admin_chat_reject_button")
        self.reject_button.clicked.connect(self._on_reject_clicked)
        pending_row.addWidget(self.reject_button)
        pending_row.addStretch(1)
        layout.addLayout(pending_row)

        memory_title = QLabel("MEMORY.md:")
        memory_title.setObjectName("admin_chat_memory_title")
        layout.addWidget(memory_title)

        self.memory_edit = QPlainTextEdit()
        self.memory_edit.setObjectName("admin_chat_memory_edit")
        self.memory_edit.setMinimumHeight(100)
        self.memory_edit.setPlaceholderText(t("desktop.adminchat.memory_notes_placeholder", facade.ui_language))
        self.memory_edit.textChanged.connect(self._on_memory_changed)
        layout.addWidget(self.memory_edit)

        memory_row = QHBoxLayout()
        memory_row.setSpacing(8)
        self.memory_reload_button = QPushButton(t("desktop.adminchat.btn_reload", facade.ui_language))
        self.memory_reload_button.setObjectName("admin_chat_memory_reload_button")
        self.memory_reload_button.clicked.connect(self._reload_memory)
        memory_row.addWidget(self.memory_reload_button)
        self.memory_save_button = QPushButton(t("desktop.adminchat.btn_save", facade.ui_language))
        self.memory_save_button.setObjectName("admin_chat_memory_save_button")
        self.memory_save_button.clicked.connect(self._on_save_memory_clicked)
        memory_row.addWidget(self.memory_save_button)
        memory_row.addStretch(1)
        layout.addLayout(memory_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("admin_chat_status_label")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_tick)

    # ---------- public ----------

    def start(self) -> None:
        self._poll_timer.start()
        self._poll_tick(force=True)

    def stop(self) -> None:
        self._poll_timer.stop()

    def on_session_changed(self) -> None:
        self._last_counters = {"messages_count": -1, "pending_count": -1, "last_message_ts": ""}
        self.messages_view.clear()
        self.pending_list.clear()
        self._pending_items = []
        self.pending_label.setText(t("desktop.adminchat.pending_label", self.facade.ui_language, count=0))
        self.memory_edit.blockSignals(True)
        self.memory_edit.clear()
        self.memory_edit.blockSignals(False)
        self._memory_dirty = False
        self._update_status("")
        if self._poll_timer.isActive():
            self._poll_tick(force=True)

    # ---------- polling ----------

    def _poll_tick(self, *, force: bool = False) -> None:
        session_uid = self._active_uid()
        if not session_uid:
            return
        counters = self._extract_counters()
        if counters is None:
            if force:
                self._reload_messages(session_uid)
                self._reload_pending(session_uid)
                if not self._memory_dirty:
                    self._reload_memory()
            return

        prev = self._last_counters
        changed_messages = (
            counters.get("messages_count") != prev.get("messages_count")
            or counters.get("last_message_ts") != prev.get("last_message_ts")
        )
        changed_pending = counters.get("pending_count") != prev.get("pending_count")

        if force or changed_messages:
            self._reload_messages(session_uid)
        if force or changed_pending:
            self._reload_pending(session_uid)
        if force and not self._memory_dirty:
            self._reload_memory()

        self._last_counters = dict(counters)

    def _extract_counters(self) -> Optional[Dict[str, Any]]:
        if self._get_status_payload is None:
            return None
        try:
            payload = self._get_status_payload() or {}
        except Exception:
            self.logger.exception("admin_chat: failed to read status payload")
            return None
        chat = payload.get("chat") if isinstance(payload, Mapping) else None
        if not isinstance(chat, Mapping):
            return None
        return {
            "messages_count": chat.get("messages_count"),
            "pending_count": chat.get("pending_count"),
            "last_message_ts": str(chat.get("last_message_ts") or ""),
        }

    # ---------- messages ----------

    def _reload_messages(self, session_uid: Optional[str] = None) -> None:
        session_uid = session_uid or self._active_uid()
        if not session_uid:
            return
        loader = getattr(self.facade, "get_admin_chat_messages", None)
        if not callable(loader):
            return
        try:
            result = loader(session_uid)
        except Exception as exc:
            self.logger.exception("admin_chat: get_admin_chat_messages failed")
            self._update_status(t("desktop.adminchat.status_load_messages_error", self.facade.ui_language, error=str(exc)))
            return
        if not isinstance(result, Mapping) or not result.get("ok"):
            self._update_status(t("desktop.adminchat.status_messages_unavailable", self.facade.ui_language,
                                  error=result.get('error') if isinstance(result, Mapping) else 'unknown'))
            return
        messages = result.get("messages") or []
        self._render_messages(messages)

    def _render_messages(self, messages: List[Any]) -> None:
        lines: List[str] = []
        for entry in messages:
            if not isinstance(entry, Mapping):
                continue
            role = str(entry.get("role") or "user")
            text = str(entry.get("text") or "")
            ts = str(entry.get("ts") or "")
            intent_type = str(entry.get("intent_type") or "")
            prefix = f"[{ts}] " if ts else ""
            marker = ""
            if intent_type == "intent_autopilot_executed":
                marker = " [autopilot ✓]"
            elif intent_type == "intent_autopilot_blocked":
                marker = " [autopilot ⚠]"
            lines.append(f"{prefix}{role}{marker}: {text}")
        self.messages_view.setPlainText("\n".join(lines))
        scrollbar = self.messages_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_send_clicked(self) -> None:
        session_uid = self._active_uid()
        if not session_uid:
            self._update_status(t("desktop.adminchat.status_select_session", self.facade.ui_language))
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        sender = getattr(self.facade, "post_admin_chat_message", None)
        if not callable(sender):
            return
        self.input_edit.clear()
        self.send_button.setEnabled(False)
        self._update_status(t("desktop.adminchat.status_sending", self.facade.ui_language))

        async def _do_send() -> None:
            try:
                result = await sender(session_uid, text=text)
            except Exception as exc:
                self.logger.exception("admin_chat: post_admin_chat_message failed")
                self._update_status(t("desktop.adminchat.status_send_error", self.facade.ui_language, error=str(exc)))
                return
            finally:
                self.send_button.setEnabled(True)
            self._handle_send_result(result)
            self._reload_messages(session_uid)
            self._reload_pending(session_uid)

        ensure_async(_do_send(), parent=self)

    def _handle_send_result(self, result: Any) -> None:
        if not isinstance(result, Mapping):
            self._update_status(t("desktop.adminchat.status_unexpected_response", self.facade.ui_language))
            return
        if not result.get("ok"):
            self._update_status(
                t("desktop.adminchat.status_error", self.facade.ui_language,
                  error=result.get('error') or 'unknown')
            )
            return
        intent = result.get("intent") or {}
        intent_type = str(intent.get("type") or "") if isinstance(intent, Mapping) else ""
        reply = str(result.get("reply_text") or "").strip()
        if bool(result.get("auto_exec")):
            exec_result = result.get("exec_result") or {}
            exit_code = exec_result.get("exit_code") if isinstance(exec_result, Mapping) else None
            target_kind = exec_result.get("target_kind") if isinstance(exec_result, Mapping) else None
            parts = [
                t("desktop.adminchat.status_autopilot_executed", self.facade.ui_language,
                  kind=target_kind or '?')
            ]
            if exit_code is not None:
                parts.append(f"exit={exit_code}")
            self._update_status(" · ".join(parts))
            return
        blocked = str(result.get("autopilot_blocked") or "").strip()
        if blocked:
            self._update_status(
                t("desktop.adminchat.status_autopilot_blocked", self.facade.ui_language, reason=blocked)
            )
            return
        if intent_type and intent_type != "answer":
            self._update_status(t("desktop.adminchat.status_intent", self.facade.ui_language, intent_type=intent_type))
        elif reply:
            self._update_status("")
        else:
            self._update_status(t("desktop.adminchat.status_answer_accepted", self.facade.ui_language))

    # ---------- pending ----------

    def _reload_pending(self, session_uid: Optional[str] = None) -> None:
        session_uid = session_uid or self._active_uid()
        if not session_uid:
            return
        loader = getattr(self.facade, "get_admin_chat_pending", None)
        if not callable(loader):
            return
        try:
            result = loader(session_uid)
        except Exception as exc:
            self.logger.exception("admin_chat: get_admin_chat_pending failed")
            self._update_status(t("desktop.adminchat.status_load_pending_error", self.facade.ui_language, error=str(exc)))
            return
        if not isinstance(result, Mapping) or not result.get("ok"):
            self._update_status(t("desktop.adminchat.status_pending_unavailable", self.facade.ui_language,
                                  error=result.get('error') if isinstance(result, Mapping) else 'unknown'))
            return
        items = list(result.get("items") or [])
        self._pending_items = [dict(item) for item in items if isinstance(item, Mapping)]
        self._render_pending()

    def _render_pending(self) -> None:
        self.pending_list.clear()
        for item in self._pending_items:
            approval_id = str(item.get("approval_id") or "")
            intent = item.get("intent") if isinstance(item.get("intent"), Mapping) else {}
            intent_type = str(intent.get("type") or "?") if intent else "?"
            action_id = str(intent.get("action_id") or "") if intent else ""
            target = str(intent.get("target") or "") if intent else ""
            blocked = str(item.get("autopilot_blocked") or "")
            label = f"{approval_id} · {intent_type}"
            if action_id:
                label += f" · {action_id}"
            if target:
                label += f" → {target}"
            if blocked:
                label += f"  ⚠ auto-exec blocked: {blocked}"
            list_item = QListWidgetItem(label)
            list_item.setData(Qt.ItemDataRole.UserRole, approval_id)
            self.pending_list.addItem(list_item)
        self.pending_label.setText(t("desktop.adminchat.pending_label", self.facade.ui_language, count=len(self._pending_items)))

    def _selected_approval_id(self) -> Optional[str]:
        item = self.pending_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value).strip() if value else None

    def _on_approve_clicked(self) -> None:
        session_uid = self._active_uid()
        approval_id = self._selected_approval_id()
        if not session_uid or not approval_id:
            self._update_status(t("desktop.adminchat.status_select_pending", self.facade.ui_language))
            return
        approver = getattr(self.facade, "approve_admin_chat_pending", None)
        if not callable(approver):
            return
        self.approve_button.setEnabled(False)
        self._update_status(
            t("desktop.adminchat.status_approving", self.facade.ui_language, id=approval_id)
        )

        async def _do_approve() -> None:
            try:
                result = await approver(session_uid, approval_id=approval_id)
            except Exception as exc:
                self.logger.exception("admin_chat: approve_admin_chat_pending failed")
                self._update_status(t("desktop.adminchat.status_approve_error", self.facade.ui_language, error=str(exc)))
                return
            finally:
                self.approve_button.setEnabled(True)
            self._handle_approve_result(result)
            self._reload_messages(session_uid)
            self._reload_pending(session_uid)

        ensure_async(_do_approve(), parent=self)

    def _handle_approve_result(self, result: Any) -> None:
        if not isinstance(result, Mapping):
            self._update_status(t("desktop.adminchat.status_unexpected_approve", self.facade.ui_language))
            return
        if not result.get("ok"):
            self._update_status(t("desktop.adminchat.status_approve_error_result", self.facade.ui_language,
                                  error=result.get('error') or 'unknown'))
            return
        exit_code = result.get("exit_code")
        target_kind = result.get("target_kind") or "?"
        duration = result.get("duration_ms")
        parts = [t("desktop.adminchat.status_executed", self.facade.ui_language, kind=target_kind)]
        if exit_code is not None:
            parts.append(f"exit={exit_code}")
        if duration is not None:
            parts.append(f"{duration}ms")
        self._update_status(" · ".join(parts))

    def _on_reject_clicked(self) -> None:
        session_uid = self._active_uid()
        approval_id = self._selected_approval_id()
        if not session_uid or not approval_id:
            self._update_status(t("desktop.adminchat.status_select_pending", self.facade.ui_language))
            return
        rejector = getattr(self.facade, "reject_admin_chat_pending", None)
        if not callable(rejector):
            return
        try:
            result = rejector(session_uid, approval_id=approval_id)
        except Exception as exc:
            self.logger.exception("admin_chat: reject_admin_chat_pending failed")
            self._update_status(t("desktop.adminchat.status_reject_error", self.facade.ui_language, error=str(exc)))
            return
        if isinstance(result, Mapping) and result.get("ok"):
            self._update_status(t("desktop.adminchat.status_rejected", self.facade.ui_language, id=approval_id))
        else:
            err = result.get("error") if isinstance(result, Mapping) else "unknown"
            self._update_status(t("desktop.adminchat.status_reject_fail", self.facade.ui_language, error=str(err)))
        self._reload_pending(session_uid)

    # ---------- memory ----------

    def _reload_memory(self) -> None:
        session_uid = self._active_uid()
        if not session_uid:
            return
        loader = getattr(self.facade, "get_admin_chat_memory_md", None)
        if not callable(loader):
            return
        try:
            result = loader(session_uid)
        except Exception as exc:
            self.logger.exception("admin_chat: get_admin_chat_memory_md failed")
            self._update_status(t("desktop.adminchat.status_load_memory_error", self.facade.ui_language, error=str(exc)))
            return
        if not isinstance(result, Mapping) or not result.get("ok"):
            return
        text = str(result.get("text") or "")
        self.memory_edit.blockSignals(True)
        self.memory_edit.setPlainText(text)
        self.memory_edit.blockSignals(False)
        self._memory_dirty = False

    def _on_memory_changed(self) -> None:
        self._memory_dirty = True

    def _on_save_memory_clicked(self) -> None:
        session_uid = self._active_uid()
        if not session_uid:
            self._update_status(t("desktop.adminchat.status_select_session", self.facade.ui_language))
            return
        saver = getattr(self.facade, "save_admin_chat_memory_md", None)
        if not callable(saver):
            return
        text = self.memory_edit.toPlainText()
        try:
            result = saver(session_uid, text=text)
        except Exception as exc:
            self.logger.exception("admin_chat: save_admin_chat_memory_md failed")
            QMessageBox.warning(self, t("desktop.adminchat.memory_title", self.facade.ui_language),
                                t("desktop.adminchat.status_save_error_msg", self.facade.ui_language, error=str(exc)))
            return
        if isinstance(result, Mapping) and result.get("ok"):
            self._memory_dirty = False
            self._update_status(t("desktop.adminchat.status_memory_saved", self.facade.ui_language))
        else:
            err = result.get("error") if isinstance(result, Mapping) else "unknown"
            self._update_status(t("desktop.adminchat.status_memory_error", self.facade.ui_language, error=str(err)))

    # ---------- i18n ----------

    def retranslate_ui(self, lang: str) -> None:
        self.setTitle(t("desktop.adminchat.title", lang))
        self.messages_view.setPlaceholderText(t("desktop.adminchat.messages_placeholder", lang))
        self.input_edit.setPlaceholderText(t("desktop.adminchat.input_placeholder", lang))
        self.send_button.setText(t("desktop.adminchat.btn_send", lang))
        self.pending_label.setText(t("desktop.adminchat.pending_label", lang, count=len(self._pending_items)))
        self.approve_button.setText(t("desktop.adminchat.btn_approve", lang))
        self.reject_button.setText(t("desktop.adminchat.btn_reject", lang))
        self.memory_reload_button.setText(t("desktop.adminchat.btn_reload", lang))
        self.memory_save_button.setText(t("desktop.adminchat.btn_save", lang))
        self.memory_edit.setPlaceholderText(t("desktop.adminchat.memory_notes_placeholder", lang))

    # ---------- helpers ----------

    def _active_uid(self) -> Optional[str]:
        try:
            uid = self._get_session_uid()
        except Exception:
            return None
        uid = str(uid or "").strip()
        return uid or None

    def _update_status(self, text: str) -> None:
        self.status_label.setText(str(text or ""))
