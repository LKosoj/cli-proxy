from dataclasses import dataclass, field
from typing import Any

from app.services.telegram_ui_scope import TelegramUiKey


@dataclass
class ChatUiState:
    pending: dict[TelegramUiKey, Any] = field(default_factory=dict)
    pending_prompt_messages: dict[Any, int] = field(default_factory=dict)
    state_menu: dict[TelegramUiKey, list] = field(default_factory=dict)
    close_menu: dict[TelegramUiKey, list] = field(default_factory=dict)
    pending_new_tool: dict[TelegramUiKey, str] = field(default_factory=dict)
    dirs_menu: dict[TelegramUiKey, list] = field(default_factory=dict)
    state_menu_page: dict[TelegramUiKey, int] = field(default_factory=dict)
    dirs_base: dict[TelegramUiKey, str] = field(default_factory=dict)
    dirs_page: dict[TelegramUiKey, int] = field(default_factory=dict)
    dirs_root: dict[TelegramUiKey, str] = field(default_factory=dict)
    dirs_mode: dict[TelegramUiKey, str] = field(default_factory=dict)
    pending_dir_input: dict[TelegramUiKey, bool] = field(default_factory=dict)
    pending_dir_create: dict[TelegramUiKey, str] = field(default_factory=dict)
    pending_git_clone: dict[TelegramUiKey, str] = field(default_factory=dict)
    restore_offered: dict[int, bool] = field(default_factory=dict)
    files_menu: dict[TelegramUiKey, list] = field(default_factory=dict)
    files_dir: dict[TelegramUiKey, str] = field(default_factory=dict)
    files_page: dict[TelegramUiKey, int] = field(default_factory=dict)
    files_entries: dict[TelegramUiKey, list] = field(default_factory=dict)
    files_pending_delete: dict[TelegramUiKey, str] = field(default_factory=dict)
    files_pending_upload: dict[TelegramUiKey, dict[str, Any]] = field(default_factory=dict)
    files_pending_upload_tasks: dict[TelegramUiKey, Any] = field(default_factory=dict)
    files_pending_rename: dict[TelegramUiKey, dict[str, Any]] = field(default_factory=dict)
    files_pending_rename_tasks: dict[TelegramUiKey, Any] = field(default_factory=dict)
    media_group_images: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    media_group_tasks: dict[tuple[int, str], Any] = field(default_factory=dict)
    media_group_documents: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    media_group_document_tasks: dict[tuple[int, str], Any] = field(default_factory=dict)
    message_buffer: dict[int, list[str]] = field(default_factory=dict)
    message_buffer_user_id: dict[int, int] = field(default_factory=dict)
    message_buffer_direct_messages_topic_id: dict[int, int] = field(default_factory=dict)
    buffer_tasks: dict[int, Any] = field(default_factory=dict)
    pending_questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_ask_question_by_chat: dict[TelegramUiKey, str] = field(default_factory=dict)
    context_by_chat: dict[int, Any] = field(default_factory=dict)


@dataclass
class AppServices:
    mode_dialogs: Any
    mode_tasks: Any
    mode_session_control: Any
    mode_pipeline: Any
    mode_agent_runtime: Any
    mode_dirs_flow: Any
    dirs_service: Any
    session_creation_service: Any
    sandbox_service: Any
    transport_service: Any
    message_buffer_service: Any
