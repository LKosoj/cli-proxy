import asyncio
from dataclasses import dataclass
import logging
import os
import time
import concurrent.futures
from typing import Dict, Optional, Any

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    WebAppInfo,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ContextTypes,
)

from app.config_runtime.loader import load_validated_settings
from config import AppConfig, ToolConfig, load_config
from app.services.dotenv_loader import load_dotenv_near
from app.services.tool_availability import available_tools, is_tool_available, tool_exec
from session import Session, session_runtime_uid
from summary import summarize_text_with_reason
from tg.command_policy import OUTSIDE_TOPIC_ALLOWED_COMMANDS
from tg.command_registry import build_command_registry
from sessions.conversation_scope import ConversationScope
from sessions.session_ui import SessionUI
from app.services.git_ops_service import GitOps
from app.services.remote_git_service import RemoteGitService
from app.services.remote_shell_service import RemoteShellService
from app.services.metrics_service import Metrics
from app.services.mcp_bridge_service import MCPBridge
from miniapp import MiniAppServer
from utils.html_renderer import ansi_to_html, make_html_file
from utils.paths import is_within_root as utils_is_within_root

from agent import configure_pending_commands_store, get_pending_command, set_approval_callback

from tg.handlers import BotHandlers
from tg.callbacks import CallbackHandler
from tg.message_processor import MessageProcessor
from sessions.session_management import SessionManagement
from app.services.dirs_service import DirsService
from app.services.session_creation_service import SessionCreationService
from app.services.sandbox_service import AgentSandboxService
from app.services.lifecycle_service import build_error_handler, build_post_init, build_post_shutdown
from app.services.i18n_service import maybe_persist_user_language
from app.services.rich_draft_coordinator import RichDraftCoordinator
from app.services.telegram_transport import (
    TelegramEditOutcome,
    TelegramTransportContext,
    TelegramTransportService,
)
from app.services.message_buffer_service import MessageBufferService
from app.services.input_dispatch_service import InputDispatchService
from app.services.access_policy_service import AccessPolicyService
from app.services.app_runtime_service import AppRuntimeService
from app.services.logging_service import setup_logging
from app.services.webhook_ingress_service import WebhookIngressService
from app.services.run_recovery_executor import build_recovery_dest, build_recovery_prompt
from app.services.run_artifact_store import RunArtifactStore
from app.services.persistent_state_map import PersistentStateMap
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import AppServices, ChatUiState
from modes.sdk import DialogService
from modes.sdk import MessagingService
from modes.sdk import AgentRuntimeService
from modes.sdk import DictStateService
from modes.sdk import DirsFlowService
from modes.sdk import ModeCallbackRouterService
from modes.sdk import ModeInputRoutingService
from modes.sdk import ModeToolingService
from modes.sdk.runtime.openai_client import chat_completion
from tg.wiring import register_handlers
from app.bootstrap import build_application
from i18n import t
from utils.lang import resolve_user_lang


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

# HTML rendering of large ANSI logs is CPU-heavy and often pure-Python.
# Running it in a thread can starve the event loop due to the GIL, which looks like "polling freeze".
# For large outputs we offload conversion to a separate process.
_HTML_PROCESS_THRESHOLD_CHARS = 100_000
_HTML_PROCESS_POOL = None
_HTML_RENDER_TAIL_CHARS = 10_000
_SUMMARY_PREPARE_THRESHOLD_CHARS = 50_000
_SUMMARY_TAIL_CHARS = 50_000
_SUMMARY_WAIT_FOR_HTML_S = 5.0
_SUMMARY_TIMEOUT_S = 100.0
# Keep module-level references for monkeypatch-based tests that patch bot.ansi_to_html/make_html_file.
_HTML_RENDER_HELPERS = (ansi_to_html, make_html_file)


@dataclass(frozen=True)
class TelegramInboundRoute:
    owner_chat_id: int
    reply_chat_id: int
    message_thread_id: Optional[int]
    session_uid: Optional[str]
    session: Optional[Session]
    direct_messages_topic_id: Optional[int] = None
    unknown_thread: bool = False

    def reply_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"chat_id": int(self.reply_chat_id)}
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = int(self.message_thread_id)
        if self.direct_messages_topic_id is not None:
            kwargs["direct_messages_topic_id"] = int(self.direct_messages_topic_id)
        return kwargs


class BotApp:
    def __init__(self, config: AppConfig):
        self.config = config
        self._bg_tasks: set = set()
        self.sandbox_service = AgentSandboxService(self.config.defaults.workdir)
        self._setup_logging()
        self._configure_agent_sandbox()
        self.container = build_application(
            config,
            run_mode_pipeline_fn=self.run_mode_pipeline,
            bot_app_provider=lambda: self,
        )
        self.state_repository = self.container.state_repository
        self.mode_registry = self.container.mode_registry
        self.mode_loader = self.container.mode_loader
        self.mode_registry_service = self.container.mode_registry_service
        self.manager = self.container.session_manager
        self.mode_tasks = self.container.mode_tasks
        self.mode_session_control = self.container.session_control
        self.mode_pipeline = self.container.mode_pipeline
        self.mode_run_artifacts = self.container.run_artifacts
        self.mode_run_observability = self.container.run_observability
        self.mode_run_doctor = self.container.run_doctor
        self.mode_run_boundary_validation = self.container.run_boundary_validation
        self.mode_skill_runtime = self.container.skill_runtime
        self.mode_run_operations = self.container.run_operations_service
        self._tool_registry = self.container.plugin_registry
        self.ssh_service = self.container.ssh_service
        self.remote_control_service = self.container.remote_control_service
        self.mode_dialogs = DialogService(
            pending_questions_provider=lambda: self.ui_state.pending_questions,
        )
        self.manager.on_session_change = self._on_session_change
        # Inject _ssh_service into sessions restored before the callback was set.
        for chat_sessions in self.manager.sessions_by_chat.values():
            for session in chat_sessions.values():
                if getattr(session, "_ssh_service", None) is None:
                    session._ssh_service = self.ssh_service
        self.mode_tooling = ModeToolingService(
            execute_tool_fn=self._mode_execute_tool,
            registry_provider=self._mode_tool_registry,
        )
        self.mode_agent_runtime = AgentRuntimeService(
            interrupt_session_fn=self._interrupt_before_close,
            clear_sandbox_fn=self._clear_agent_sandbox,
            clear_session_files_fn=self._clear_agent_session_files,
            clear_session_cache_fn=self._clear_agent_session_cache,
            get_session_fn=self.manager.get,
            get_session_by_uid_fn=self._get_agent_session_by_uid_service,
        )
        self.mode_dirs_flow = DirsFlowService(
            start_flow_fn=self._start_dirs_flow_service,
            clear_flow_fn=self._clear_dirs_flow_service,
            get_mode_token_fn=self._get_dirs_flow_token_service,
        )
        self.metrics = Metrics()
        self.ui_state = ChatUiState()
        # Backward-compatible aliases kept for tests and lightweight fakes that still
        # reference pre-ui_state attributes directly.
        self.pending = self.ui_state.pending
        self.context_by_chat = self.ui_state.context_by_chat
        self.message_buffer = self.ui_state.message_buffer
        self.message_buffer_user_id = self.ui_state.message_buffer_user_id
        self.buffer_tasks = self.ui_state.buffer_tasks
        self.media_group_documents = self.ui_state.media_group_documents
        self._pending_custom_input_status_by_chat: Dict[TelegramUiKey, str] = {}
        self.dirs_service = DirsService(self)
        self.session_creation_service = SessionCreationService(self)
        self.notification_queue_service = self.container.notification_queue_service
        self.transport_service = TelegramTransportService(self)
        self.rich_draft_coordinator = RichDraftCoordinator()
        self.message_buffer_service = MessageBufferService(self)
        self.access_policy_service = AccessPolicyService(self)
        self.cli_limits_service = self.container.cli_limits_service
        self.project_registry = self.container.project_registry
        self.session_thread_repository = self.container.session_thread_repository
        self.session_thread_manager = self.container.session_thread_manager
        self.shared_http_ingress = self.container.shared_http_ingress
        self.system_event_bus = self.container.system_event_bus
        self.scheduler_service = self.container.scheduler_service
        self.security = self.container.security
        self.webhook_ingress_service = WebhookIngressService(self)
        self.mode_launch_adapter = self.container.mode_launch_adapter_service
        self.runtime_service = AppRuntimeService(self)
        self.advanced_orchestrator_service = self.container.advanced_orchestrator_service
        self.artifact_intent_service = self.container.artifact_intent_service
        self.report_history_service = self.container.report_history_service
        self.session_snapshot_report_service = self.container.session_snapshot_report_service
        self.config_service = self.container.config_service
        self.orchestrator_chat_completion = chat_completion
        self.mode_callback_router = ModeCallbackRouterService(
            mode_registry=self.mode_registry_service,
            dialogs=self.mode_dialogs,
            send_message=self._send_message,
            send_output=self.send_output,
            resolve_session=lambda chat_id, message_thread_id=None: self.resolve_telegram_scope_session(
                reply_chat_id=int(chat_id),
                message_thread_id=message_thread_id,
            ),
            get_dirs_mode_token=lambda chat_id, message_thread_id=None: str(
                self.ui_state.dirs_mode.get(self.telegram_ui_key(int(chat_id), message_thread_id), "") or ""
            ),
            clear_dirs_mode_token=lambda chat_id, message_thread_id=None: self.ui_state.dirs_mode.pop(
                self.telegram_ui_key(int(chat_id), message_thread_id),
                None,
            ),
        )
        from app.services.lint_evolution_runtime import make_session_hook as _make_lint_hook
        self.mode_input_router = ModeInputRoutingService(
            mode_registry=self.mode_registry_service,
            dialogs=self.mode_dialogs,
            send_message=self._send_message,
            send_output=self.send_output,
            lint_evolution_hook=_make_lint_hook(self.config.lint_evolution),
        )
        from tg.pending_input_ui import TelegramPendingInputUiAdapter
        self.pending_input_ui = TelegramPendingInputUiAdapter(self)
        self.input_dispatch_service = InputDispatchService(self, pending_input_ui=self.pending_input_ui)
        self.mode_runtime_registry: Dict[str, Any] = {}
        self.media_group_idle_sec: float = 20.0
        self.services = AppServices(
            mode_dialogs=self.mode_dialogs,
            mode_tasks=self.mode_tasks,
            mode_session_control=self.mode_session_control,
            mode_pipeline=self.mode_pipeline,
            mode_agent_runtime=self.mode_agent_runtime,
            mode_dirs_flow=self.mode_dirs_flow,
            dirs_service=self.dirs_service,
            session_creation_service=self.session_creation_service,
            sandbox_service=self.sandbox_service,
            transport_service=self.transport_service,
            message_buffer_service=self.message_buffer_service,
        )
        self._last_delivery_error: Optional[str] = None
        # Pending "continue or start new" decision when manager_auto_resume=false and a plan is active.
        self.manager_resume_pending = PersistentStateMap(
            self.config.defaults.state_path,
            "_manager_resume_pending",
        )
        self.mode_manager_resume_pending = DictStateService(self.manager_resume_pending)
        # Agent pending project picker flow state by chat_id.
        self.agent_project_pending_by_chat = PersistentStateMap(
            self.config.defaults.state_path,
            "_agent_project_pending_by_chat",
        )
        self.mode_agent_project_pending_by_chat = DictStateService(self.agent_project_pending_by_chat)
        self.session_ui = SessionUI(
            self.config,
            self.manager,
            self._send_message,
            self._edit_message,
            self._format_ts,
            self._short_label,
            self._clear_agent_session_cache,
            self._interrupt_before_close,
            self.mode_registry_service,
            self.is_session_allowed_for_chat,
            bot_app=self,
        )
        self._register_mode_runtimes_from_plugins()
        configure_pending_commands_store(self.config.defaults.state_path)
        set_approval_callback(self._request_command_approval)
        self.git = GitOps(
            self.config,
            self.manager,
            self._send_message,
            self._edit_message,
            self._send_document,
            self._short_label,
            self._handle_cli_input,
        )
        _remote_shell = RemoteShellService(self.ssh_service)
        self.remote_git = RemoteGitService(_remote_shell)
        self.mcp = MCPBridge(self.config, self)
        self.miniapp_server = MiniAppServer(self)
        self._html_process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        self._task_deadline_checker_task: Optional[asyncio.Task] = None
        self._shutdown_in_progress: bool = False

        # Initialize modules

        self.handlers = BotHandlers(self)
        self.callbacks = CallbackHandler(self)
        self.message_processor = MessageProcessor(self)
        self.session_management = SessionManagement(self)
        self.session_management.set_html_process_pool(self._html_process_pool)

        from tg.file_upload_handler import FileUploadHandler
        self._file_upload_handler = FileUploadHandler(self)

        # Initialize mode plugins with shared SDK services to keep modes decoupled from BotApp internals.
        self._initialize_mode_plugins()

    @staticmethod
    def _extract_message_thread_id(update: Optional[Update]) -> Optional[int]:
        if update is None:
            return None
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        try:
            raw_value = getattr(message, "message_thread_id", None)
        except Exception:
            raw_value = None
        try:
            thread_id = int(raw_value) if raw_value is not None else 0
        except Exception:
            thread_id = 0
        return thread_id if thread_id > 0 else None

    @staticmethod
    def _extract_direct_messages_topic_id(update: Optional[Update]) -> Optional[int]:
        if update is None:
            return None
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        raw_value = None
        for attr_name in ("direct_messages_topic", "direct_message_topic"):
            topic = getattr(message, attr_name, None)
            raw_value = getattr(topic, "topic_id", None)
            if raw_value is not None:
                break
        if raw_value is None:
            api_kwargs = getattr(message, "api_kwargs", None)
            if isinstance(api_kwargs, dict):
                raw_value = api_kwargs.get("direct_messages_topic_id")
                if raw_value is None:
                    raw_value = api_kwargs.get("direct_message_topic_id")
                topic_data = api_kwargs.get("direct_messages_topic")
                if raw_value is None and isinstance(topic_data, dict):
                    raw_value = topic_data.get("topic_id")
        try:
            topic_id = int(raw_value) if raw_value is not None else 0
        except Exception:
            topic_id = 0
        return topic_id if topic_id > 0 else None

    @staticmethod
    def telegram_ui_key(chat_id: int, message_thread_id: Optional[int] = None) -> TelegramUiKey:
        return TelegramUiKey.from_parts(chat_id, message_thread_id)

    def telegram_ui_key_from_update(self, update: Optional[Update]) -> Optional[TelegramUiKey]:
        return TelegramUiKey.from_update(update)

    def telegram_ui_key_from_query(self, query: Any) -> Optional[TelegramUiKey]:
        return TelegramUiKey.from_query(query)

    def telegram_ui_key_from_route(
        self,
        route: Optional[TelegramInboundRoute],
        *,
        fallback_chat_id: int,
    ) -> TelegramUiKey:
        return TelegramUiKey.from_route(route, fallback_chat_id=fallback_chat_id)

    def build_telegram_reply_dest(
        self,
        session: Optional[Session],
        chat_id: int,
        *,
        user_id: Optional[int] = None,
        direct_messages_topic_id: Optional[int] = None,
    ) -> dict[str, Any]:
        dest: dict[str, Any] = {"kind": "telegram", "chat_id": int(chat_id)}
        if user_id is not None:
            dest["user_id"] = int(user_id)
        if direct_messages_topic_id is not None:
            dest["direct_messages_topic_id"] = int(direct_messages_topic_id)
        scope = getattr(session, "conversation_scope", None)
        if (
            isinstance(scope, ConversationScope)
            and scope.message_thread_id is not None
            and int(scope.chat_id) == int(chat_id)
        ):
            dest["message_thread_id"] = int(scope.message_thread_id)
        return dest

    def build_telegram_transport_context(
        self,
        context: Any,
        *,
        session: Optional[Session],
        chat_id: Optional[int],
        dest: Optional[dict] = None,
        user_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        direct_messages_topic_id: Optional[int] = None,
        require_thread_id: Optional[bool] = None,
    ) -> TelegramTransportContext:
        merged_dest = dict(dest or {})
        resolved_chat_id = None
        if chat_id is not None:
            try:
                resolved_chat_id = int(chat_id)
            except Exception:
                resolved_chat_id = chat_id
        if resolved_chat_id is not None:
            merged_dest.setdefault("chat_id", resolved_chat_id)
        if direct_messages_topic_id is not None:
            merged_dest.setdefault("direct_messages_topic_id", int(direct_messages_topic_id))
        if session is not None and resolved_chat_id is not None:
            session_dest = self.build_telegram_reply_dest(
                session,
                int(resolved_chat_id),
                user_id=user_id,
                direct_messages_topic_id=direct_messages_topic_id,
            )
            for key, value in session_dest.items():
                merged_dest.setdefault(key, value)
        resolved_thread_id = message_thread_id
        if resolved_thread_id is None:
            resolved_thread_id = merged_dest.get("message_thread_id")
        scope = getattr(session, "conversation_scope", None)
        if (
            resolved_thread_id is None
            and isinstance(scope, ConversationScope)
            and scope.message_thread_id is not None
            and resolved_chat_id is not None
            and int(scope.chat_id) == int(resolved_chat_id)
        ):
            resolved_thread_id = int(scope.message_thread_id)
        if require_thread_id is None:
            require_thread_id = bool(
                isinstance(scope, ConversationScope)
                and scope.message_thread_id is not None
                and resolved_chat_id is not None
                and int(scope.chat_id) == int(resolved_chat_id)
            )
        try:
            normalized_chat_id = int(resolved_chat_id) if resolved_chat_id is not None else None
        except Exception:
            normalized_chat_id = None
        return TelegramTransportContext(
            raw_context=context,
            chat_id=normalized_chat_id,
            message_thread_id=int(resolved_thread_id) if resolved_thread_id is not None else None,
            direct_messages_topic_id=(
                int(merged_dest["direct_messages_topic_id"])
                if merged_dest.get("direct_messages_topic_id") is not None
                else None
            ),
            require_thread_id=bool(require_thread_id),
            session_uid=(
                str(getattr(scope, "session_uid", "") or "").strip()
                if isinstance(scope, ConversationScope)
                else None
            ),
        )

    def resolve_telegram_scope_session(
        self,
        *,
        reply_chat_id: int,
        message_thread_id: Optional[int] = None,
        owner_chat_id: Optional[int] = None,
    ) -> Optional[Session]:
        manager = getattr(self, "manager", None)
        if manager is None:
            return None
        try:
            session = manager.get_by_scope(int(reply_chat_id), message_thread_id)
        except Exception:
            logging.getLogger(__name__).exception(
                "failed to resolve session by scope reply_chat_id=%s message_thread_id=%s",
                reply_chat_id,
                message_thread_id,
            )
            session = None
        return session

    def resolve_telegram_callback_scope(self, query: Any) -> tuple[int, Optional[int], int, Optional[Session]]:
        message = getattr(query, "message", None)
        raw_chat_id = getattr(message, "chat_id", None)
        if raw_chat_id is None:
            raw_chat_id = getattr(getattr(message, "chat", None), "id", None)
        reply_chat_id = int(raw_chat_id or 0)
        raw_thread_id = getattr(message, "message_thread_id", None)
        try:
            message_thread_id = int(raw_thread_id) if raw_thread_id is not None else None
        except Exception:
            message_thread_id = None
        session = self.resolve_telegram_scope_session(
            reply_chat_id=reply_chat_id,
            message_thread_id=message_thread_id,
        )
        owner_chat_id = int(getattr(session, "chat_id", 0) or reply_chat_id) if session is not None else reply_chat_id
        if session is None:
            thread_mode = getattr(self.config, "thread_mode", None)
            thread_mode_mode = str(getattr(thread_mode, "mode", "") or "").strip()
            topics_chat_id = getattr(thread_mode, "topics_chat_id", None)
            if (
                bool(getattr(thread_mode, "enabled", False))
                and thread_mode_mode == "group"
                and topics_chat_id is not None
                and int(reply_chat_id) == int(topics_chat_id)
            ):
                raw_user_id = getattr(getattr(query, "from_user", None), "id", None)
                try:
                    if raw_user_id is not None:
                        owner_chat_id = int(raw_user_id)
                except Exception:
                    owner_chat_id = int(reply_chat_id)
        return reply_chat_id, message_thread_id, owner_chat_id, session

    def resolve_telegram_inbound_route(self, update: Update) -> TelegramInboundRoute:
        reply_chat = getattr(update, "effective_chat", None)
        reply_chat_id = int(getattr(reply_chat, "id", 0) or 0)
        thread_id = self._extract_message_thread_id(update)
        direct_topic_id = self._extract_direct_messages_topic_id(update)
        manager = getattr(self, "session_thread_manager", None)
        thread_mode = getattr(self.config, "thread_mode", None)
        thread_mode_enabled = bool(getattr(thread_mode, "enabled", False))
        thread_mode_mode = str(getattr(thread_mode, "mode", "") or "").strip()
        topics_chat_id = getattr(thread_mode, "topics_chat_id", None)

        def route_for(
            *,
            owner_chat_id: int,
            reply_chat_id: int,
            message_thread_id: Optional[int],
            session_uid: Optional[str],
            session: Optional[Session],
            unknown_thread: bool,
        ) -> TelegramInboundRoute:
            route = TelegramInboundRoute(
                owner_chat_id=owner_chat_id,
                reply_chat_id=reply_chat_id,
                message_thread_id=message_thread_id,
                session_uid=session_uid,
                session=session,
                direct_messages_topic_id=direct_topic_id,
                unknown_thread=unknown_thread,
            )
            log = logging.getLogger(__name__)
            if unknown_thread:
                log.warning(
                    "telegram inbound route chat_id=%s message_thread_id=%s "
                    "direct_messages_topic_id=%s owner_chat_id=%s session_id=%s "
                    "session_uid=%s unknown_thread=%s",
                    route.reply_chat_id,
                    route.message_thread_id,
                    route.direct_messages_topic_id,
                    route.owner_chat_id,
                    str(getattr(route.session, "id", "") or "-"),
                    route.session_uid or "-",
                    route.unknown_thread,
                )
            elif thread_id is not None or direct_topic_id is not None:
                log.debug(
                    "telegram inbound route chat_id=%s message_thread_id=%s "
                    "direct_messages_topic_id=%s owner_chat_id=%s session_id=%s "
                    "session_uid=%s unknown_thread=%s",
                    route.reply_chat_id,
                    route.message_thread_id,
                    route.direct_messages_topic_id,
                    route.owner_chat_id,
                    str(getattr(route.session, "id", "") or "-"),
                    route.session_uid or "-",
                    route.unknown_thread,
                )
            return route

        if (
            thread_mode_enabled
            and thread_mode_mode == "group"
            and topics_chat_id is not None
            and int(reply_chat_id) != int(topics_chat_id)
        ):
            return route_for(
                owner_chat_id=reply_chat_id,
                reply_chat_id=reply_chat_id,
                message_thread_id=thread_id,
                session_uid=None,
                session=None,
                unknown_thread=True,
            )

        if thread_mode_enabled and thread_mode_mode == "private":
            chat_type = str(getattr(reply_chat, "type", "") or "").lower()
            is_private_chat = chat_type in ("private", "")
            if thread_id is None:
                # In private 1-on-1 chats, topics don't exist (thread_id is always
                # None).  Try to find an existing session by chat_id and, if the chat
                # really is private, fall through to the non-thread-mode path instead
                # of blocking with unknown_thread.
                session = self.resolve_telegram_scope_session(
                    reply_chat_id=reply_chat_id,
                    message_thread_id=None,
                    owner_chat_id=reply_chat_id,
                )
                if session is not None:
                    scope = getattr(session, "conversation_scope", None)
                    return route_for(
                        owner_chat_id=int(getattr(session, "chat_id", 0) or reply_chat_id),
                        reply_chat_id=reply_chat_id,
                        message_thread_id=None,
                        session_uid=str(getattr(scope, "session_uid", "") or "").strip() or None,
                        session=session,
                        unknown_thread=False,
                    )
                if is_private_chat:
                    # Private chat without session — skip thread enforcement,
                    # let the command handler deal with session creation.
                    return route_for(
                        owner_chat_id=reply_chat_id,
                        reply_chat_id=reply_chat_id,
                        message_thread_id=None,
                        session_uid=None,
                        session=None,
                        unknown_thread=False,
                    )
                return route_for(
                    owner_chat_id=reply_chat_id,
                    reply_chat_id=reply_chat_id,
                    message_thread_id=None,
                    session_uid=None,
                    session=None,
                    unknown_thread=True,
                )
            session = self.resolve_telegram_scope_session(
                reply_chat_id=reply_chat_id,
                message_thread_id=thread_id,
                owner_chat_id=reply_chat_id,
            )
            if session is None:
                rebinder = getattr(manager, "rebind_recent_stale_session", None)
                if callable(rebinder):
                    try:
                        session = rebinder(
                            owner_chat_id=reply_chat_id,
                            topics_chat_id=reply_chat_id,
                            message_thread_id=thread_id,
                        )
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "failed to rebind stale private topic owner_chat_id=%s thread_id=%s",
                            reply_chat_id,
                            thread_id,
                        )
                        session = None
                if session is None:
                    return route_for(
                        owner_chat_id=reply_chat_id,
                        reply_chat_id=reply_chat_id,
                        message_thread_id=thread_id,
                        session_uid=None,
                        session=None,
                        unknown_thread=True,
                    )
            syncer = getattr(manager, "bind_existing_topic_for_session", None)
            if callable(syncer):
                try:
                    scope = getattr(session, "conversation_scope", None)
                    if (
                        not isinstance(scope, ConversationScope)
                        or int(scope.chat_id) != int(reply_chat_id)
                        or int(scope.message_thread_id or 0) != int(thread_id or 0)
                    ):
                        syncer(
                            owner_chat_id=int(getattr(session, "chat_id", 0) or reply_chat_id),
                            session=session,
                            topics_chat_id=reply_chat_id,
                            message_thread_id=thread_id,
                        )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "failed to sync private topic inbound scope owner_chat_id=%s session_id=%s thread_id=%s",
                        reply_chat_id,
                        getattr(session, "id", "-"),
                        thread_id,
                    )
            scope = getattr(session, "conversation_scope", None)
            return route_for(
                owner_chat_id=int(getattr(session, "chat_id", 0) or reply_chat_id),
                reply_chat_id=reply_chat_id,
                message_thread_id=thread_id,
                session_uid=str(getattr(scope, "session_uid", "") or "").strip() or None,
                session=session,
                unknown_thread=False,
            )

        if (
            manager is None
            or not hasattr(manager, "is_enabled")
            or not bool(manager.is_enabled())
            or topics_chat_id is None
            or int(reply_chat_id) != int(topics_chat_id)
        ):
            session = self.resolve_telegram_scope_session(
                reply_chat_id=reply_chat_id,
                message_thread_id=thread_id,
                owner_chat_id=reply_chat_id,
            )
            scope = getattr(session, "conversation_scope", None)
            owner_chat_id = (
                int(getattr(session, "chat_id", 0) or reply_chat_id)
                if session is not None
                else reply_chat_id
            )
            return route_for(
                owner_chat_id=owner_chat_id,
                reply_chat_id=reply_chat_id,
                message_thread_id=thread_id,
                session_uid=str(getattr(scope, "session_uid", "") or "").strip() or None,
                session=session,
                unknown_thread=False,
            )

        if thread_id is None:
            return route_for(
                owner_chat_id=reply_chat_id,
                reply_chat_id=reply_chat_id,
                message_thread_id=None,
                session_uid=None,
                session=None,
                unknown_thread=True,
            )

        session_uid = manager.resolve_session_uid(chat_id=reply_chat_id, message_thread_id=thread_id)
        session = self.manager.get_by_uid(str(session_uid)) if session_uid else None
        if session is None:
            session = self.resolve_telegram_scope_session(
                reply_chat_id=reply_chat_id,
                message_thread_id=thread_id,
            )
        if session is None:
            return route_for(
                owner_chat_id=reply_chat_id,
                reply_chat_id=reply_chat_id,
                message_thread_id=thread_id,
                session_uid=None,
                session=None,
                unknown_thread=True,
            )

        scope = getattr(session, "conversation_scope", None)
        owner_chat_id = int(getattr(session, "chat_id", 0) or reply_chat_id)
        return route_for(
            owner_chat_id=owner_chat_id,
            reply_chat_id=reply_chat_id,
            message_thread_id=thread_id,
            session_uid=str(getattr(scope, "session_uid", "") or session_uid or "").strip() or None,
            session=session,
            unknown_thread=False,
        )

    def mark_telegram_thread_delivery_failed(
        self,
        *,
        chat_id: Optional[int],
        message_thread_id: Optional[int],
        reason: str = "",
    ) -> None:
        manager = getattr(self, "session_thread_manager", None)
        marker = getattr(manager, "mark_topic_stale", None) if manager is not None else None
        if callable(marker) and chat_id is not None and message_thread_id is not None:
            marker(
                topics_chat_id=int(chat_id),
                message_thread_id=int(message_thread_id),
                reason=str(reason or ""),
            )

    def _route_has_any_sessions(self, route: TelegramInboundRoute) -> bool:
        manager = getattr(self, "manager", None)
        if manager is None:
            return False
        owner_chat_id = int(getattr(route, "owner_chat_id", 0) or 0)
        try:
            if owner_chat_id and bool(manager.sessions_for_chat(owner_chat_id)):
                return True
        except Exception:
            logging.getLogger(__name__).exception(
                "failed to inspect session inventory for owner_chat_id=%s",
                owner_chat_id,
            )
        thread_mode = getattr(self.config, "thread_mode", None)
        thread_mode_mode = str(getattr(thread_mode, "mode", "") or "").strip()
        topics_chat_id = getattr(thread_mode, "topics_chat_id", None)
        if thread_mode_mode == "group" and topics_chat_id is not None and int(route.reply_chat_id) == int(topics_chat_id):
            by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
            return any(bool(by_id) for by_id in by_chat.values() if isinstance(by_id, dict))
        return False

    def _unknown_thread_text(self, route: TelegramInboundRoute) -> str:
        thread_mode = getattr(self.config, "thread_mode", None)
        thread_mode_mode = str(getattr(thread_mode, "mode", "") or "").strip()
        topics_chat_id = getattr(thread_mode, "topics_chat_id", None)
        lang = resolve_user_lang(self.config, chat_id=int(route.reply_chat_id))
        if (
            bool(getattr(thread_mode, "enabled", False))
            and thread_mode_mode == "group"
            and topics_chat_id is not None
            and int(route.reply_chat_id) != int(topics_chat_id)
        ):
            return t("bot.unknown_thread_group", lang, topics_chat_id=int(topics_chat_id))
        if route.message_thread_id is None:
            if self._route_has_any_sessions(route):
                return t("bot.unknown_thread_use_topics", lang)
            return t("bot.no_session_create", lang)
        return t("bot.unknown_thread_no_session", lang)

    def _can_allow_outside_topic(self, route: TelegramInboundRoute) -> bool:
        thread_mode = getattr(self.config, "thread_mode", None)
        thread_mode_mode = str(getattr(thread_mode, "mode", "") or "").strip()
        topics_chat_id = getattr(thread_mode, "topics_chat_id", None)
        if thread_mode_mode == "group" and topics_chat_id is not None:
            return int(route.reply_chat_id) == int(topics_chat_id)
        return True

    def _build_outside_topic_route(self, update: Update, route: TelegramInboundRoute) -> TelegramInboundRoute:
        effective_user = getattr(update, "effective_user", None)
        owner_chat_id = int(getattr(effective_user, "id", 0) or route.owner_chat_id or route.reply_chat_id)
        return TelegramInboundRoute(
            owner_chat_id=owner_chat_id,
            reply_chat_id=int(route.reply_chat_id),
            # Outside-topic fallback must escape the unknown/stale thread entirely.
            # Reusing its message_thread_id can make Telegram anchor the reply inside
            # a phantom command-named topic instead of the chat root.
            message_thread_id=None,
            session_uid=None,
            session=None,
            unknown_thread=False,
        )

    async def ensure_telegram_inbound_authorized(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        scope: str = "generic",
        require_admin: bool = False,
        emit_denied_message: bool = True,
        emit_unknown_thread_message: bool = True,
        allow_outside_topic: bool = False,
    ) -> Optional[TelegramInboundRoute]:
        route = self.resolve_telegram_inbound_route(update)
        if route.unknown_thread:
            if allow_outside_topic and self._can_allow_outside_topic(route):
                route = self._build_outside_topic_route(update, route)
            else:
                if emit_unknown_thread_message:
                    await self._send_message(
                        context,
                        text=self._unknown_thread_text(route),
                        md2=True,
                        **route.reply_kwargs(),
                    )
                return None

        try:
            rate_decision = self.security.consume_rate_limit(
                "telegram.ingress",
                int(route.owner_chat_id),
                limit=120,
                window_sec=60,
                burst_limit=30,
                burst_window_sec=10,
            )
        except ValueError:
            rate_decision = None
        if rate_decision is not None and not rate_decision.allowed:
            await self.security.emit_audit(
                category="rate_limit_denied",
                action="telegram_ingress",
                status="denied",
                user_id=int(route.owner_chat_id),
                subject=scope,
                scope="telegram.ingress",
                reason=str(rate_decision.reason or ""),
                context={"chat_id": int(route.owner_chat_id)},
                details=rate_decision.__dict__,
            )
            if emit_denied_message:
                _rl_lang = resolve_user_lang(self.config, chat_id=int(route.owner_chat_id))
                await self._send_message(
                    context,
                    text=t("bot.rate_limit", _rl_lang),
                    **route.reply_kwargs(),
                )
            return None

        decision = self.access_policy_service.authorize(
            int(route.owner_chat_id),
            scope=scope,
            require_admin=require_admin,
        )
        if decision.allowed:
            _user = getattr(update, "effective_user", None)
            _user_id = getattr(_user, "id", None)
            _lang_code = getattr(_user, "language_code", None)
            if _user_id is not None:
                _config_svc = getattr(self, "config_service", None)
                if _config_svc is not None:
                    asyncio.create_task(
                        maybe_persist_user_language(
                            _user_id, _lang_code, self.config, _config_svc
                        )
                    )
            return route

        if not emit_denied_message:
            return None

        if require_admin:
            await self._send_message(
                context,
                text=self.access_policy_service.admin_denied_text(scope),
                **route.reply_kwargs(),
            )
            return None

        if self.access_policy_service.is_whitelisted(int(route.owner_chat_id)):
            _ac_lang = resolve_user_lang(self.config, chat_id=int(route.owner_chat_id))
            await self._send_message(
                context,
                text=t("bot.access_not_configured", _ac_lang),
                **route.reply_kwargs(),
            )
        return None

    async def ensure_telegram_inbound_session(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        auto_create: bool = False,
        scope: str = "generic",
        allow_outside_topic: bool = False,
    ) -> tuple[Optional[TelegramInboundRoute], Optional[Session]]:
        route = await self.ensure_telegram_inbound_authorized(
            update,
            context,
            scope=scope,
            allow_outside_topic=allow_outside_topic,
        )
        if route is None:
            return None, None

        if route.session is not None:
            return route, route.session

        if auto_create:
            session = await self.ensure_scope_session(
                int(route.owner_chat_id),
                context,
                reply_chat_id=int(route.reply_chat_id),
                message_thread_id=route.message_thread_id,
            )
            if session is None:
                return route, None
        else:
            session = self.resolve_telegram_scope_session(
                reply_chat_id=int(route.reply_chat_id),
                message_thread_id=route.message_thread_id,
                owner_chat_id=int(route.owner_chat_id),
            )
        if session is None:
            await self._send_message(
                context,
                text=self.access_policy_service.SESSION_REQUIRED_TEXT,
                **route.reply_kwargs(),
            )
            return route, None
        return route, session

    async def _cancel_mode_tasks_service(self, session_id: str, mode_id: str, timeout_s: float = 0.2) -> int:
        return await self.mode_tasks.cancel_all(session_id=str(session_id), mode_id=str(mode_id), timeout_s=float(timeout_s))

    async def _cancel_session_tasks_service(self, session_id: str, timeout_s: float = 0.2) -> int:
        return await self.mode_tasks.cancel_session(session_id=str(session_id), timeout_s=float(timeout_s))

    def _mode_messaging_factory(self, context: Any) -> MessagingService:
        return MessagingService(
            send_message=self._send_message,
            edit_message=self._edit_message,
            delete_message=self._delete_message,
            send_document=self._send_document,
            transport_context=context,
        )

    def _mode_tool_registry(self) -> Any:
        registry = getattr(self, "_tool_registry", None)
        if registry is None:
            raise RuntimeError("tool registry is not initialized")
        return registry

    async def _mode_execute_tool(self, tool_name: str, args: dict, tool_ctx: dict) -> dict:
        registry = self._mode_tool_registry()
        return await registry.execute(str(tool_name or ""), dict(args or {}), dict(tool_ctx or {}))

    async def _start_dirs_flow_service(self, chat_id: int, context: Any, root: str, mode_token: str) -> None:
        await self.dirs_service.start_flow(int(chat_id), context, root=str(root), mode_token=str(mode_token))

    def _clear_dirs_flow_service(self, chat_id: int, mode_id: str, flow: str) -> None:
        self.dirs_service.clear_flow(int(chat_id), mode_id=str(mode_id or ""), flow=str(flow or ""))

    def _get_dirs_flow_token_service(self, chat_id: Any, message_thread_id: Optional[int] = None) -> str:
        return str(
            self.ui_state.dirs_mode.get(self.telegram_ui_key(int(chat_id), message_thread_id), "")
            or ""
        )

    def _get_agent_session_by_uid_service(self, session_uid: str, chat_id: Optional[int] = None) -> Any:
        token = str(session_uid or "").strip()
        if not token:
            return None
        return self.manager.get_by_uid(token)

    def _initialize_mode_plugins(self) -> None:
        self.mode_registry_service.initialize_plugins(
            config=self.config,
            services={
                "tasks": self.mode_tasks,
                "dialogs": self.mode_dialogs,
                "session_control": self.mode_session_control,
                "messaging_factory": self._mode_messaging_factory,
                "pipeline": self.mode_pipeline,
                "mode_dependencies": self.container.mode_dependencies,
                "run_artifacts": self.mode_run_artifacts,
                "run_observability": self.mode_run_observability,
                "run_doctor": self.mode_run_doctor,
                "run_boundary_validation": self.mode_run_boundary_validation,
                "skill_runtime": self.mode_skill_runtime,
                "agent_runtime": self.mode_agent_runtime,
                "dirs_flow": self.mode_dirs_flow,
                "manager_pending": self.mode_manager_resume_pending,
                "agent_pending": self.mode_agent_project_pending_by_chat,
                "runtime_by_capability": self.get_runtime_by_capability,
                "tooling": self.mode_tooling,
                "ssh": self.ssh_service,
            },
        )

    def register_mode_runtime(self, mode_id: str, runtime: Any) -> None:
        mid = str(mode_id or "").strip()
        if not mid:
            return
        self.mode_runtime_registry[mid] = runtime

    def _register_mode_runtimes_from_plugins(self) -> None:
        registry = getattr(self, "mode_registry", None)
        if registry is None:
            return
        for mode_id in registry.list_ids():
            plugin = registry.get(mode_id)
            if plugin is None or not hasattr(plugin, "build_runtime"):
                continue
            try:
                runtime = plugin.build_runtime(self.config)
            except Exception as e:
                logging.getLogger(__name__).exception("mode runtime init failed mode=%s err=%s", mode_id, e)
                continue
            if runtime is not None:
                self.register_mode_runtime(mode_id, runtime)

    def iter_mode_runtimes(self):
        return list(self.mode_runtime_registry.values())

    def get_runtime_by_capability(self, capability: str) -> Any:
        cap = str(capability or "").strip()
        if not cap:
            return None
        for runtime in self.iter_mode_runtimes():
            try:
                supports = getattr(runtime, "supports_capability", None)
                if callable(supports) and bool(supports(cap)):
                    return runtime
                caps = getattr(runtime, "capabilities", None)
                if isinstance(caps, (set, frozenset, list, tuple)):
                    if cap in {str(x).strip() for x in caps}:
                        return runtime
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "runtime capability check degraded runtime=%s capability=%s err=%s",
                    type(runtime).__name__,
                    cap,
                    e,
                )
                continue
        return None

    @property
    def summarize_text_with_reason(self):
        # Access from module level to allow patching in tests
        import sys
        bot_module = sys.modules.get('bot', sys.modules[__name__])
        if hasattr(bot_module, 'summarize_text_with_reason'):
            return bot_module.summarize_text_with_reason
        return summarize_text_with_reason

    def _configure_agent_sandbox(self) -> None:
        self.sandbox_service.configure()

    def _agent_sandbox_root(self) -> str:
        return self.sandbox_service.root()

    def _agent_service_entries(self) -> set[str]:
        return self.sandbox_service.service_entries()

    def _clear_agent_sandbox(self, chat_id: Optional[int] = None) -> tuple[int, int]:
        return self.sandbox_service.clear(chat_id=chat_id)

    def _clear_agent_session_files(self, session_id: str) -> bool:
        return self.sandbox_service.clear_session(str(session_id))

    def is_admin(self, chat_id: int) -> bool:
        return int(chat_id) in set(getattr(self.config.telegram, "admlist_chat_ids", []) or [])

    def _user_projects(self, chat_id: int) -> list[str]:
        raw = (getattr(self.config.telegram, "user_workdirs", {}) or {}).get(int(chat_id)) or []
        if isinstance(raw, str):
            raw = [raw]
        projects: list[str] = []
        seen: set[str] = set()
        for p in raw if isinstance(raw, list) else []:
            sp = str(p or "").strip()
            if not sp:
                continue
            rp = os.path.realpath(sp)
            if not os.path.isdir(rp):
                continue
            if rp in seen:
                continue
            seen.add(rp)
            projects.append(rp)
        return projects

    def user_projects(self, chat_id: int) -> list[str]:
        return self._user_projects(chat_id)

    def _user_projects_set(self, chat_id: int) -> set[str]:
        return {os.path.realpath(p) for p in self._user_projects(chat_id)}

    def is_session_allowed_for_chat(self, chat_id: int, session: Optional[Session]) -> bool:
        if session is None:
            return False
        chat_id = int(chat_id)
        if self.is_admin(chat_id):
            return True
        if not self.is_user(chat_id):
            return False
        allowed = self._user_projects_set(chat_id)
        if not allowed:
            return False
        try:
            return os.path.realpath(str(session.workdir or "")) in allowed
        except Exception:
            return False

    def is_user(self, chat_id: int) -> bool:
        chat_id = int(chat_id)
        if self.is_admin(chat_id):
            return False
        if chat_id not in (self.config.telegram.whitelist_chat_ids or []):
            return False
        return bool(self._user_projects(chat_id))

    def is_allowed(self, chat_id: int) -> bool:
        # Used in low-context places (filters/background tasks). Do not emit messages here.
        return bool(self.access_policy_service.is_allowed(int(chat_id)))

    async def ensure_allowed(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        return bool(await self.access_policy_service.ensure_allowed(int(chat_id), context))

    def is_within_root(self, path: str, root: str) -> bool:
        return utils_is_within_root(path, root)

    def _plugin_awaiting_input(self, chat_id: int) -> bool:
        """Check if any plugin is waiting for free-text input from the user."""
        registry = getattr(self, "_tool_registry", None)
        if registry is None:
            return False
        try:
            return registry.any_awaiting_input(chat_id)
        except Exception:
            return False

    def _cancel_plugin_dialogs(self, chat_id: int) -> None:
        """Cancel all pending plugin dialogs for the given chat."""
        registry = getattr(self, "_tool_registry", None)
        if registry:
            try:
                registry.cancel_all_inputs(chat_id)
            except Exception:
                logging.getLogger(__name__).exception("cancel plugin dialogs failed chat_id=%s", chat_id)

    def _on_session_change(self, chat_id: int) -> None:
        """Called by SessionManager when the session inventory changes.

        Cancels any active plugin dialogs for all whitelisted chats so that
        stale dialogs never block message processing after session transitions.
        Injects shared _ssh_service reference into sessions that lack it.
        """
        try:
            cid = int(chat_id)
        except Exception:
            return
        # Inject _ssh_service into sessions that don't have it yet.
        ssh_svc = getattr(self, "ssh_service", None)
        if ssh_svc is not None:
            for session in self.manager.sessions_for_chat(cid).values():
                if getattr(session, "_ssh_service", None) is None:
                    session._ssh_service = ssh_svc
        self._cancel_plugin_dialogs(cid)
        upload_keys = [
            key for key in list(self.ui_state.files_pending_upload_tasks.keys())
            if isinstance(key, TelegramUiKey) and int(key.chat_id) == cid
        ]
        rename_keys = [
            key for key in list(self.ui_state.files_pending_rename_tasks.keys())
            if isinstance(key, TelegramUiKey) and int(key.chat_id) == cid
        ]
        if not upload_keys and not rename_keys:
            self._stop_files_upload_wait(cid)
            self._stop_files_rename_wait(cid)
            return
        for ui_key in upload_keys:
            self._stop_files_upload_wait(cid, message_thread_id=ui_key.message_thread_id)
        for ui_key in rename_keys:
            self._stop_files_rename_wait(cid, message_thread_id=ui_key.message_thread_id)

    def _setup_logging(self) -> None:
        setup_logging(self.config)

    def _format_ts(self, ts: float, lang: str = "ru") -> str:
        import datetime as _dt

        if not ts:
            return t("session_status.no", lang)
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _short_label(self, text: str, max_len: int = 40) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _tool_exec(self, tool: ToolConfig) -> Optional[str]:
        return tool_exec(tool)

    def _is_tool_available(self, name: str) -> bool:
        return is_tool_available(self.config, name)

    def _available_tools(self) -> list[str]:
        return available_tools(self.config)

    def _expected_tools(self) -> str:
        return ", ".join(sorted(self.config.tools.keys()))

    async def _send_message(self, context: ContextTypes.DEFAULT_TYPE, **kwargs):
        return await self.transport_service.send_message(context, **kwargs)

    async def send_message(self, context: ContextTypes.DEFAULT_TYPE, **kwargs):
        """Public wrapper over the internal Telegram send so callers outside BotApp
        do not depend on the private `_send_message`."""
        return await self._send_message(context, **kwargs)

    async def _send_rich_message_draft(self, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> bool:
        return await self.transport_service.send_rich_message_draft(context, **kwargs)

    async def _send_document(self, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> bool:
        return await self.transport_service.send_document(context, **kwargs)

    async def _send_ask_question(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        session_id: str,
        question_id: str,
        question: str,
        options: list[str],
        allow_custom: bool = True,
        system_options: bool = True,
        message_thread_id: Optional[int] = None,
    ) -> None:
        _ask_q_lang = resolve_user_lang(self.config, chat_id=chat_id)
        options = self._normalize_ask_options(options, allow_custom=allow_custom)
        options = self._ensure_min_ask_options(options, system_options=system_options, lang=_ask_q_lang)
        ui_key = self.telegram_ui_key(
            chat_id,
            message_thread_id if message_thread_id is not None else getattr(context, "message_thread_id", None),
        )
        self.ui_state.pending_questions[question_id] = {
            "question": str(question or ""),
            "options": options,
            "chat_id": int(ui_key.chat_id),
            "message_thread_id": ui_key.message_thread_id,
            "session_id": session_id,
            "awaiting_custom": False,
            "allow_custom": allow_custom,
            "created_at": time.time(),
        }
        rows = [[InlineKeyboardButton(opt, callback_data=f"ask:{question_id}:{idx}")] for idx, opt in enumerate(options)]
        if allow_custom:
            rows.append([InlineKeyboardButton(t("bot.ask_custom_btn", _ask_q_lang), callback_data=f"ask:{question_id}:custom")])
        keyboard = InlineKeyboardMarkup(rows)
        await self._send_message(context, text=question, reply_markup=keyboard, **ui_key.reply_kwargs())

    @staticmethod
    def _normalize_ask_options(options: list[str], allow_custom: bool = True) -> list[str]:
        # "Свой вариант" always rendered as a dedicated custom-input button.
        custom_markers = ("свой вариант", "свой ответ", "other", "custom")
        normalized: list[str] = []
        seen: set[str] = set()
        for option in options or []:
            text = str(option or "").strip()
            if not text:
                continue
            folded = text.casefold()
            if allow_custom and any(marker in folded for marker in custom_markers):
                continue
            if folded in seen:
                continue
            seen.add(folded)
            normalized.append(text)
        return normalized

    @staticmethod
    def _ensure_min_ask_options(options: list[str], system_options: bool = True, lang: str = "ru") -> list[str]:
        normalized = [str(x).strip() for x in (options or []) if str(x).strip()]
        if len(normalized) >= 2:
            return normalized[:4]
        if not system_options:
            return normalized
        if len(normalized) == 1:
            return [normalized[0], t("bot.ask_stop_clarify", lang)]
        return [t("bot.ask_continue_assumptions", lang), t("bot.ask_stop_clarify", lang)]

    def _clear_pending_question(self, question_id: str) -> bool:
        qid = str(question_id or "").strip()
        if not qid:
            return False
        meta = self.ui_state.pending_questions.pop(qid, None)
        if isinstance(meta, dict):
            try:
                ui_key = self.telegram_ui_key(
                    int(meta.get("chat_id")),
                    meta.get("message_thread_id"),
                )
            except Exception:
                ui_key = None
            if ui_key is not None and self.ui_state.active_ask_question_by_chat.get(ui_key) == qid:
                self.ui_state.active_ask_question_by_chat.pop(ui_key, None)
        # Keep registry pending-futures consistent even when transport/UI side clears metadata.
        reg = getattr(self, "_tool_registry", None)
        pending = getattr(reg, "pending_questions", None) if reg is not None else None
        fut = pending.pop(qid, None) if isinstance(pending, dict) else None
        if fut is not None and hasattr(fut, "done") and not fut.done():
            try:
                fut.cancel()
            except Exception as e:
                logging.exception("clear pending question future failed: %s", e)
        return bool(meta is not None or fut is not None)

    def _clear_pending_questions(
        self,
        *,
        session_id: Optional[str] = None,
        chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
    ) -> int:
        sid = str(session_id or "").strip() or None
        cid = int(chat_id) if chat_id is not None else None
        expected_ui_key = self.telegram_ui_key(cid, message_thread_id) if cid is not None else None
        removed = 0
        for qid, meta in list(self.ui_state.pending_questions.items()):
            if not isinstance(meta, dict):
                continue
            if sid is not None and str(meta.get("session_id") or "") != sid:
                continue
            if expected_ui_key is not None:
                meta_ui_key = self.telegram_ui_key(
                    int(meta.get("chat_id") or 0),
                    meta.get("message_thread_id"),
                )
                if meta_ui_key != expected_ui_key:
                    continue
            elif cid is not None and int(meta.get("chat_id") or 0) != cid:
                continue
            if self._clear_pending_question(qid):
                removed += 1
        if expected_ui_key is not None and expected_ui_key in self.ui_state.active_ask_question_by_chat:
            qid = str(self.ui_state.active_ask_question_by_chat.get(expected_ui_key) or "").strip()
            meta = self.ui_state.pending_questions.get(qid) if qid else None
            should_clear_active = False
            if not qid:
                should_clear_active = True
            elif not isinstance(meta, dict):
                should_clear_active = True
            else:
                active_sid = str(meta.get("session_id") or "").strip()
                active_ui_key = self.telegram_ui_key(
                    int(meta.get("chat_id") or 0),
                    meta.get("message_thread_id"),
                )
                if active_ui_key != expected_ui_key:
                    should_clear_active = True
                if sid is not None and active_sid == sid:
                    should_clear_active = True
            if should_clear_active:
                self.ui_state.active_ask_question_by_chat.pop(expected_ui_key, None)
        return removed

    def _resolve_pending_custom_answer(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        answer = (text or "").strip()
        if not answer:
            return False
        ui_key = self.telegram_ui_key(chat_id, message_thread_id)
        self._pending_custom_input_status_by_chat.pop(ui_key, None)
        chat_id_int = int(ui_key.chat_id)
        active_qid = str(self.ui_state.active_ask_question_by_chat.get(ui_key, "") or "").strip()
        if not active_qid:
            return False
        active_meta = self.ui_state.pending_questions.get(active_qid)
        if not isinstance(active_meta, dict):
            self.ui_state.active_ask_question_by_chat.pop(ui_key, None)
            return False
        active_ui_key = self.telegram_ui_key(
            int(active_meta.get("chat_id") or 0),
            active_meta.get("message_thread_id"),
        )
        if active_ui_key != ui_key:
            self.ui_state.active_ask_question_by_chat.pop(ui_key, None)
            return False
        active_sid = str(active_meta.get("session_id") or "").strip()
        if active_sid and not self._session_exists_for_chat(chat_id_int, active_sid):
            self._clear_pending_question(active_qid)
            self._pending_custom_input_status_by_chat[ui_key] = "stale"
            return True

        if not bool(active_meta.get("awaiting_custom", False)):
            allow_custom = bool(active_meta.get("allow_custom", False))
            if not allow_custom or not self._should_accept_implicit_analyst_text_answer(
                chat_id_int,
                active_sid,
            ):
                self.ui_state.active_ask_question_by_chat.pop(ui_key, None)
                return False
        lowered = answer.casefold()
        if lowered in {"-", "отмена", "cancel"}:
            active_meta["awaiting_custom"] = False
            active_meta.pop("custom_prompt_msg_id", None)
            self.ui_state.active_ask_question_by_chat.pop(ui_key, None)
            self._pending_custom_input_status_by_chat[ui_key] = "cancelled"
            return True

        runtime = self.get_runtime_by_capability("resolve_question")
        try:
            resolved = bool(runtime and runtime.resolve_question(active_qid, answer))
        except Exception as e:
            logging.getLogger(__name__).exception("resolve_question failed question_id=%s err=%s", active_qid, e)
            self._pending_custom_input_status_by_chat[ui_key] = "stale"
            return True
        if resolved:
            self._clear_pending_question(active_qid)
            self._pending_custom_input_status_by_chat[ui_key] = "resolved"
            return True
        self._pending_custom_input_status_by_chat[ui_key] = "stale"
        return False

    def _should_accept_implicit_analyst_text_answer(self, chat_id: int, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        manager = getattr(self, "manager", None)
        getter = getattr(manager, "get", None) if manager is not None else None
        if not callable(getter):
            return False
        try:
            session = getter(int(chat_id), sid)
        except Exception:
            logging.getLogger(__name__).exception(
                "implicit analyst text answer session lookup failed chat_id=%s session_id=%s",
                chat_id,
                sid,
            )
            return False
        if session is None:
            return False
        active_mode = str(
            getattr(
                getattr(session, "modes", None),
                "active_mode",
                getattr(session, "active_mode", ""),
            )
            or ""
        ).strip().lower()
        return active_mode == "analyst"

    def _session_exists_for_chat(self, chat_id: int, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        manager = getattr(self, "manager", None)
        if manager is None:
            return False
        getter = getattr(manager, "get", None)
        if callable(getter):
            try:
                return getter(int(chat_id), sid) is not None
            except Exception:
                logging.getLogger(__name__).exception(
                    "session lookup failed in _session_exists_for_chat chat_id=%s session_id=%s",
                    chat_id,
                    sid,
                )
                return False
        by_chat = getattr(manager, "sessions_by_chat", None)
        if isinstance(by_chat, dict):
            try:
                sessions = by_chat.get(int(chat_id)) or {}
                return sid in sessions
            except Exception:
                logging.getLogger(__name__).exception(
                    "sessions_by_chat lookup failed in _session_exists_for_chat chat_id=%s session_id=%s",
                    chat_id,
                    sid,
                )
        return False

    def _pop_pending_custom_input_status(
        self,
        chat_id: int,
        *,
        message_thread_id: Optional[int] = None,
    ) -> str:
        ui_key = self.telegram_ui_key(chat_id, message_thread_id)
        return str(self._pending_custom_input_status_by_chat.pop(ui_key, "") or "")

    async def _delete_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> bool:
        return await self.transport_service.delete_message(context, chat_id, message_id)

    async def _edit_message(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
        message_id: int, text: str, *, md2: bool = True, reply_markup: Optional[InlineKeyboardMarkup] = None,
        prefer_rich: bool = True,
    ) -> bool:
        return await self.transport_service.edit_message(
            context,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            md2=md2,
            prefer_rich=prefer_rich,
            reply_markup=reply_markup,
        )

    async def _edit_message_outcome(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
        message_id: int, text: str, *, md2: bool = True, reply_markup: Optional[InlineKeyboardMarkup] = None,
        prefer_rich: bool = True,
    ) -> TelegramEditOutcome:
        return await self.transport_service.edit_message_outcome(
            context,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            md2=md2,
            prefer_rich=prefer_rich,
            reply_markup=reply_markup,
        )

    async def _clear_message_reply_markup(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        *,
        dest: Optional[dict] = None,
    ) -> bool:
        _ = dest
        return await self.transport_service.edit_message_reply_markup(
            context,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )

    def _find_session_by_id(self, session_id: str) -> Optional[Session]:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        manager = getattr(self, "manager", None)
        by_chat = getattr(manager, "sessions_by_chat", None) if manager is not None else None
        if not isinstance(by_chat, dict):
            return None
        for sessions in by_chat.values():
            if not isinstance(sessions, dict):
                continue
            session = sessions.get(sid)
            if session is not None:
                return session
        return None

    def _request_command_approval(self, chat_id: int, cmd_id: str, cmd: str, reason: str) -> None:
        pending = get_pending_command(cmd_id)
        session = self._find_session_by_id(getattr(pending, "session_id", ""))
        reply_chat_id = int(chat_id)
        message_thread_id = None
        scope = getattr(session, "conversation_scope", None) if session is not None else None
        if isinstance(scope, ConversationScope):
            if getattr(scope, "chat_id", None) is not None:
                reply_chat_id = int(scope.chat_id)
            if getattr(scope, "message_thread_id", None) is not None:
                message_thread_id = int(scope.message_thread_id)
        context = (
            self.ui_state.context_by_chat.get(reply_chat_id)
            or self.ui_state.context_by_chat.get(chat_id)
        )
        if not context:
            return

        async def _send() -> None:
            _cmd_lang = resolve_user_lang(getattr(self, "config", None), chat_id=int(chat_id))
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(t("desktop.btn.cmd_approve", _cmd_lang), callback_data=f"approve_cmd:{cmd_id}"),
                        InlineKeyboardButton(t("desktop.btn.cmd_deny", _cmd_lang), callback_data=f"deny_cmd:{cmd_id}"),
                    ]
                ]
            )
            send_kwargs = {
                "chat_id": int(reply_chat_id),
                "text": t("bot.cmd_confirm", _cmd_lang, reason=reason, cmd=cmd),
                "reply_markup": keyboard,
            }
            if message_thread_id is not None:
                send_kwargs["message_thread_id"] = int(message_thread_id)
            await self._send_message(context, **send_kwargs)

        task = asyncio.create_task(_send())
        bg = getattr(self, "_bg_tasks", None)
        if bg is None:
            bg = self._bg_tasks = set()
        bg.add(task)
        task.add_done_callback(bg.discard)

    def _build_state_keyboard(self, ui_key: TelegramUiKey) -> InlineKeyboardMarkup:
        keys = self.ui_state.state_menu.get(ui_key, [])
        page = self.ui_state.state_menu_page.get(ui_key, 0)
        page_size = 10
        start = page * page_size
        end = start + page_size
        _sk_lang = resolve_user_lang(self.config, chat_id=int(ui_key.chat_id))
        rows = []
        for i, k in enumerate(keys[start:end], start=start):
            rows.append([InlineKeyboardButton(self._short_label(k), callback_data=f"state_pick:{i}")])
        nav = []
        if start > 0:
            nav.append(InlineKeyboardButton(t("msg.dirs.btn_prev", _sk_lang), callback_data=f"state_page:{page-1}"))
        if end < len(keys):
            nav.append(InlineKeyboardButton(t("msg.dirs.btn_next", _sk_lang), callback_data=f"state_page:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(t("btn.session.cancel", _sk_lang), callback_data="agent_cancel")])
        return InlineKeyboardMarkup(rows)

    async def send_output(
        self,
        session: Session,
        dest: dict,
        output: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        send_header: bool = True,
        header_override: Optional[str] = None,
        force_html: bool = False,
        send_summary: bool = True,
    ) -> None:
        await self.session_management.send_output(
            session, dest, output, context,
            send_header=send_header, header_override=header_override,
            force_html=force_html,
            send_summary=send_summary,
        )

    async def run_prompt(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.session_management.run_prompt(session, prompt, dest, context)

    async def run_mode_pipeline(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
        mode_id: str,
    ) -> None:
        await self.session_management.run_mode_pipeline(session, prompt, dest, context, mode_id=mode_id)

    def _resolve_recovery_execution_vector(
        self,
        *,
        session: Session,
        state: Dict[str, Any],
        context: Any,
        dest: Optional[Dict[str, Any]],
    ) -> tuple[Any, Dict[str, Any], str]:
        resolved_dest = dict(dest or {})
        if not resolved_dest:
            resolved_dest = build_recovery_dest(default_kind="telegram", session=session, state=state)
        resolved_dest.setdefault("kind", "telegram")
        resolved_context = context
        degradation_message = ""
        if str(resolved_dest.get("kind") or "").strip().lower() != "telegram":
            return resolved_context, resolved_dest, degradation_message
        if resolved_context is None:
            session_uid = str(getattr(getattr(session, "conversation_scope", None), "session_uid", "") or session.id or "-")
            try:
                _deg_lang = resolve_user_lang(self.config, chat_id=getattr(session, "chat_id", None))
            except Exception:
                _deg_lang = "ru"
            degradation_message = t("msg.recovery.degraded_delivery", _deg_lang)
            logging.getLogger(__name__).warning(
                "telegram recovery degraded to final-only delivery mode=%s session_uid=%s run_chat_id=%s",
                str(getattr(session, "active_mode", "") or ""),
                session_uid,
                resolved_dest.get("chat_id"),
            )
            return None, resolved_dest, degradation_message
        if isinstance(resolved_context, TelegramTransportContext):
            return resolved_context, resolved_dest, degradation_message
        return (
            self.build_telegram_transport_context(
                resolved_context,
                session=session,
                chat_id=resolved_dest.get("chat_id"),
                dest=resolved_dest,
                user_id=resolved_dest.get("user_id"),
                message_thread_id=resolved_dest.get("message_thread_id"),
            ),
            resolved_dest,
            degradation_message,
        )

    @staticmethod
    def _with_recovery_degradation(payload: Dict[str, Any], degradation_message: str) -> Dict[str, Any]:
        if not degradation_message:
            return payload
        result = dict(payload or {})
        current_message = str(result.get("message") or "").strip()
        result["message"] = (
            f"{current_message} {degradation_message}".strip()
            if current_message
            else degradation_message
        )
        result["degraded_delivery"] = True
        result["degradation_reason"] = "missing_transport_context"
        return result

    async def _execute_recommended_run_action(
        self,
        *,
        session: Session,
        mode_id: str,
        operation: str,
        run: Any,
        state: Dict[str, Any],
        report: Any,
        context: Any = None,
        dest: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_mode = str(mode_id or "").strip()
        operation_name = str(operation or "").strip()
        try:
            _rec_lang = resolve_user_lang(self.config, chat_id=getattr(session, "chat_id", None))
        except Exception:
            _rec_lang = "ru"
        if not operation_name:
            return {"status": "blocked", "message": t("msg.recovery.operation_not_defined", _rec_lang)}
        mode = self.mode_registry_service.get(resolved_mode) if self.mode_registry_service else None
        if mode is None:
            return {"status": "blocked", "message": t("msg.recovery.mode_unavailable", _rec_lang, mode_id=resolved_mode)}
        resolved_context, resolved_dest, degradation_message = self._resolve_recovery_execution_vector(
            session=session,
            state=state,
            context=context,
            dest=dest,
        )
        custom_executor = getattr(mode, "execute_recovery_action", None)
        if callable(custom_executor):
            payload = await custom_executor(
                session=session,
                action=operation_name,
                run=run,
                state=state,
                report=report,
                bot_app=self,
                context=resolved_context,
                dest=resolved_dest,
            )
            return self._with_recovery_degradation(dict(payload or {}), degradation_message)
        if not hasattr(mode, "run_pipeline"):
            return {"status": "blocked", "message": t("msg.recovery.mode_no_pipeline", _rec_lang, mode_id=resolved_mode)}
        prompt_text = build_recovery_prompt(
            session=session,
            mode_id=resolved_mode,
            action=operation_name,
            state=state,
        )
        if not prompt_text:
            return self._with_recovery_degradation(
                {
                    "status": "blocked",
                    "message": t("bot.recovery_no_inputs", _rec_lang),
                    "executed_operation": operation_name,
                },
                degradation_message,
            )
        artifact_store = RunArtifactStore(self.config)
        latest_before = artifact_store.latest_run(session=session, mode_id=resolved_mode)
        output = await mode.run_pipeline(
            session=session,
            user_text=prompt_text,
            bot_app=self,
            context=resolved_context,
            dest=resolved_dest,
        )
        latest_after = artifact_store.latest_run(session=session, mode_id=resolved_mode)
        payload = {
            "status": "ok",
            "message": str(output or "").strip() or t("bot.op_executed", _rec_lang, op=operation_name),
            "executed_operation": operation_name,
            "executed_via": f"mode_run_pipeline:{operation_name}",
        }
        if latest_after is not None:
            before_run_id = str(getattr(latest_before, "run_id", "") or "")
            after_run_id = str(getattr(latest_after, "run_id", "") or "")
            if after_run_id and after_run_id not in {before_run_id, str(run.run_id)}:
                payload["spawned_run_id"] = after_run_id
        return self._with_recovery_degradation(payload, degradation_message)

    def _clear_agent_session_cache(self, session_id: str) -> None:
        self.session_management._clear_agent_session_cache(session_id)

    def _interrupt_before_close(self, session_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.session_management._interrupt_before_close(session_id, chat_id, context)

    async def close_session_with_cleanup(
        self,
        session_id: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE | None = None,
    ) -> bool:
        owner_chat_id = int(chat_id)
        sid = str(session_id or "").strip()
        if not sid:
            return False

        session = self.manager.get(owner_chat_id, sid)
        if session is None:
            return False

        scope = getattr(session, "conversation_scope", None)
        self._interrupt_before_close(sid, owner_chat_id, context)
        closed = self.manager.close(owner_chat_id, sid)
        if not closed:
            return False

        self._clear_agent_session_cache(sid)
        thread_manager = getattr(self, "session_thread_manager", None)
        if thread_manager is not None:
            try:
                await thread_manager.cleanup_closed_session(
                    owner_chat_id=owner_chat_id,
                    session_id=sid,
                    bot=getattr(context, "bot", None),
                    scope=scope if isinstance(scope, ConversationScope) else None,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "close session cleanup failed chat_id=%s session_id=%s",
                    owner_chat_id,
                    sid,
                )
        return True

    async def ensure_scope_session(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        reply_chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
    ) -> Optional[Session]:
        return await self.session_management.ensure_scope_session(
            chat_id,
            context,
            reply_chat_id=reply_chat_id,
            message_thread_id=message_thread_id,
        )

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id if update and update.effective_chat else None
        if chat_id is not None:
            await self._flush_media_groups_for_chat(chat_id)
        await self.message_processor.process_message(update, context)

    def _has_attachments(self, message: Message) -> bool:
        return any(
            [
                message.document,
                message.photo,
                message.video,
                message.audio,
                message.voice,
                message.sticker,
                message.animation,
                message.video_note,
            ]
        )

    async def on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        route = await self.ensure_telegram_inbound_authorized(update, context)
        if route is None:
            return
        self.metrics.inc("commands")
        _uc_lang = resolve_user_lang(self.config, chat_id=int(route.owner_chat_id))
        await self._send_message(context, text=t("bot.cmd_not_found", _uc_lang), **route.reply_kwargs())

    async def on_pre_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        extractor = getattr(self, "_extract_message_thread_id", None)
        if not callable(extractor):
            extractor = BotApp._extract_message_thread_id
        thread_id = extractor(update)
        # Allow /start for everyone: it only shows the user's identifiers and the "contact admin" hint.
        # This also avoids "Доступ не настроен..." from ensure_allowed for whitelisted-but-unconfigured users.
        try:
            txt = (update.message.text or "").strip() if update.message else ""
        except Exception:
            txt = ""
        try:
            command_name = str(txt.split()[0].split("@", 1)[0].lstrip("/") or "").strip().lower()
        except Exception:
            command_name = ""
        if command_name == "start":
            self._stop_files_rename_wait(chat_id, message_thread_id=thread_id)
            return
        route_authorizer = getattr(self, "ensure_telegram_inbound_authorized", None)
        if callable(route_authorizer):
            route = await route_authorizer(
                update,
                context,
                allow_outside_topic=command_name in OUTSIDE_TOPIC_ALLOWED_COMMANDS,
                emit_denied_message=False,
                emit_unknown_thread_message=False,
            )
            if route is None:
                return
        elif not await self.access_policy_service.ensure_allowed(chat_id, context):
            return
        self._stop_files_rename_wait(chat_id, message_thread_id=thread_id)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        if not chat:
            return
        chat_id = int(chat.id)
        user_id = getattr(user, "id", None)

        _start_lang = resolve_user_lang(self.config, chat_id=chat_id)
        parts = []
        if user_id is not None:
            parts.append(t("bot.your_telegram_id", _start_lang, user_id=int(user_id)))
        parts.append(f"Chat ID: {chat_id}")
        parts.append(t("bot.contact_admin", _start_lang))

        await self._send_message(context, chat_id=chat_id, text="\n".join(parts), md2=True)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        self._stop_files_rename_wait(chat_id, message_thread_id=self._extract_message_thread_id(update))
        await self.message_processor.process_document(update, context)

    def _resolve_unique_file_path(self, target_dir: str, file_name: str) -> str:
        return self._file_upload_handler._resolve_unique_file_path(target_dir, file_name)

    def _stop_files_upload_wait(self, chat_id: int, *, message_thread_id: Optional[int] = None) -> None:
        self._file_upload_handler._stop_files_upload_wait(chat_id, message_thread_id=message_thread_id)

    def _stop_files_rename_wait(self, chat_id: int, *, message_thread_id: Optional[int] = None) -> None:
        self._file_upload_handler._stop_files_rename_wait(chat_id, message_thread_id=message_thread_id)

    async def _files_upload_wait_expire(
        self,
        chat_id: int,
        message_thread_id: Optional[int],
        expires_at: float,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._file_upload_handler._files_upload_wait_expire(chat_id, message_thread_id, expires_at, context)

    def _start_files_upload_wait(
        self,
        chat_id: int,
        target_dir: str,
        root_dir: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        self._file_upload_handler._start_files_upload_wait(
            chat_id, target_dir, root_dir, context, message_thread_id=message_thread_id
        )

    async def _files_rename_wait_expire(
        self,
        chat_id: int,
        message_thread_id: Optional[int],
        expires_at: float,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._file_upload_handler._files_rename_wait_expire(chat_id, message_thread_id, expires_at, context)

    def _start_files_rename_wait(
        self,
        chat_id: int,
        source_path: str,
        root_dir: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        self._file_upload_handler._start_files_rename_wait(
            chat_id, source_path, root_dir, context, message_thread_id=message_thread_id
        )

    async def _maybe_save_pending_uploaded_file(
        self,
        chat_id: int,
        doc: Any,
        data: bytearray,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        return await self._file_upload_handler._maybe_save_pending_uploaded_file(
            chat_id, doc, data, context, message_thread_id=message_thread_id
        )

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.message_processor.process_photo(update, context)

    async def _flush_media_groups_for_chat(
        self,
        chat_id: int,
        exclude_media_group_id: Optional[str] = None,
    ) -> None:
        await self._file_upload_handler._flush_media_groups_for_chat(chat_id, exclude_media_group_id)

    def _clear_media_groups_for_session(self, session: Session) -> int:
        session_id = str(getattr(session, "id", "") or "").strip()
        session_uid = session_runtime_uid(session)
        if not session_id and not session_uid:
            return 0
        keys: set[tuple[int, str]] = set()
        for store in (self.ui_state.media_group_images, self.ui_state.media_group_documents):
            for key, payload in list(store.items()):
                if not isinstance(payload, dict):
                    continue
                payload_session_id = str(payload.get("session_id") or "").strip()
                payload_session_uid = str(payload.get("session_uid") or "").strip()
                if session_id and payload_session_id == session_id:
                    keys.add(key)
                    continue
                if session_uid and payload_session_uid == session_uid:
                    keys.add(key)
        removed = 0
        for key in keys:
            if self.ui_state.media_group_images.pop(key, None) is not None:
                removed += 1
            if self.ui_state.media_group_documents.pop(key, None) is not None:
                removed += 1
            for task_map in (self.ui_state.media_group_tasks, self.ui_state.media_group_document_tasks):
                task = task_map.pop(key, None)
                if task and not task.done():
                    task.cancel()
        return removed

    async def _flush_media_group(self, key: tuple[int, str]) -> None:
        album = self.ui_state.media_group_images.pop(key, None)
        documents = self.ui_state.media_group_documents.pop(key, None)
        image_task = self.ui_state.media_group_tasks.pop(key, None)
        document_task = self.ui_state.media_group_document_tasks.pop(key, None)
        for task in (image_task, document_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()

        if album:
            chat_id = int(album.get("chat_id") or 0)
            session_id = str(album.get("session_id") or "")
            session_uid = str(album.get("session_uid") or "")
            owner_chat_id = int(album.get("owner_chat_id") or 0)
            paths = list(album.get("paths") or [])
            caption = str(album.get("caption") or "").strip()
            context = album.get("context")
            if chat_id and session_id and paths and context is not None:
                session = self.manager.get(owner_chat_id, session_id) if owner_chat_id else None
                if session is None and session_uid:
                    session = self.manager.get_by_uid(session_uid)
                if session is None:
                    session = self.manager.get(chat_id, session_id)
                if session:
                    buffer_key = self.message_buffer_service._scope_buffer_key(session, chat_id)
                    await self._flush_buffer(chat_id, session, context)
                    await self._stage_user_input(
                        session,
                        caption,
                        chat_id,
                        context,
                        dest=self.build_telegram_reply_dest(
                            session,
                            chat_id,
                            user_id=(self.ui_state.message_buffer_user_id or {}).get(buffer_key),
                        ),
                        image_paths=paths,
                    )

        if documents:
            chat_id = int(documents.get("chat_id") or 0)
            session_id = str(documents.get("session_id") or "")
            session_uid = str(documents.get("session_uid") or "")
            owner_chat_id = int(documents.get("owner_chat_id") or 0)
            blocks = [str(x) for x in list(documents.get("blocks") or []) if str(x).strip()]
            caption = str(documents.get("caption") or "").strip()
            context = documents.get("context")
            if chat_id and session_id and blocks and context is not None:
                session = self.manager.get(owner_chat_id, session_id) if owner_chat_id else None
                if session is None and session_uid:
                    session = self.manager.get_by_uid(session_uid)
                if session is None:
                    session = self.manager.get(chat_id, session_id)
                if session:
                    buffer_key = self.message_buffer_service._scope_buffer_key(session, chat_id)
                    await self._flush_buffer(chat_id, session, context)
                    payload_parts = []
                    if caption:
                        payload_parts.append(caption)
                    payload_parts.append("\n\n".join(blocks))
                    await self._stage_user_input(
                        session,
                        "\n\n".join(payload_parts),
                        chat_id,
                        context,
                        dest=self.build_telegram_reply_dest(
                            session,
                            chat_id,
                            user_id=(self.ui_state.message_buffer_user_id or {}).get(buffer_key),
                        ),
                    )

    async def _media_group_wait_and_flush(self, key: tuple[int, str]) -> None:
        try:
            await asyncio.sleep(self.media_group_idle_sec)
            await self._flush_media_group(key)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")

    async def _add_media_group_image(
        self,
        chat_id: int,
        media_group_id: str,
        session_id: str,
        session_uid: Optional[str],
        owner_chat_id: Optional[int],
        context: ContextTypes.DEFAULT_TYPE,
        image_path: str,
        caption: str,
    ) -> None:
        key = (int(chat_id), str(media_group_id))
        album = self.ui_state.media_group_images.get(key)
        if not album:
            album = {
                "chat_id": int(chat_id),
                "session_id": str(session_id),
                "session_uid": str(session_uid or ""),
                "owner_chat_id": int(owner_chat_id or 0),
                "paths": [],
                "caption": "",
                "context": context,
            }
            self.ui_state.media_group_images[key] = album
        album["session_id"] = str(session_id)
        album["session_uid"] = str(session_uid or "")
        album["owner_chat_id"] = int(owner_chat_id or 0)
        album["context"] = context
        album["paths"].append(image_path)
        if caption and not album.get("caption"):
            album["caption"] = caption.strip()
        old_task = self.ui_state.media_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        self.ui_state.media_group_tasks[key] = asyncio.create_task(self._media_group_wait_and_flush(key))

    async def _add_media_group_document(
        self,
        *,
        chat_id: int,
        media_group_id: str,
        session_id: str,
        session_uid: Optional[str],
        owner_chat_id: Optional[int],
        context: ContextTypes.DEFAULT_TYPE,
        block: str,
        caption: str,
    ) -> None:
        key = (int(chat_id), str(media_group_id))
        group = self.ui_state.media_group_documents.get(key)
        if not group:
            group = {
                "chat_id": int(chat_id),
                "session_id": str(session_id),
                "session_uid": str(session_uid or ""),
                "owner_chat_id": int(owner_chat_id or 0),
                "blocks": [],
                "caption": "",
                "context": context,
            }
            self.ui_state.media_group_documents[key] = group
        group["session_id"] = str(session_id)
        group["session_uid"] = str(session_uid or "")
        group["owner_chat_id"] = int(owner_chat_id or 0)
        group["context"] = context
        group["blocks"].append(str(block))
        if caption and not group.get("caption"):
            group["caption"] = caption.strip()
        old_task = self.ui_state.media_group_document_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        self.ui_state.media_group_document_tasks[key] = asyncio.create_task(self._media_group_wait_and_flush(key))

    async def _store_image_bytes(
        self,
        session: Session,
        data: bytearray,
        filename: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> Optional[str]:
        safe_name = os.path.basename(filename) or "image.jpg"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base_dir = self.config.defaults.image_temp_dir
        if os.path.isabs(base_dir):
            img_dir = base_dir
        else:
            img_dir = os.path.join(session.workdir, base_dir)
        os.makedirs(img_dir, exist_ok=True)
        self._cleanup_image_dir(img_dir)
        out_name = f"{stamp}_{safe_name}"
        image_path = os.path.join(img_dir, out_name)
        try:
            with open(image_path, "wb") as f:
                f.write(data)
            return image_path
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_message(context, chat_id=chat_id, text=f"Не удалось сохранить изображение: {e}")
            return None

    async def _store_attachment_bytes(
        self,
        session: Session,
        data: bytearray,
        filename: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> Optional[str]:
        safe_name = os.path.basename(filename) or "attachment.txt"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base_dir = self.config.defaults.image_temp_dir
        if os.path.isabs(base_dir):
            attachment_dir = base_dir
        else:
            attachment_dir = os.path.join(session.workdir, base_dir)
        os.makedirs(attachment_dir, exist_ok=True)
        self._cleanup_image_dir(attachment_dir)
        out_name = f"{stamp}_{safe_name}"
        attachment_path = self._resolve_unique_file_path(attachment_dir, out_name)
        try:
            with open(attachment_path, "wb") as f:
                f.write(bytes(data))
            return attachment_path
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_message(context, chat_id=chat_id, text=f"Не удалось сохранить файл: {e}")
            return None

    async def _handle_image_bytes(
        self,
        session: Session,
        data: bytearray,
        filename: str,
        caption: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        image_path = await self._store_image_bytes(session, data, filename, chat_id, context)
        if not image_path:
            return
        await self._stage_user_input(
            session,
            caption.strip(),
            chat_id,
            context,
            image_paths=[image_path],
        )

    def _cleanup_image_dir(self, img_dir: str) -> None:
        cutoff = time.time() - 24 * 60 * 60
        try:
            for entry in os.scandir(img_dir):
                if not entry.is_file():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.remove(entry.path)
                except Exception as e:
                    logging.getLogger(__name__).warning("image cleanup skipped path=%s err=%s", entry.path, e)
                    continue
        except Exception as e:
            logging.getLogger(__name__).warning("image cleanup failed dir=%s err=%s", img_dir, e)
            return

    async def _handle_cli_input(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        dest: Optional[dict] = None,
        image_path: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
    ) -> None:
        await self.input_dispatch_service.handle_cli_input(
            session,
            text,
            chat_id,
            context,
            dest=dest,
            image_path=image_path,
            image_paths=image_paths,
        )

    async def _handle_user_input(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        dest: Optional[dict] = None,
    ) -> None:
        await self.input_dispatch_service.handle_user_input(
            session,
            text,
            chat_id,
            context,
            dest=dest,
        )

    async def _stage_user_input(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        dest: Optional[dict] = None,
        image_path: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
    ) -> None:
        await self.input_dispatch_service.stage_user_input(
            session,
            text,
            chat_id,
            context,
            dest=dest,
            image_path=image_path,
            image_paths=image_paths,
        )

    def _mode_allows_plugin_ui(self, session: Optional[Session]) -> bool:
        return self.mode_registry_service.allows_agent_plugin_ui(session)

    async def _buffer_or_send(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: Optional[int] = None,
        direct_messages_topic_id: Optional[int] = None,
    ) -> None:
        await self.message_buffer_service.buffer_or_send(
            session=session,
            text=text,
            chat_id=chat_id,
            context=context,
            user_id=user_id,
            direct_messages_topic_id=direct_messages_topic_id,
        )

    async def _schedule_flush(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.message_buffer_service.schedule_flush(chat_id, session, context)

    async def _flush_after_delay(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.message_buffer_service.flush_after_delay(chat_id, session, context)

    async def _flush_buffer(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.message_buffer_service.flush_buffer(chat_id, session, context)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.callbacks.handle_callback(update, context)

    async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_tools(update, context)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_new(update, context)

    async def cmd_newpath(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_newpath(update, context)

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_sessions(update, context)

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_close(update, context)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_status(update, context)

    async def cmd_reports(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_reports(update, context)

    async def cmd_limits(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_limits(update, context)

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode_id: str) -> None:
        await self.handlers.cmd_mode(update, context, mode_id)

    async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_interrupt(update, context)

    def _start_mode_task(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        mode_id: Optional[str] = None,
    ) -> None:
        self.session_management.start_mode_task(session, prompt, dest, context, mode_id=mode_id)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_queue(update, context)

    async def cmd_clearqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_clearqueue(update, context)

    async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_rename(update, context)

    async def cmd_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_dirs(update, context)

    async def cmd_cwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_cwd(update, context)

    async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_git(update, context)

    async def cmd_selfupdate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_selfupdate(update, context)

    async def cmd_setprompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_setprompt(update, context)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_resume(update, context)

    async def cmd_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_state(update, context)

    async def cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_send(update, context)

    def _bot_commands(self, *, include_admin: bool = False) -> list[BotCommand]:
        commands = []
        for entry in build_command_registry(self):
            if not entry["menu"]:
                continue
            if bool(entry.get("admin_only")) and not include_admin:
                continue
            commands.append(BotCommand(command=entry["name"], description=str(entry["desc"])))
        return commands

    async def set_bot_commands(self, app: Application) -> None:
        await app.bot.set_my_commands(
            self._bot_commands(include_admin=False),
            scope=BotCommandScopeDefault(),
        )
        admin_commands = self._bot_commands(include_admin=True)
        for chat_id in list(getattr(self.config.telegram, "admlist_chat_ids", []) or []):
            await app.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=int(chat_id)),
            )

    async def cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_files(update, context)

    async def cmd_miniapp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        route = await self.ensure_telegram_inbound_authorized(
            update,
            context,
            allow_outside_topic=True,
        )
        if route is None:
            return
        chat_id = int(route.reply_chat_id)
        _ma_lang = resolve_user_lang(self.config, chat_id=chat_id)
        if not bool(getattr(self.config.miniapp, "enabled", False)):
            await self._send_message(context, chat_id=chat_id, text=t("bot.miniapp_disabled", _ma_lang))
            return
        url = self._build_miniapp_webapp_url()
        if not url:
            await self._send_message(
                context,
                chat_id=chat_id,
                text=t("bot.miniapp_url_not_set", _ma_lang),
                md2=True,
            )
            return
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("bot.miniapp_open_btn", _ma_lang), web_app=WebAppInfo(url=url))]]
        )
        await self._send_message(
            context,
            chat_id=chat_id,
            text=t("bot.miniapp_open_text", _ma_lang),
            reply_markup=kb,
            md2=True,
        )

    def _build_miniapp_webapp_url(self) -> Optional[str]:
        return self.runtime_service.build_miniapp_webapp_url()

    async def reload_runtime_config(self) -> Dict[str, Any]:
        return await self.runtime_service.reload_runtime_config()

    def _list_dir_entries(self, base: str) -> list[dict]:
        return self.runtime_service.list_dir_entries(base)

    async def _send_files_menu(
        self,
        chat_id: int,
        session: Session,
        context: ContextTypes.DEFAULT_TYPE,
        edit_message: Optional[object],
        message_thread_id: Optional[int] = None,
    ) -> None:
        await self.handlers._send_files_menu(
            chat_id,
            session,
            context,
            edit_message,
            message_thread_id=message_thread_id,
        )

    def _preset_commands(self) -> Dict[str, str]:
        return self.runtime_service.preset_commands()

    def _guess_clone_path(self, url: str, base: str) -> Optional[str]:
        return self.runtime_service.guess_clone_path(url, base)

    async def cmd_preset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_preset(update, context)

    async def cmd_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_metrics(update, context)

    async def cmd_lint_evolution_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.handlers.cmd_lint_evolution_status(update, context)

    async def cmd_lint_autopause_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.handlers.cmd_lint_autopause_resume(update, context)

    async def cmd_lint_schema_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.handlers.cmd_lint_schema_history(update, context)

    async def cmd_lint_gate_dry_run(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self.handlers.cmd_lint_gate_dry_run(update, context)

    async def cmd_sessions_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_sessions_search(update, context)

    async def cmd_git_branch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_git_branch(update, context)

    async def cmd_git_checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_git_checkout(update, context)

    async def cmd_git_stash_pop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_git_stash_pop(update, context)

    async def cmd_git_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_git_show(update, context)

    async def cmd_remote_git_pull(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_remote_git_pull(update, context)

    async def cmd_remote_git_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_remote_git_push(update, context)

    async def cmd_remote_git_fetch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_remote_git_fetch(update, context)

    async def run_prompt_raw(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        *,
        chat_id: Optional[int] = None,
        source: str = "raw_prompt",
        task_bearing: bool = True,
        technical_command: Optional[bool] = None,
    ) -> str:
        return await self.session_management.run_prompt_raw(
            prompt,
            session_id,
            chat_id=chat_id,
            source=source,
            task_bearing=task_bearing,
            technical_command=technical_command,
        )

    async def _send_dirs_menu(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        base: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        await self.dirs_service.send_menu(
            int(chat_id),
            context,
            str(base),
            message_thread_id=message_thread_id,
        )

    async def shutdown_runtime(self) -> None:
        """Gracefully stop mode tasks and running CLI processes before application shutdown."""
        self._shutdown_in_progress = True
        sessions: list[Session] = []
        try:
            by_chat = getattr(self.manager, "sessions_by_chat", {}) or {}
            for sessions_map in by_chat.values():
                if not isinstance(sessions_map, dict):
                    continue
                for session in sessions_map.values():
                    if isinstance(session, Session):
                        sessions.append(session)
        except Exception:
            logging.getLogger(__name__).exception("failed to collect sessions for shutdown")
            sessions = []

        for session in sessions:
            session._preserve_tmux_on_shutdown = True
            try:
                session.interrupt()
            except Exception:
                logging.getLogger(__name__).exception("session interrupt failed on shutdown sid=%s", session.id)

        cancel_jobs = []
        seen_ids: set[str] = set()
        for session in sessions:
            sid = str(getattr(session, "id", "") or "").strip()
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            cancel_jobs.append(self.mode_tasks.cancel_session(session_id=sid, timeout_s=1.0))
        if cancel_jobs:
            results = await asyncio.gather(*cancel_jobs, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logging.getLogger(__name__).error(
                        "mode tasks cancel failed on shutdown",
                        exc_info=(type(res), res, res.__traceback__),
                    )

        for session in sessions:
            try:
                session.close(preserve_tmux=True)
            except Exception:
                logging.getLogger(__name__).exception("session close failed on shutdown sid=%s", session.id)

        # Shutdown async services that spawn background workers.
        for svc_name in ("notification_queue_service", "system_event_bus"):
            svc = getattr(self, svc_name, None)
            if svc is not None and hasattr(svc, "shutdown"):
                try:
                    await svc.shutdown()
                except Exception:
                    logging.getLogger(__name__).debug("shutdown %s failed", svc_name, exc_info=True)

        # Останавливаем MCP-клиенты через plugin_registry.
        plugin_registry = getattr(self, "_tool_registry", None)
        close_mcp = getattr(plugin_registry, "close_mcp", None) if plugin_registry is not None else None
        if callable(close_mcp):
            try:
                await close_mcp()
            except Exception:
                logging.getLogger(__name__).exception("shutdown mcp clients failed")

    def shutdown_html_process_pool(self) -> None:
        pool = getattr(self, "_html_process_pool", None)
        if pool is None:
            return
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python versions without cancel_futures support.
            pool.shutdown(wait=False)
        finally:
            self._html_process_pool = None
            try:
                self.session_management.set_html_process_pool(None)
            except Exception:
                logging.getLogger(__name__).exception("failed to clear HTML process pool from session management")


def build_app(config: AppConfig) -> Application:
    # Increase HTTPX timeouts to reduce intermittent ConnectTimeout/TimedOut on Telegram API calls
    # (e.g. answer_callback_query). Values are configurable via config.yaml under "telegram".
    request = HTTPXRequest(
        connection_pool_size=int(getattr(config.telegram, "connection_pool_size", 8)),
        connect_timeout=float(getattr(config.telegram, "connect_timeout_sec", 20.0)),
        read_timeout=float(getattr(config.telegram, "read_timeout_sec", 20.0)),
        write_timeout=float(getattr(config.telegram, "write_timeout_sec", 20.0)),
        pool_timeout=float(getattr(config.telegram, "pool_timeout_sec", 10.0)),
    )
    app = Application.builder().token(config.telegram.token).request(request).build()
    bot_app = BotApp(config)
    register_handlers(app=app, bot_app=bot_app, config=config)
    app.post_init = build_post_init(bot_app)
    app.post_shutdown = build_post_shutdown(bot_app)
    app.add_error_handler(build_error_handler())
    return app


def main() -> None:
    # Add ~/.local/bin to PATH for CLI tools installed in user directory
    # (e.g., claude-code installed via npm in home directory)
    home_local_bin = os.path.expanduser("~/.local/bin")
    if os.path.isdir(home_local_bin):
        os.environ["PATH"] = home_local_bin + os.pathsep + os.environ.get("PATH", "")
        logging.getLogger(__name__).info("Added ~/.local/bin to PATH: %s", home_local_bin)

    # Ensure .env is loaded early for the whole process (plugins may read os.environ).
    # load_config() also loads .env near config, but this keeps behavior robust if config
    # path changes or config loading is refactored.
    try:
        load_dotenv_near(CONFIG_PATH, filename=".env", override=False)
    except Exception:
        logging.getLogger(__name__).exception("dotenv preload failed path=%s", CONFIG_PATH)
    load_validated_settings(CONFIG_PATH)
    config = load_config(CONFIG_PATH)
    app = build_app(config)
    app.run_polling(
        poll_interval=float(getattr(config.telegram, "poll_interval_sec", 0.0)),
        timeout=int(getattr(config.telegram, "polling_timeout_sec", 5)),
    )


if __name__ == "__main__":
    main()
