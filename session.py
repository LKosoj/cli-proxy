import asyncio
import os
import errno
import signal
import time
import re
import uuid
import shutil
from collections import deque
from dataclasses import InitVar, dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Deque, Dict, List, Optional

import logging
import pexpect
import subprocess
import shlex
import threading

from app.services.claude_jsonl_monitor import ClaudeJsonlMonitor
from app.services.cli_json_stream import (
    CliJsonStreamRecorder,
    build_cli_json_stream_adapter,
    cli_json_stream_archive_enabled,
    recover_cli_text_from_raw_stream,
)
from app.services.gemini_session_monitor import GeminiJsonMonitor
from app.services.tool_availability import available_tools, is_tool_available
from app.services.project_prompts_service import ensure_project_prompts
from app.services.ssh_skill_generator import generate_ssh_skill_text
from app.services.state_repository import get_state_repository
from app.services.session_tick_history_store import append_session_tick
from app.services.qwen_jsonl_monitor import QwenJsonlMonitor, extract_progress_text
from config import AppConfig, ToolConfig, save_config
from modes.sdk.runtime.cli_contracts import parse_bundle_for_response_format
from sessions.conversation_scope import ConversationScope, DesktopScope
from sessions.queue_item import normalize_queue_item_payload
from sessions.scoped_key import (
    build_session_scoped_key,
    is_session_scoped_key,
    sanitize_scoped_key_token as _sanitize_scoped_key_token,
    session_scoped_key,
)
from utils.cli import build_command, detect_prompt_regex, detect_resume_regex, resolve_env_value
from utils.paths import legacy_sandbox_session_dir, sandbox_session_dir
from utils.text import extract_tick_tokens, is_time_only_text, strip_ansi

logger = logging.getLogger(__name__)

_HEADLESS_WAIT_POLL_SEC = 2.0
_HEADLESS_EOF_EXIT_TIMEOUT_SEC = 10.0
_HEADLESS_EOF_STOP_GRACE_SEC = 1.0
_CLI_RESPONSE_FORMAT_RE = re.compile(r"CLI_RESPONSE_FORMAT:\s*([a-z0-9_]+)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# .gitignore guard for .cli-proxy/
# ---------------------------------------------------------------------------

_gitignore_checked: set = set()

# Singleton lock serialising save_config() from prompt/resume-regex autodetection (H3).
_CONFIG_SAVE_LOCK = threading.Lock()


def ensure_cli_proxy_gitignored(workdir: str) -> None:
    """Ensure ``.cli-proxy/`` is listed in the project's ``.gitignore``.

    * If ``.gitignore`` exists but lacks the entry — appends it.
    * If ``.gitignore`` does not exist — creates it with the entry.
    * Results are cached per *realpath* so repeated calls are cheap.
    * Errors are logged but never raised.
    """
    real = os.path.realpath(workdir)
    if real in _gitignore_checked:
        return
    try:
        _do_ensure_cli_proxy_gitignored(real)
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to ensure .cli-proxy in .gitignore for %s", workdir,
            exc_info=True,
        )
    _gitignore_checked.add(real)


def _do_ensure_cli_proxy_gitignored(workdir: str) -> None:
    gitignore_path = os.path.join(workdir, ".gitignore")
    marker = ".cli-proxy/"
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == ".cli-proxy" or stripped == ".cli-proxy/":
                return
        suffix = "\n" if content and not content.endswith("\n") else ""
        with open(gitignore_path, "a", encoding="utf-8") as fh:
            fh.write(f"{suffix}{marker}\n")
    else:
        with open(gitignore_path, "w", encoding="utf-8") as fh:
            fh.write(f"{marker}\n")


def _normalize_cli_name(value: Any) -> str:
    return str(value or "").strip()


def _extract_cli_response_format(prompt: str) -> str:
    match = _CLI_RESPONSE_FORMAT_RE.search(str(prompt or ""))
    return str(match.group(1) or "").strip().lower() if match else ""


def _is_complete_structured_cli_output(text: str, *, response_format: str) -> bool:
    normalized_format = str(response_format or "").strip().lower()
    if not normalized_format:
        return False
    try:
        return parse_bundle_for_response_format(str(text or ""), normalized_format) is not None
    except Exception:
        return False


def session_active_cli_name(session: Any) -> str:
    """Return the session's current active CLI name from nested or legacy state."""
    return _normalize_cli_name(
        getattr(getattr(session, "cli", None), "active_cli", "")
        or getattr(session, "active_cli", "")
        or getattr(getattr(session, "tool", None), "name", "")
    )


def pick_runtime_available_cli(config: Optional[AppConfig], preferred: Optional[str] = None) -> Optional[str]:
    """
    Pick a CLI that is enabled and currently available for execution.

    Unlike SessionManager._pick_initial_cli(), this helper never falls back to a
    merely configured CLI because it is used right before real runtime execution.
    """
    if config is None:
        return None
    preferred_name = _normalize_cli_name(preferred) or None
    default_cli = _normalize_cli_name(getattr(getattr(config, "defaults", None), "default_cli", None)) or None
    candidates = [preferred_name, default_cli, "qwen"]
    available = available_tools(config)
    for candidate in candidates:
        if candidate and candidate in available:
            return candidate
    if available:
        return sorted(available)[0]
    return None


def remember_session_cli_switch_notice(session: Any, previous_cli: Optional[str], active_cli: Optional[str]) -> None:
    cli_state = getattr(session, "cli", None)
    if cli_state is None:
        return
    target = _normalize_cli_name(active_cli)
    previous = _normalize_cli_name(previous_cli)
    if not target or previous == target:
        return
    cli_state.pending_switch_notice = {
        "from": previous,
        "to": target,
    }


def consume_session_cli_switch_notice_text(session: Any, lang: str = "ru") -> Optional[str]:
    cli_state = getattr(session, "cli", None)
    if cli_state is None:
        return None
    raw_notice = getattr(cli_state, "pending_switch_notice", None)
    cli_state.pending_switch_notice = None
    if not isinstance(raw_notice, dict):
        return None
    previous = _normalize_cli_name(raw_notice.get("from"))
    target = _normalize_cli_name(raw_notice.get("to"))
    if not target or previous == target:
        return None
    from i18n import t
    if previous:
        return t("msg.session.cli_switch_notice_with_prev", lang, previous=previous, target=target)
    return t("msg.session.cli_switch_notice_no_prev", lang, target=target)


def _strip_transient_codex_stderr_blocks(stderr_text: str) -> tuple[str, int]:
    """Remove known recoverable Codex router noise from session stderr logs."""
    kept_lines: List[str] = []
    suppressed_lines = 0
    suppress_block = False

    for raw_line in strip_ansi(str(stderr_text or "")).splitlines():
        line = raw_line.rstrip("\n")
        if suppress_block:
            if not line.strip() or line.startswith((" ", "\t")):
                suppressed_lines += 1
                continue
            suppress_block = False
        if "codex_core::tools::router" in line and "apply_patch verification failed" in line:
            suppressed_lines += 1
            suppress_block = True
            continue
        kept_lines.append(line)

    return "\n".join(kept_lines).strip(), suppressed_lines


def _prepare_headless_stderr_for_logging(
    tool_name: str,
    stderr_text: str,
    *,
    returncode: Optional[int],
    has_user_output: bool,
) -> tuple[str, int]:
    cleaned = strip_ansi(str(stderr_text or "")).strip()
    if not cleaned:
        return "", 0
    if (tool_name or "").strip().lower() != "codex" or returncode != 0 or not has_user_output:
        return cleaned, 0
    return _strip_transient_codex_stderr_blocks(cleaned)


@dataclass(frozen=True)
class SessionCliSwitchResult:
    switched: bool
    previous_cli: Optional[str]
    active_cli: Optional[str]
    transfer_available: bool = False
    source_session_id: Optional[str] = None


EXECUTION_BACKEND_HEADLESS = "headless"
EXECUTION_BACKEND_TMUX = "tmux"
EXECUTION_BACKEND_INTERACTIVE = "interactive"
SESSION_EXECUTION_BACKENDS = frozenset({EXECUTION_BACKEND_HEADLESS, EXECUTION_BACKEND_TMUX})
RUNTIME_EXECUTION_BACKENDS = frozenset(
    {EXECUTION_BACKEND_HEADLESS, EXECUTION_BACKEND_TMUX, EXECUTION_BACKEND_INTERACTIVE}
)


@dataclass(frozen=True)
class SessionBackendSwitchResult:
    switched: bool
    previous_backend: Optional[str]
    active_backend: Optional[str]
    active_cli: Optional[str]
    reason: Optional[str] = None


def _normalize_execution_backend(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _tool_execution_backends(tool: Any) -> list[str]:
    configured = getattr(tool, "execution_backends", None)
    if isinstance(configured, (list, tuple)):
        result: list[str] = []
        for item in configured:
            backend = _normalize_execution_backend(item)
            if backend in SESSION_EXECUTION_BACKENDS and backend not in result:
                result.append(backend)
        if result:
            return result
    mode = str(getattr(tool, "mode", "") or "").strip().lower()
    if mode == "headless":
        return [EXECUTION_BACKEND_HEADLESS]
    if mode == "interactive":
        return [EXECUTION_BACKEND_INTERACTIVE]
    return []


def available_execution_backends(session: Any, cli_name: Optional[str] = None) -> list[str]:
    config = getattr(session, "config", None)
    tool = getattr(session, "tool", None)
    name = str(cli_name or session_active_cli_name(session) or "").strip()
    if config is not None and name and name in (getattr(config, "tools", None) or {}):
        tool = config.tools[name]
    return _tool_execution_backends(tool)


def get_session_execution_backend(session: Any, cli_name: Optional[str] = None) -> str:
    active_cli = str(cli_name or session_active_cli_name(session) or "").strip()
    available = available_execution_backends(session, active_cli)
    config = getattr(session, "config", None)
    tool = getattr(session, "tool", None)
    if config is not None and active_cli and active_cli in (getattr(config, "tools", None) or {}):
        tool = config.tools[active_cli]

    for candidate in (
        _normalize_execution_backend(getattr(tool, "default_execution_backend", None)),
        _normalize_execution_backend(getattr(getattr(config, "defaults", None), "default_execution_backend", None)),
        EXECUTION_BACKEND_HEADLESS if str(getattr(tool, "mode", "") or "").strip().lower() == "headless" else "",
        EXECUTION_BACKEND_INTERACTIVE if str(getattr(tool, "mode", "") or "").strip().lower() == "interactive" else "",
    ):
        if candidate in available:
            return candidate
    return available[0] if available else EXECUTION_BACKEND_HEADLESS


def session_execution_backend_switch_blockers(session: Any) -> list[str]:
    blockers: list[str] = []
    if bool(getattr(session, "busy", False)):
        blockers.append("busy")
    run_lock = getattr(session, "run_lock", None)
    if run_lock is not None and hasattr(run_lock, "locked") and run_lock.locked():
        blockers.append("run_lock")
    if len(getattr(session, "queue", None) or []) > 0:
        blockers.append("queue")
    is_active_by_tick = getattr(session, "is_active_by_tick", None)
    if callable(is_active_by_tick):
        try:
            if bool(is_active_by_tick()):
                blockers.append("tick_active")
        except Exception:
            logger.exception("session backend switch tick activity check failed session_id=%s", getattr(session, "id", None))
    return blockers


def set_session_execution_backend(
    session: Any,
    backend: str,
    cli_name: Optional[str] = None,
) -> SessionBackendSwitchResult:
    active_cli = str(cli_name or session_active_cli_name(session) or "").strip()
    requested = _normalize_execution_backend(backend)
    previous = get_session_execution_backend(session, active_cli)
    available = available_execution_backends(session, active_cli)
    if requested == previous and requested in available:
        return SessionBackendSwitchResult(False, previous, previous, active_cli)
    if requested not in SESSION_EXECUTION_BACKENDS:
        return SessionBackendSwitchResult(False, previous, previous, active_cli, reason="unsupported backend")
    if requested not in available:
        return SessionBackendSwitchResult(False, previous, previous, active_cli, reason="backend not available for cli")
    return SessionBackendSwitchResult(
        switched=False,
        previous_backend=previous,
        active_backend=previous,
        active_cli=active_cli,
        reason="execution backend is configured in settings",
    )


async def switch_session_active_cli_if_needed(session: Any) -> SessionCliSwitchResult:
    """
    Ensure the session points to an executable CLI before direct execution.

    If the active CLI is unavailable, switch to any available fallback and store a
    transient notice so the caller can report the switch in the same interaction channel.
    """
    config = getattr(session, "config", None)
    current = session_active_cli_name(session) or None
    if config is None:
        return SessionCliSwitchResult(switched=False, previous_cli=current, active_cli=current)

    configured = getattr(config, "tools", None) or {}
    current_is_ready = bool(current and current in configured and is_tool_available(config, current))
    if current_is_ready:
        return SessionCliSwitchResult(switched=False, previous_cli=current, active_cli=current)

    # Capture source session info BEFORE switching (resume_token still points to current CLI).
    source_token = None
    cli_state = getattr(session, "cli", None)
    if cli_state is not None and current:
        source_token = (getattr(cli_state, "resume_tokens", None) or {}).get(current)

    fallback = pick_runtime_available_cli(config, preferred=current)
    if not fallback:
        if current and current in configured:
            raise RuntimeError(f"Нет доступных CLI: текущий {current} выключен или недоступен.")
        if current:
            raise RuntimeError(f"Нет доступных CLI: текущий {current} больше не настроен.")
        raise RuntimeError("Нет доступных CLI для этой сессии.")

    await session.set_active_cli_persistent(fallback)
    remember_session_cli_switch_notice(session, current, fallback)
    has_transfer = bool(source_token and str(source_token).strip())
    return SessionCliSwitchResult(
        switched=bool(current != fallback),
        previous_cli=current,
        active_cli=fallback,
        transfer_available=has_transfer,
        source_session_id=str(source_token).strip() if has_transfer else None,
    )


def session_runtime_uid(session: Any) -> str:
    """Return the canonical runtime session UID for real and fake session objects."""
    scope = getattr(session, "scope", None)
    if scope is None:
        scope = getattr(session, "conversation_scope", None)
    token = str(getattr(scope, "session_uid", "") or "").strip()
    session_id = str(getattr(session, "id", "") or "").strip()
    if token:
        if token.startswith("chat:") and token.count(":") == 1 and session_id:
            return f"{token}:{session_id}"
        return token
    if not session_id:
        return ""
    return f"desktop:{session_id}"


@dataclass
class CliState:
    active_cli: Optional[str] = None
    resume_tokens: Dict[str, Optional[str]] = field(default_factory=dict)
    execution_backends: Dict[str, str] = field(default_factory=dict)
    tmux_users: Dict[str, Optional[str]] = field(default_factory=dict)
    cli_work_type: Optional[str] = None
    auto_commands_ran: bool = False
    pending_switch_notice: Optional[Dict[str, str]] = field(default=None, repr=False)


@dataclass
class GitState:
    busy: bool = False
    conflict: bool = False
    conflict_files: list[str] = field(default_factory=list)
    conflict_kind: Optional[str] = None
    # Мьютекс для атомарного check-and-set busy; не сериализуется
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)


@dataclass
class ModeState:
    active_mode: Optional[str] = None
    analyst_mode: str = "spec"
    analyst_template_id: str = "default"
    manager_quiet_mode: bool = False
    agent_memory: Dict[str, Any] = field(default_factory=dict)
    ssh_remote_enabled: bool = False
    remote_control_enabled: bool = False
    remote_control_host_alias: Optional[str] = None


@dataclass
class OrchestratorState:
    enabled: bool = False
    pending_input: Optional[Dict[str, Any]] = field(default=None, repr=False)
    last_mode_output: Optional[str] = field(default=None, repr=False)
    last_mode_id: Optional[str] = field(default=None, repr=False)


@dataclass
class SddState:
    feature_slug: Optional[str] = None
    spec_dir: Optional[str] = None          # specs/<NNN>-<slug>
    phase: str = "idle"                     # idle|specify|plan|tasks|analyze|handoff|done
    pending_gate: Optional[str] = None
    constitution_path: Optional[str] = None
    source_intent: Optional[str] = None
    last_action: str = ""                   # transient UX state: "gate_revise" survives restart
    project_init_status: str = "idle"       # idle|confirming|running|done|failed|cancelled
    project_init_step: str = ""
    project_init_kind: str = ""             # existing_codebase|empty_repo
    project_init_started_at: Optional[float] = None
    project_init_finished_at: Optional[float] = None
    project_init_error: str = ""
    project_profile_path: str = ""
    project_init_snapshot_path: str = ""


# Backward-compatible aliases for existing imports/tests.
SessionModesState = ModeState
SessionOrchestratorState = OrchestratorState


@dataclass
class Session:
    id: str
    tool: ToolConfig
    workdir: str
    idle_timeout_sec: int
    config: AppConfig
    name: Optional[str] = None
    busy: bool = False
    queue: Deque[Any] = field(default_factory=deque)
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    child: Optional[pexpect.spawn] = None
    current_proc: Optional[asyncio.subprocess.Process] = None
    _headless_interrupt_flag: bool = False
    _active_execution_backend: str = field(default="none", init=False, repr=False)
    _preserve_tmux_on_shutdown: bool = field(default=False, init=False, repr=False)
    last_tmux_request_id: Optional[str] = field(default=None, init=False, repr=False)
    cli: CliState = field(default_factory=CliState)
    git: GitState = field(default_factory=GitState)
    modes: ModeState = field(default_factory=ModeState)
    orchestrator: OrchestratorState = field(default_factory=OrchestratorState)
    sdd: SddState = field(default_factory=SddState)
    # Legacy init arguments are accepted for backward compatibility and folded into nested state.
    active_cli: InitVar[Optional[str]] = None
    resume_tokens: InitVar[Optional[Dict[str, Optional[str]]]] = None
    auto_commands_ran: InitVar[Optional[bool]] = None
    cli_work_type: InitVar[Optional[str]] = None
    git_busy: InitVar[Optional[bool]] = None
    git_conflict: InitVar[Optional[bool]] = None
    git_conflict_files: InitVar[Optional[List[str]]] = None
    git_conflict_kind: InitVar[Optional[str]] = None
    analyst_template_id: InitVar[Optional[str]] = None
    manager_quiet_mode: InitVar[Optional[bool]] = None
    agent_memory: InitVar[Optional[Dict[str, Any]]] = None
    started_at: Optional[float] = None
    last_output_ts: Optional[float] = None
    last_tick_ts: Optional[float] = None
    last_tick_value: Optional[str] = None
    last_assistant_text_ts: Optional[float] = None
    last_assistant_text_value: Optional[str] = None
    assistant_preview_message_id: Optional[int] = None
    assistant_preview_last_value: Optional[str] = None
    assistant_preview_creation_attempted: bool = False
    tick_seen: int = 0
    # Optional executor profile id used by Dispatcher.
    executor_profile: Optional[str] = None
    project_root: Optional[str] = None
    # Per-session "state" (previously stored by tool+workdir). This avoids collisions when
    # multiple sessions share the same tool/workdir.
    state_summary: Optional[str] = None
    state_updated_at: Optional[float] = None
    headless_forced_stop: Optional[str] = None
    last_cli_raw_stream_path: Optional[str] = None
    last_cli_normalized_stream_path: Optional[str] = None
    chat_id: Optional[int] = None
    conversation_scope: Optional[ConversationScope] = None
    scoped_key: Optional[str] = None
    # Qwen Code JSONL monitor for real-time progress tracking
    _qwen_monitor: Optional[QwenJsonlMonitor] = field(default=None, repr=False, init=False)
    # Claude Code JSONL monitor for local transcript progress tracking
    _claude_monitor: Optional[ClaudeJsonlMonitor] = field(default=None, repr=False, init=False)
    # Gemini CLI session JSON monitor for real-time progress tracking
    _gemini_monitor: Optional[GeminiJsonMonitor] = field(default=None, repr=False, init=False)
    # Shared SSH service reference (set by SessionManager, not per-session).
    _ssh_service: Optional[Any] = field(default=None, repr=False, init=False)

    def __post_init__(
        self,
        active_cli: Optional[str],
        resume_tokens: Optional[Dict[str, Optional[str]]],
        auto_commands_ran: Optional[bool],
        cli_work_type: Optional[str],
        git_busy: Optional[bool],
        git_conflict: Optional[bool],
        git_conflict_files: Optional[List[str]],
        git_conflict_kind: Optional[str],
        analyst_template_id: Optional[str],
        manager_quiet_mode: Optional[bool],
        agent_memory: Optional[Dict[str, Any]],
    ) -> None:
        if not isinstance(self.cli, CliState):
            self.cli = CliState()
        if not isinstance(self.git, GitState):
            self.git = GitState()
        if not isinstance(self.modes, ModeState):
            self.modes = ModeState()
        if not isinstance(self.orchestrator, OrchestratorState):
            self.orchestrator = OrchestratorState()
        if not isinstance(self.sdd, SddState):
            self.sdd = SddState()

        if active_cli is not None:
            self.cli.active_cli = str(active_cli or "").strip() or None
        if isinstance(resume_tokens, dict):
            self.cli.resume_tokens = dict(resume_tokens)
        elif not isinstance(self.cli.resume_tokens, dict):
            self.cli.resume_tokens = {}
        if not isinstance(getattr(self.cli, "execution_backends", None), dict):
            self.cli.execution_backends = {}
        if not isinstance(getattr(self.cli, "tmux_users", None), dict):
            self.cli.tmux_users = {}
        for cli_name, configured_tool in (getattr(self.config, "tools", None) or {}).items():
            self.cli.tmux_users.setdefault(
                str(cli_name),
                str(getattr(configured_tool, "tmux_user", "") or "").strip() or None,
            )
        if auto_commands_ran is not None:
            self.cli.auto_commands_ran = bool(auto_commands_ran)
        if cli_work_type is not None:
            self.cli.cli_work_type = str(cli_work_type or "").strip() or None
        if git_busy is not None:
            self.git.busy = bool(git_busy)
        if git_conflict is not None:
            self.git.conflict = bool(git_conflict)
        if isinstance(git_conflict_files, list):
            self.git.conflict_files = [str(x) for x in git_conflict_files if str(x or "").strip()]
        elif not isinstance(self.git.conflict_files, list):
            self.git.conflict_files = []
        if git_conflict_kind is not None:
            self.git.conflict_kind = str(git_conflict_kind or "").strip() or None
        if analyst_template_id is not None:
            self.modes.analyst_template_id = str(analyst_template_id or "default").strip() or "default"
        if manager_quiet_mode is not None:
            self.modes.manager_quiet_mode = bool(manager_quiet_mode)
        if isinstance(agent_memory, dict):
            self.modes.agent_memory = dict(agent_memory)
        elif not isinstance(self.modes.agent_memory, dict):
            self.modes.agent_memory = {}

        # Default active CLI: whatever tool we were created with.
        if not self.cli.active_cli:
            self.cli.active_cli = str(getattr(self.tool, "name", "") or "").strip() or None
        # Ensure tool matches active_cli when possible.
        try:
            if self.cli.active_cli and self.config and self.cli.active_cli in (self.config.tools or {}):
                if getattr(self.tool, "name", None) != self.cli.active_cli:
                    self.tool = self.config.tools[self.cli.active_cli]
        except Exception:
            logging.getLogger(__name__).exception("session init failed while syncing active_cli tool")

        # Normalize resume_tokens and ensure active CLI key exists.
        if not isinstance(self.cli.resume_tokens, dict):
            self.cli.resume_tokens = {}
        active = str(self.cli.active_cli or getattr(self.tool, "name", "") or "").strip()
        if active:
            self.cli.resume_tokens.setdefault(active, self.cli.resume_tokens.get(active))
        if not isinstance(self.conversation_scope, (ConversationScope, DesktopScope)):
            if self.chat_id is not None:
                self.conversation_scope = ConversationScope.from_parts(self.chat_id)
            else:
                self.conversation_scope = None
        explicit_scoped_key = _sanitize_scoped_key_token(self.scoped_key)
        if explicit_scoped_key and is_session_scoped_key(explicit_scoped_key):
            self.scoped_key = explicit_scoped_key
        else:
            scoped_chat_id = self.chat_id
            if scoped_chat_id is None:
                if isinstance(self.conversation_scope, DesktopScope):
                    scoped_chat_id = 0
                else:
                    scoped_chat_id = getattr(self.conversation_scope, "chat_id", None)
            self.scoped_key = build_session_scoped_key(scoped_chat_id, self.id)

    _LEGACY_STATE_FIELDS = {
        "active_cli": ("cli", "active_cli"),
        "resume_tokens": ("cli", "resume_tokens"),
        "auto_commands_ran": ("cli", "auto_commands_ran"),
        "cli_work_type": ("cli", "cli_work_type"),
        "git_busy": ("git", "busy"),
        "git_conflict": ("git", "conflict"),
        "git_conflict_files": ("git", "conflict_files"),
        "git_conflict_kind": ("git", "conflict_kind"),
        "analyst_template_id": ("modes", "analyst_template_id"),
        "manager_quiet_mode": ("modes", "manager_quiet_mode"),
        "agent_memory": ("modes", "agent_memory"),
    }

    def __getattribute__(self, name: str) -> Any:
        legacy_map = object.__getattribute__(self, "_LEGACY_STATE_FIELDS")
        mapping = legacy_map.get(str(name))
        if mapping:
            state_name, state_field = mapping
            state = object.__getattribute__(self, "__dict__").get(state_name)
            if state is not None and hasattr(state, state_field):
                return getattr(state, state_field)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        legacy_map = Session.__dict__.get("_LEGACY_STATE_FIELDS", {})
        mapping = legacy_map.get(str(name))
        if mapping:
            state_name, state_field = mapping
            state = self.__dict__.get(state_name)
            if state is not None and hasattr(state, state_field):
                if name == "resume_tokens":
                    value = dict(value) if isinstance(value, dict) else {}
                elif name == "git_conflict_files":
                    value = [str(x) for x in (value or []) if str(x or "").strip()] if isinstance(value, list) else []
                elif name == "agent_memory":
                    value = dict(value) if isinstance(value, dict) else {}
                elif name in ("auto_commands_ran", "git_busy", "git_conflict", "manager_quiet_mode"):
                    value = bool(value)
                elif name in ("active_cli", "cli_work_type", "git_conflict_kind"):
                    value = str(value or "").strip() or None
                elif name == "analyst_template_id":
                    value = str(value or "default").strip() or "default"
                setattr(state, state_field, value)
                return
        object.__setattr__(self, name, value)

    @property
    def scope(self) -> Any:
        return self.conversation_scope

    @scope.setter
    def scope(self, value: Any) -> None:
        self.conversation_scope = value

    @property
    def resume_token(self) -> Optional[str]:
        active = str(self.cli.active_cli or getattr(self.tool, "name", "") or "").strip()
        if not active:
            return None
        return (self.cli.resume_tokens or {}).get(active)

    @resume_token.setter
    def resume_token(self, value: Optional[str]) -> None:
        active = str(self.cli.active_cli or getattr(self.tool, "name", "") or "").strip()
        if not active:
            return
        if not isinstance(self.cli.resume_tokens, dict):
            self.cli.resume_tokens = {}
        self.cli.resume_tokens[active] = value

    async def _close_tmux_for_cli_async(self, cli_name: str, *, tool_override: Any = None) -> bool:
        name = str(cli_name or "").strip()
        if not name:
            return False
        configured_tools = getattr(self.config, "tools", None) or {}
        stored_tmux_users = getattr(self.cli, "tmux_users", None) or {}
        tool = tool_override
        if tool is None:
            if name in stored_tmux_users:
                tool = SimpleNamespace(name=name, tmux_user=stored_tmux_users.get(name))
            else:
                tool = configured_tools.get(name)
        if tool is None and str(getattr(self.tool, "name", "") or "").strip() == name:
            tool = self.tool
        if tool is None:
            tool = SimpleNamespace(name=name, tmux_user=None)

        from app.services.cli_backends import TmuxExecutionBackend
        from app.services.cli_backends.tmux_driver import TmuxDriver

        view = SimpleNamespace(
            id=self.id,
            workdir=self.workdir,
            config=self.config,
            tool=tool,
            cli=SimpleNamespace(
                active_cli=name,
                resume_tokens=dict(getattr(self.cli, "resume_tokens", None) or {}),
                execution_backends=dict(getattr(self.cli, "execution_backends", None) or {}),
            ),
            conversation_scope=self.conversation_scope,
            scope=self.scope,
            _active_execution_backend=self._active_execution_backend,
        )
        paths = TmuxExecutionBackend().paths(view)
        state = TmuxExecutionBackend._read_state(paths)
        if name not in stored_tmux_users and "tmux_user" in state:
            tool = SimpleNamespace(name=name, tmux_user=state.get("tmux_user"))
            view.tool = tool
        tmux_user = str(getattr(tool, "tmux_user", "") or "").strip() or None
        backend = TmuxExecutionBackend(driver=TmuxDriver(user=tmux_user, timeout_sec=2.0))
        has_state = os.path.exists(paths["state_path"])
        if not TmuxDriver.tmux_available():
            if not has_state:
                return False
            raise RuntimeError(f"tmux is unavailable while closing session {self.id} cli {name}")
        try:
            return bool(await backend.close(view))
        except Exception as exc:
            raise RuntimeError(
                f"tmux close failed for session {self.id} cli {name}: {exc}"
            ) from exc

    def _close_tmux_for_cli(self, cli_name: str, *, tool_override: Any = None) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return bool(
                asyncio.run(
                    self._close_tmux_for_cli_async(
                        cli_name,
                        tool_override=tool_override,
                    )
                )
            )
        raise RuntimeError("synchronous tmux close is not allowed from a running event loop")

    async def close_active_tmux_async(self) -> bool:
        closed = await self._close_tmux_for_cli_async(session_active_cli_name(self))
        if self._active_execution_backend == EXECUTION_BACKEND_TMUX:
            self._active_execution_backend = "none"
        return closed

    def close_active_tmux(self) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return bool(asyncio.run(self.close_active_tmux_async()))

        results: list[bool] = []
        errors: list[BaseException] = []

        def _close_tmux() -> None:
            try:
                results.append(bool(asyncio.run(self.close_active_tmux_async())))
            except BaseException as exc:
                logging.getLogger(__name__).exception(
                    "active tmux close failed session_id=%s cli=%s",
                    self.id,
                    session_active_cli_name(self),
                )
                errors.append(exc)

        thread = threading.Thread(target=_close_tmux, daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        if thread.is_alive():
            raise RuntimeError(f"tmux close timed out for session {self.id}")
        if errors:
            raise errors[0]
        return bool(results and results[0])

    async def set_active_cli_persistent(self, cli_name: str) -> None:
        name = str(cli_name or "").strip()
        if not name:
            raise ValueError("cli_name is empty")
        if not self.config or name not in (self.config.tools or {}):
            raise ValueError(f"unknown cli: {name}")
        previous = session_active_cli_name(self)
        if previous and previous != name:
            await self._close_tmux_for_cli_async(previous)
        self.cli.active_cli = name
        self.tool = self.config.tools[name]
        self.cli.tmux_users[name] = str(getattr(self.tool, "tmux_user", "") or "").strip() or None
        self.resume_token = (self.cli.resume_tokens or {}).get(name)

    async def set_active_cli_persistent_when_idle(self, cli_name: str) -> None:
        if self.busy or self.run_lock.locked():
            raise RuntimeError(f"session {self.id} is busy")
        is_active_by_tick = getattr(self, "is_active_by_tick", None)
        if callable(is_active_by_tick) and is_active_by_tick():
            raise RuntimeError(f"session {self.id} is active")

        await self.run_lock.acquire()
        try:
            if self.busy:
                raise RuntimeError(f"session {self.id} is busy")
            if callable(is_active_by_tick) and is_active_by_tick():
                raise RuntimeError(f"session {self.id} is active")
            await self.set_active_cli_persistent(cli_name)
        finally:
            self.run_lock.release()

    def set_active_cli(self, cli_name: str, *, close_previous_tmux: bool = False) -> None:
        """
        Switch the active CLI for this session.

        Rules:
        - Only switches to a CLI present in config.tools.
        - Updates `tool` and active `resume_token` view.
        """
        name = str(cli_name or "").strip()
        if not name:
            raise ValueError("cli_name is empty")
        if not self.config or name not in (self.config.tools or {}):
            raise ValueError(f"unknown cli: {name}")
        previous = session_active_cli_name(self)
        if close_previous_tmux and previous and previous != name:
            self._close_tmux_for_cli(previous)
        self.cli.active_cli = name
        self.tool = self.config.tools[name]
        self.cli.tmux_users[name] = str(getattr(self.tool, "tmux_user", "") or "").strip() or None
        # Update active resume token view from per-cli mapping.
        self.resume_token = (self.cli.resume_tokens or {}).get(name)

    def reset_all_resume_tokens(self) -> None:
        """Clear resume tokens for all CLIs in this session (and the active token view)."""
        if isinstance(self.cli.resume_tokens, dict):
            for k in list(self.cli.resume_tokens.keys()):
                self.cli.resume_tokens[k] = None
        self.resume_token = None

    def _cli_language_directive(self) -> str:
        """Localized instruction telling the CLI agent which language to answer in.

        Resolved from the session owner (chat_id == telegram user_id in private chats).
        Returns "" for the fallback language (Russian baseline) so existing
        Russian-default flows send the prompt byte-for-byte unchanged.
        """
        try:
            from utils.lang import resolve_user_lang, FALLBACK_LANG
            from i18n import t

            lang = resolve_user_lang(self.config, chat_id=self.chat_id)
            if lang == FALLBACK_LANG:
                return ""
            return t("agent.cli_language_directive", lang)
        except Exception:
            return ""

    async def run_prompt(
        self,
        prompt: str,
        image_path: Optional[str] = None,
        image_paths: Optional[List[str]] = None,
        *,
        force_fresh: bool = False,
        tmux_request_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        directive = self._cli_language_directive()
        if directive:
            prompt = f"{directive}\n\n{prompt}"
        if image_paths:
            valid_paths = [str(p).strip() for p in image_paths if str(p).strip()]
            if not valid_paths:
                image_paths = None
            else:
                image_paths = valid_paths
        selected_backend = get_session_execution_backend(self)
        if selected_backend == EXECUTION_BACKEND_TMUX and (image_paths or image_path):
            raise RuntimeError("tmux backend does not support image requests in v1")
        if image_paths:
            is_gemini = (self.tool.name or "").strip().lower() == "gemini"
            is_qwen = (self.tool.name or "").strip().lower() == "qwen"
            is_claude = (self.tool.name or "").strip().lower() == "claude"
            if not self.tool.image_cmd and not is_gemini and not is_qwen and not is_claude:
                raise RuntimeError(f"{self.tool.name} не поддерживает изображения")
            image_arg: Optional[str] = None
            if is_qwen:
                cmd_template = ["qwen", "{prompt}", "--model", "vision-model"]
                if self.resume_token:
                    cmd_template.extend(["--resume", "{resume}"])
            else:
                if self.resume_token and self.tool.resume_cmd:
                    cmd_template = self.tool.resume_cmd
                else:
                    cmd_template = self.tool.headless_cmd or self.tool.cmd
                if is_gemini and self.resume_token and not self.tool.resume_cmd:
                    cmd_template = self._build_gemini_resume_template(cmd_template)
            prompt_to_send = prompt
            if is_gemini:
                refs = "\n".join(f"@{p}" for p in image_paths)
                prompt_to_send = f"{refs}\n{prompt}".strip()
            elif is_qwen:
                refs: List[str] = []
                for p in image_paths:
                    try:
                        refs.append(os.path.relpath(p, self.workdir))
                    except Exception:
                        refs.append(p)
                refs_str = " ".join(f"@{p}" for p in refs)
                if prompt.strip():
                    prompt_to_send = f"{prompt.strip()} {refs_str}".strip()
                else:
                    prompt_to_send = f"Расскажи о {refs_str}".strip()
            else:
                image_arg = ",".join(image_paths)
            if self.tool.image_cmd:
                cmd_template = cmd_template + self.tool.image_cmd
            return await self._run_headless(prompt_to_send, cmd_template=cmd_template, image_path=image_arg)
        if image_path:
            is_gemini = (self.tool.name or "").strip().lower() == "gemini"
            is_qwen = (self.tool.name or "").strip().lower() == "qwen"
            is_claude = (self.tool.name or "").strip().lower() == "claude"
            if not self.tool.image_cmd and not is_gemini and not is_qwen and not is_claude:
                raise RuntimeError(f"{self.tool.name} не поддерживает изображения")
            if is_qwen:
                cmd_template = ["qwen", "{prompt}", "--model", "vision-model"]
                if self.resume_token:
                    cmd_template.extend(["--resume", "{resume}"])
            else:
                if self.resume_token and self.tool.resume_cmd:
                    cmd_template = self.tool.resume_cmd
                else:
                    cmd_template = self.tool.headless_cmd or self.tool.cmd
                if is_gemini and self.resume_token and not self.tool.resume_cmd:
                    cmd_template = self._build_gemini_resume_template(cmd_template)
            prompt_to_send = prompt
            if is_gemini:
                attachment_ref = f"@{image_path}"
                prompt_to_send = f"{attachment_ref}\n{prompt}".strip()
            elif is_qwen:
                try:
                    attachment_path = os.path.relpath(image_path, self.workdir)
                except Exception:
                    attachment_path = image_path
                # Qwen vision expects file mention inside the prompt, for example: "@diagram.png".
                if prompt.strip():
                    prompt_to_send = f"{prompt.strip()} @{attachment_path}"
                else:
                    prompt_to_send = f"Расскажи о @{attachment_path}"
            if self.tool.image_cmd:
                cmd_template = cmd_template + self.tool.image_cmd
            # Images are never executed in "fresh" mode: image flows are typically interactive and
            # provider-specific; keep behavior unchanged unless explicitly required later.
            return await self._run_headless(prompt_to_send, cmd_template=cmd_template, image_path=image_path)
        if selected_backend == EXECUTION_BACKEND_TMUX:
            return await self._run_tmux(
                prompt,
                force_fresh=force_fresh,
                request_context=tmux_request_context,
            )
        if self.tool.mode == "headless":
            try:
                return await self._run_headless(prompt, force_fresh=force_fresh)
            except Exception as exc:
                msg = str(exc or "")
                if "Event loop is closed" in msg or "no running event loop" in msg:
                    raise
                # switch to interactive mode when headless run fails
                return await self._run_interactive(prompt, force_fresh=force_fresh)
        return await self._run_interactive(prompt, force_fresh=force_fresh)

    async def _run_tmux(
        self,
        prompt: str,
        *,
        force_fresh: bool = False,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        from app.services.cli_backends import TmuxExecutionBackend

        self._active_execution_backend = EXECUTION_BACKEND_TMUX
        try:
            backend = TmuxExecutionBackend()
            result = await backend.run(
                self,
                prompt,
                force_fresh=force_fresh,
                request_context=request_context,
            )
            self.last_tmux_request_id = result.request_id
            paths = backend.paths(self)
            self.last_cli_raw_stream_path = paths.get("pane_log")
            self.last_cli_normalized_stream_path = str(result.diagnostics.get("transcript_path") or "") or None
            if result.abnormal_stop:
                self.headless_forced_stop = str(result.diagnostics.get("failure_reason") or "tmux_request_failed")
            return result.text
        finally:
            if self._active_execution_backend == EXECUTION_BACKEND_TMUX:
                self._active_execution_backend = "none"

    async def recover_tmux_request(self, request: Any) -> str:
        from app.services.cli_backends import TmuxExecutionBackend

        self._active_execution_backend = EXECUTION_BACKEND_TMUX
        try:
            backend = TmuxExecutionBackend()
            result = await backend.recover(self, request)
            self.last_tmux_request_id = result.request_id
            paths = backend.paths(self)
            self.last_cli_raw_stream_path = paths.get("pane_log")
            self.last_cli_normalized_stream_path = str(result.diagnostics.get("transcript_path") or "") or None
            if result.abnormal_stop:
                self.headless_forced_stop = str(result.diagnostics.get("failure_reason") or "tmux_request_failed")
            return result.text
        finally:
            if self._active_execution_backend == EXECUTION_BACKEND_TMUX:
                self._active_execution_backend = "none"

    async def _run_headless(
        self,
        prompt: str,
        cmd_template: Optional[List[str]] = None,
        image_path: Optional[str] = None,
        *,
        force_fresh: bool = False,
    ) -> str:
        _log = logging.getLogger("session.headless")
        is_codex = (self.tool.name or "").strip().lower() == "codex"
        is_gemini = (self.tool.name or "").strip().lower() == "gemini"
        is_qwen = (self.tool.name or "").strip().lower() == "qwen"
        is_claude = (self.tool.name or "").strip().lower() == "claude"
        is_grok = (self.tool.name or "").strip().lower() == "grok"
        self.headless_forced_stop = None
        resume = None if force_fresh else self.resume_token
        claude_session_id: Optional[str] = None
        if is_claude:
            if resume:
                claude_session_id = str(resume).strip() or None
            else:
                claude_session_id = str(uuid.uuid4())

        if cmd_template is None:
            cmd_template = self.tool.headless_cmd or self.tool.cmd
            if not force_fresh:
                if self.resume_token and self.tool.resume_cmd:
                    cmd_template = self.tool.resume_cmd
                elif self.resume_token and is_gemini:
                    cmd_template = self._build_gemini_resume_template(cmd_template)
            resume = None if force_fresh else self.resume_token
        ssh_skill = generate_ssh_skill_text(self.workdir, session=self)
        if ssh_skill:
            prompt = f"{ssh_skill}\n\n---\n\n{prompt}"
        cmd, use_stdin = build_command(cmd_template, prompt, resume, image=image_path)
        if is_qwen and not resume:
            # For Qwen, a fresh run must not inherit the implicit "latest session"
            # behavior from --continue. Resume should happen only with an explicit token.
            cmd = [part for part in cmd if part != "--continue"]
        if is_codex:
            cmd = self._ensure_codex_json_command(cmd)
        if is_gemini:
            cmd = self._ensure_gemini_stream_json_command(cmd)
        if is_qwen:
            cmd = self._ensure_qwen_stream_json_command(cmd)
        if is_claude:
            cmd = self._ensure_claude_stream_json_command(cmd)
            if not use_stdin and str(prompt or "").startswith("-"):
                for idx, part in enumerate(cmd):
                    if part == prompt:
                        cmd = cmd[:idx] + cmd[idx + 1:]
                        use_stdin = True
                        break
        if is_grok:
            cmd = self._ensure_grok_streaming_json_command(cmd)
        stream_adapter = build_cli_json_stream_adapter(self.tool.name)
        expected_cli_response_format = _extract_cli_response_format(prompt)
        stream_recorder = CliJsonStreamRecorder(
            enabled=bool(stream_adapter) and cli_json_stream_archive_enabled(self.config),
            workdir=self.workdir,
            cli_name=self.tool.name,
            session_uid=session_runtime_uid(self) or self.id,
        )
        self.last_cli_raw_stream_path = None
        self.last_cli_normalized_stream_path = None

        # Для claude используем запуск от имени claude-bot
        run_as_user = None
        if is_claude:
            if not resume and claude_session_id:
                # For a fresh Claude session, use an explicit session UUID
                # without inheriting the default "--continue" template.
                cmd = [part for part in cmd if part != "--continue"]
                cmd.extend(["--session-id", claude_session_id])
                # Opt-in: `--no-session-persistence` уменьшает шум в
                # ~/.claude/projects, но ломает session_transfer/reader_claude
                # (transcript на диске отсутствует). Поэтому только когда
                # пользователь явно включил флаг в tools.claude.
                # NB: для resume флаг не добавляем — `claude --help` явно
                # говорит «cannot be resumed».
                if self.tool.no_session_persistence_on_fresh:
                    if "--no-session-persistence" not in cmd:
                        cmd.append("--no-session-persistence")
            run_as_user = "claude-bot"
            # Передаём команду через su -c с явным указанием рабочей директории.
            # Родитель (cli-proxy под Claude Code) экспортирует CLAUDECODE=1 и
            # семейство CLAUDE_CODE_* маркеров вложенности (ENTRYPOINT/EXECPATH/
            # SESSION_ID и т.п.). Вложенный `claude` детектит их и падает с
            # конфликтом «уже внутри Claude Code». `su -` обычно сбрасывает env,
            # но в зависимости от PAM-конфигурации может пробросить — снимаем явно.
            cd_cmd = f"cd {shlex.quote(self.workdir)} && "
            unset_nested = "env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_EXECPATH -u CLAUDE_CODE_SESSION_ID "
            full_cmd = cd_cmd + unset_nested + " ".join(shlex.quote(str(x)) for x in cmd)
            cmd = ["su", "-", run_as_user, "-c", full_cmd]

        _log.info("START cmd=%s use_stdin=%s cwd=%s run_as=%s", cmd, use_stdin, self.workdir, run_as_user)
        env = os.environ.copy()
        if is_claude:
            # Defense in depth: для будущих веток, где is_claude может пойти не через
            # `su -` (login shell сам сбрасывает env), убедимся что nested-маркеры
            # не уйдут вложенному процессу. Сохраняем переменные, влияющие на
            # выбор бэкенда/авторизации (Bedrock/Vertex/OAuth) — иначе при отказе
            # от `su -` обёртки сломается аутентификация вложенного `claude`.
            _claude_auth_keep = {
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_OAUTH_TOKEN",
            }
            for nested_var in list(env):
                if nested_var in _claude_auth_keep:
                    continue
                if nested_var == "CLAUDECODE" or nested_var.startswith("CLAUDE_CODE_"):
                    env.pop(nested_var, None)
        if ssh_skill:
            from app.services.ssh_config_loader import load_ssh_secrets as _load_ssh_secrets
            env.update(_load_ssh_secrets(self.workdir))
        if self.tool.env:
            for k, v in self.tool.env.items():
                if v is None:
                    continue
                env[k] = resolve_env_value(str(v))
        # Capture stderr separately when configured. Gemini is forced into this
        # mode so service lines from stderr never leak into user-visible output.
        use_separate_stderr = getattr(self.tool, "separate_stderr", False) or is_gemini or bool(stream_adapter)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.workdir,
            env=env,
            stdin=asyncio.subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if use_separate_stderr else asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.current_proc = proc
        self._headless_interrupt_flag = False
        self._active_execution_backend = "headless"
        _log.info("PID=%s started (separate_stderr=%s)", proc.pid, use_separate_stderr)
        if use_stdin and proc.stdin:
            proc.stdin.write((prompt + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            _log.info("stdin written and closed")
        out_buf = bytearray()
        err_buf = bytearray()
        drain_eof = False
        semantic_completion = asyncio.Event() if stream_adapter else None
        semantic_output_text: Optional[str] = None
        semantic_structured_candidate: Optional[str] = None
        streamed_assistant_tick_seen = False

        def _record_stream_event(event: Any) -> None:
            nonlocal semantic_output_text, streamed_assistant_tick_seen
            nonlocal semantic_structured_candidate
            try:
                stream_recorder.record_event(event)
            except Exception:
                _log.exception("CLI JSON stream event archive failed")
            if event.kind == "session_started":
                session_id = str(event.session_id or "").strip() or None
                if session_id and not force_fresh:
                    # Фиксируем resume_token сразу, как только CLI сообщил о старте
                    # сессии (для Claude это событие system/init). С этого момента
                    # Claude уже пишет транскрипт на диск, поэтому сессия остаётся
                    # resumable даже если прогон затем прервут через /interrupt или
                    # он завершится ошибкой. Ранее fresh-сессия Claude откладывала
                    # запись до финального события completed, из-за чего при любом
                    # раннем выходе (уже resumable) session id терялся.
                    self.resume_token = session_id
                return
            if event.kind == "assistant_text":
                if event.text is not None:
                    if is_time_only_text(event.text):
                        return
                    semantic_output_text = event.text
                    assistant_tick_text = event.text
                    payload = event.payload if isinstance(event.payload, dict) else None
                    if payload and payload.get("delta") is True and stream_adapter is not None:
                        accumulated = stream_adapter.final_output_text()
                        if accumulated and not is_time_only_text(accumulated):
                            assistant_tick_text = accumulated
                    before_value = self.last_tick_value
                    before_seen = int(self.tick_seen or 0)
                    self._update_activity(
                        assistant_tick_text,
                        tick_kind="assistant_text",
                        replace_last=bool(payload and payload.get("delta") is True),
                    )
                    if self.last_tick_value != before_value or int(self.tick_seen or 0) != before_seen:
                        streamed_assistant_tick_seen = True
                    if (
                        is_codex
                        and semantic_completion is not None
                        and expected_cli_response_format
                    ):
                        candidates = [semantic_output_text]
                        combined_candidate = (
                            f"{semantic_structured_candidate}{semantic_output_text}"
                            if semantic_structured_candidate
                            else semantic_output_text
                        )
                        if combined_candidate and combined_candidate != semantic_output_text:
                            candidates.append(combined_candidate)
                        semantic_structured_candidate = combined_candidate or semantic_structured_candidate
                        for candidate in candidates:
                            if not _is_complete_structured_cli_output(
                                candidate,
                                response_format=expected_cli_response_format,
                            ):
                                continue
                            semantic_output_text = candidate
                            _log.info(
                                "semantic completion inferred from codex assistant_text format=%s",
                                expected_cli_response_format,
                            )
                            semantic_completion.set()
                            break
                return
            if event.kind in {"progress", "tool_event", "failed", "thinking"}:
                if event.text:
                    self._update_activity(event.text, tick_kind=event.kind)
            if event.kind in {"completed", "failed"} and semantic_completion is not None:
                final_output = stream_adapter.final_output_text() if stream_adapter else ""
                if final_output and not is_time_only_text(final_output):
                    semantic_output_text = final_output
                semantic_completion.set()

        def _consume_stream_line(line: str) -> None:
            if not stream_adapter:
                return
            payload_line = str(line or "").rstrip("\n")
            if not payload_line.strip():
                return
            try:
                stream_recorder.record_raw_line(payload_line)
            except Exception:
                _log.exception("CLI JSON stream raw archive failed")
            try:
                events = stream_adapter.feed_line(payload_line)
            except Exception:
                _log.exception("CLI JSON stream line parse failed cli=%s line=%r", self.tool.name, payload_line[:500])
                return
            for event in events:
                _record_stream_event(event)

        async def _drain_stdout() -> None:
            nonlocal drain_eof
            if not proc.stdout:
                _log.warning("no stdout pipe")
                return
            text_buf = ""
            try:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        if stream_adapter and text_buf.strip():
                            _consume_stream_line(text_buf)
                        drain_eof = True
                        _log.info("drain: EOF received, total %d bytes", len(out_buf))
                        break
                    out_buf.extend(chunk)
                    decoded = chunk.decode(errors="ignore")
                    if stream_adapter:
                        text_buf += decoded
                        while True:
                            line, sep, remainder = text_buf.partition("\n")
                            if not sep:
                                text_buf = line
                                break
                            _consume_stream_line(line)
                            text_buf = remainder
                    try:
                        if not stream_adapter:
                            self._update_activity(decoded)
                    except Exception:
                        _log.exception("stdout drain activity update failed")
            except asyncio.CancelledError:
                _log.warning("drain: cancelled, had %d bytes", len(out_buf))
                raise
            except Exception:
                _log.exception("drain: exception")

        async def _drain_stderr() -> None:
            """Drain stderr separately when separate_stderr is enabled."""
            if not proc.stderr:
                return
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    err_buf.extend(chunk)
                    # When stdout carries normalized JSON-stream events, stderr
                    # is service/debug output only and must not affect ticks.
                    if stream_adapter:
                        continue
                    # Update activity from stderr too so progress is visible
                    try:
                        self._update_activity(chunk.decode(errors="ignore"))
                    except Exception:
                        _log.exception("stderr drain activity update failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("stderr drain: exception")

        def _pid_exists(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except OSError as e:
                return e.errno != errno.ESRCH

        wait_task = asyncio.create_task(proc.wait())
        drain_task = asyncio.create_task(_drain_stdout())
        stderr_drain_task = asyncio.create_task(_drain_stderr()) if use_separate_stderr else None
        semantic_done_task = (
            asyncio.create_task(semantic_completion.wait())
            if semantic_completion is not None
            else None
        )

        async def _cancel_bg_task(task: Optional[asyncio.Task]) -> None:
            if task is None or task.done():
                return
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                return
            except Exception:
                _log.exception("background task cancellation failed")

        async def _wait_headless_exit_gracefully(timeout_s: float) -> bool:
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=max(0.0, float(timeout_s)))
                return True
            except asyncio.TimeoutError:
                return False
            except Exception:
                _log.exception("headless EOF-timeout wait failed pid=%s", getattr(proc, "pid", None))
                return bool(wait_task.done() or getattr(proc, "returncode", None) is not None)

        async def _stop_headless_after_semantic_completion() -> None:
            if getattr(proc, "returncode", None) is not None:
                return
            if getattr(proc, "pid", None) is None:
                return
            _log.info("semantic completion reached; stopping lingering process pid=%s", proc.pid)
            if not self._signal_headless_process_tree(proc.pid, signal.SIGTERM):
                _log.warning("semantic completion SIGTERM found no process tree pid=%s", proc.pid)
            if await _wait_headless_exit_gracefully(0.3):
                return
            if not self._signal_headless_process_tree(proc.pid, signal.SIGKILL):
                _log.warning("semantic completion SIGKILL found no process tree pid=%s", proc.pid)
            await _wait_headless_exit_gracefully(0.3)

        async def _stop_headless_after_eof_timeout() -> None:
            if getattr(proc, "returncode", None) is not None:
                return
            if getattr(proc, "pid", None) is None:
                return
            if not self._signal_headless_process_tree(proc.pid, signal.SIGTERM):
                _log.warning("headless EOF-timeout SIGTERM found no process tree pid=%s", proc.pid)
            if await _wait_headless_exit_gracefully(_HEADLESS_EOF_STOP_GRACE_SEC):
                return
            if not self._signal_headless_process_tree(proc.pid, signal.SIGKILL):
                _log.warning("headless EOF-timeout SIGKILL found no process tree pid=%s", proc.pid)
            await _wait_headless_exit_gracefully(_HEADLESS_EOF_STOP_GRACE_SEC)

        forced_reason: Optional[str] = None
        stderr_text = ""
        try:
            poll_iter = 0
            drain_eof_deadline: Optional[float] = None
            while True:
                poll_iter += 1
                wait_targets = {wait_task}
                if semantic_done_task is not None:
                    wait_targets.add(semantic_done_task)
                done, _ = await asyncio.wait(
                    wait_targets,
                    timeout=_HEADLESS_WAIT_POLL_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if semantic_done_task is not None and semantic_done_task in done:
                    _log.info("semantic completion observed after %d polls", poll_iter)
                    await _stop_headless_after_semantic_completion()
                    break
                if wait_task in done:
                    _log.info("wait_task done after %d polls, returncode=%s", poll_iter, proc.returncode)
                    break
                effective_returncode = self._poll_headless_native_returncode(proc)
                pid_alive = _pid_exists(proc.pid) if proc.pid else False
                _log.info(
                    "poll #%d: wait_task.done=%s pid_exists=%s returncode=%s drain_eof=%s buf=%d",
                    poll_iter, wait_task.done(), pid_alive, effective_returncode, drain_eof, len(out_buf),
                )
                if effective_returncode is not None:
                    forced_reason = f"returncode={effective_returncode} есть, но wait() не завершился (stdout pipe удерживается)"
                    _log.warning("%s", forced_reason)
                    break
                # Если asyncio не получил событие завершения, но PID уже отсутствует,
                # считаем процесс завершенным, чтобы не держать сессию busy бесконечно.
                if proc.pid and not pid_alive:
                    forced_reason = "PID отсутствует, но ожидание завершения не сработало"
                    _log.warning("%s (returncode=%s)", forced_reason, proc.returncode)
                    break
                if drain_eof:
                    if drain_eof_deadline is None:
                        drain_eof_deadline = time.monotonic() + _HEADLESS_EOF_EXIT_TIMEOUT_SEC
                        _log.warning(
                            "stdout EOF получен до завершения процесса; ждём завершение не дольше %.1fs",
                            _HEADLESS_EOF_EXIT_TIMEOUT_SEC,
                        )
                    elif time.monotonic() >= drain_eof_deadline:
                        forced_reason = (
                            f"процесс не завершился в течение {_HEADLESS_EOF_EXIT_TIMEOUT_SEC:.1f}s после EOF stdout"
                        )
                        _log.warning("%s pid=%s", forced_reason, getattr(proc, "pid", None))
                        await _stop_headless_after_eof_timeout()
                        break

            # Даем короткий grace на дочитывание хвоста вывода. Если stdout не закрывается — прекращаем чтение.
            grace_sec = 2.0
            if not drain_task.done():
                _log.info("waiting for drain (grace %.1fs)...", grace_sec)
                try:
                    await asyncio.wait_for(asyncio.shield(drain_task), timeout=grace_sec)
                    _log.info("drain finished within grace")
                except asyncio.TimeoutError:
                    forced_reason = forced_reason or "stdout не закрылся после завершения процесса"
                    _log.warning("drain timed out: %s", forced_reason)
                    # Закрываем наш read-end пайпа, чтобы не держать ресурсы.
                    try:
                        transport = getattr(proc.stdout, "_transport", None) if proc.stdout else None
                        if transport is not None:
                            transport.close()
                    except Exception:
                        _log.exception("stdout transport close failed after drain timeout")
                except Exception:
                    forced_reason = forced_reason or "ошибка при ожидании stdout после завершения процесса"
                    _log.exception("drain grace exception")
            else:
                _log.info("drain already finished")

            if not wait_task.done():
                _log.warning("wait_task still not done, cancelling")
                wait_task.cancel()

            # Clean up stderr drain task
            if stderr_drain_task is not None:
                if not stderr_drain_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(stderr_drain_task), timeout=2.0)
                    except asyncio.TimeoutError:
                        _log.warning("stderr drain timed out during cleanup")
                    except Exception:
                        _log.exception("stderr drain cleanup failed")
                stderr_text = bytes(err_buf).decode(errors="ignore").strip()

            raw_text = bytes(out_buf).decode(errors="ignore")
            if semantic_output_text is not None:
                text = semantic_output_text
            else:
                recovered_stream_text = ""
                if stream_adapter:
                    recovered_stream_text = recover_cli_text_from_raw_stream(self.tool.name, raw_text)
                text = recovered_stream_text or raw_text
            if stderr_text:
                stderr_log_text, suppressed_stderr_lines = _prepare_headless_stderr_for_logging(
                    self.tool.name,
                    stderr_text,
                    returncode=proc.returncode,
                    has_user_output=bool(text.strip()),
                )
                if stderr_log_text:
                    if suppressed_stderr_lines:
                        _log.info(
                            "stderr (%d chars, %d transient lines suppressed): %s",
                            len(stderr_log_text), suppressed_stderr_lines, stderr_log_text[:500],
                        )
                    else:
                        _log.info("stderr (%d chars): %s", len(stderr_log_text), stderr_log_text[:500])
                elif suppressed_stderr_lines:
                    _log.debug(
                        "suppressed transient stderr from %s (%d lines)",
                        self.tool.name, suppressed_stderr_lines,
                    )
            _log.info("END PID=%s forced=%s output_len=%d", proc.pid, forced_reason, len(text))
            if forced_reason:
                self.headless_forced_stop = forced_reason
                if not text:
                    from i18n import t
                    from utils.lang import resolve_user_lang
                    _lang = resolve_user_lang(self.config, chat_id=self.chat_id)
                    text = t("msg.session.headless_read_failed", _lang, reason=forced_reason)
            if not (semantic_output_text is not None and streamed_assistant_tick_seen and not forced_reason):
                self._update_activity(
                    text,
                    allow_short=semantic_output_text is not None,
                    tick_kind="assistant_text" if semantic_output_text is not None else None,
                )
            if not force_fresh:
                if is_gemini:
                    if not self.resume_token:
                        await self._recover_gemini_resume_token_from_list_sessions()
                elif is_qwen:
                    if not self.resume_token:
                        self._recover_qwen_resume_token_from_chat_files()
                elif is_claude:
                    if not self.resume_token:
                        if not resume and claude_session_id and proc.returncode == 0:
                            self.resume_token = claude_session_id
                        elif text:
                            # Claude не печатает session id в обычном print-режиме.
                            self._maybe_autoset_resume_regex(text)
                            self._maybe_update_resume(text)
                elif not self.resume_token:
                    resume_source_base = raw_text if stream_adapter is not None else text
                    resume_source = resume_source_base if not stderr_text else f"{resume_source_base}\n{stderr_text}"
                    self._maybe_autoset_resume_regex(resume_source)
                    self._maybe_update_resume(resume_source)
            self.last_cli_raw_stream_path = str(stream_recorder.raw_path) if stream_recorder.raw_path else None
            self.last_cli_normalized_stream_path = (
                str(stream_recorder.normalized_path) if stream_recorder.normalized_path else None
            )
            return text
        except asyncio.CancelledError:
            _log.warning("cancelled: stopping process pid=%s", getattr(proc, "pid", None))
            self._headless_interrupt_flag = True
            if proc.returncode is None and getattr(proc, "pid", None):
                if not self._signal_headless_process_tree(proc.pid, signal.SIGTERM):
                    _log.warning("cancel cleanup SIGTERM found no process tree pid=%s", proc.pid)
                if not self._signal_headless_process_tree(proc.pid, signal.SIGKILL):
                    _log.warning("cancel cleanup SIGKILL found no process tree pid=%s", proc.pid)
            raise
        finally:
            await _cancel_bg_task(drain_task)
            await _cancel_bg_task(wait_task)
            await _cancel_bg_task(stderr_drain_task)
            await _cancel_bg_task(semantic_done_task)
            normalized_stream_path = stream_recorder.normalized_path
            stream_recorder.close()
            if normalized_stream_path:
                self.last_cli_normalized_stream_path = str(normalized_stream_path)
            self.current_proc = None
            self._headless_interrupt_flag = False
            if self._active_execution_backend == "headless":
                self._active_execution_backend = "none"

    async def _run_interactive(self, prompt: str, *, force_fresh: bool = False) -> str:
        # Interactive mode has no standardized "fresh chat" contract across tools.
        # For safety, force_fresh disables any resume-token updates.
        self._active_execution_backend = "interactive"
        try:
            if force_fresh:
                prev = self.resume_token
                try:
                    out = await asyncio.to_thread(self._run_interactive_sync, prompt)
                    self.resume_token = prev
                    return out
                finally:
                    self.resume_token = prev
            return await asyncio.to_thread(self._run_interactive_sync, prompt)
        finally:
            if self._active_execution_backend == "interactive":
                self._active_execution_backend = "none"

    def _ensure_child(self) -> None:
        if self.child and self.child.isalive():
            return
        cmd_template = self.tool.interactive_cmd or self.tool.cmd
        is_claude = (self.tool.name or "").strip().lower() == "claude"

        # Для claude используем обёртку через su -c
        if is_claude:
            # Запуск от имени claude-bot с явным указанием рабочей директории
            full_cmd = f"cd {shlex.quote(self.workdir)} && " + " ".join(shlex.quote(str(x)) for x in cmd_template)
            cmd_template = ["su", "-", "claude-bot", "-c", full_cmd]

        env = os.environ.copy()
        if self.tool.env:
            for k, v in self.tool.env.items():
                if v is None:
                    continue
                env[k] = resolve_env_value(str(v))
        self.child = pexpect.spawn(
            cmd_template[0],
            cmd_template[1:],
            cwd=self.workdir,
            encoding="utf-8",
            echo=False,
            timeout=self.idle_timeout_sec,
            env=env,
        )
        if self.tool.auto_commands and not self.auto_commands_ran:
            self.auto_commands_ran = True
            for cmd in self.tool.auto_commands:
                try:
                    self.child.sendline(cmd)
                    if self.tool.prompt_regex:
                        self.child.expect(self.tool.prompt_regex, timeout=5)
                except Exception:
                    continue

    def _run_interactive_sync(self, prompt: str) -> str:
        self._ensure_child()
        assert self.child is not None
        self.child.sendline(prompt)

        if self.tool.prompt_regex:
            self.child.expect(self.tool.prompt_regex)
            output = self.child.before
            self._update_activity(output)
            return output

        # No prompt regex: wait for timeout then attempt autodetect
        output_parts = []
        last_output_ts = time.time()
        while True:
            try:
                self.child.expect(pexpect.TIMEOUT, timeout=1)
            except Exception:
                logger.exception("interactive read loop expect failed")
            chunk = self.child.before
            if chunk:
                output_parts.append(chunk)
                self._update_activity(chunk)
                last_output_ts = time.time()
            now = time.time()
            last_tick_ts = self.last_tick_ts or 0.0
            idle_for = now - last_output_ts
            tick_idle_for = now - last_tick_ts if last_tick_ts else idle_for
            if idle_for >= self.idle_timeout_sec and tick_idle_for >= self.idle_timeout_sec:
                break
            if self.child and not self.child.isalive():
                break
        output = "".join(output_parts)
        if not self.resume_token and (self.tool.name or "").strip().lower() == "qwen":
            self._recover_qwen_resume_token_from_chat_files()
        elif not self.resume_token and (self.tool.name or "").strip().lower() == "claude":
            # Claude использует resume токен из вывода
            self._maybe_autoset_resume_regex(output)
            self._maybe_update_resume(output)
        elif not self.resume_token:
            self._maybe_update_resume(output)
            self._maybe_autoset_resume_regex(output)
        lines = output.splitlines()
        regex = detect_prompt_regex(lines)
        if regex:
            with _CONFIG_SAVE_LOCK:
                self.tool.prompt_regex = regex
                save_config(self.config)
        return output

    def interrupt(self) -> None:
        active_backend = str(getattr(self, "_active_execution_backend", "") or "").strip().lower()
        if active_backend == EXECUTION_BACKEND_TMUX:
            if self._preserve_tmux_on_shutdown:
                logger.info("tmux interrupt skipped during shutdown session_id=%s", self.id)
                return
            try:
                from app.services.cli_backends import TmuxExecutionBackend

                coro = TmuxExecutionBackend().interrupt(self)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(coro)
                else:
                    loop.create_task(coro)
            except Exception:
                logger.exception("tmux interrupt failed session_id=%s", self.id)
            return
        if active_backend == "interactive":
            if self.child and self.child.isalive():
                try:
                    self.child.sendcontrol("c")
                except Exception:
                    logger.exception("interactive interrupt failed")
            return
        if active_backend == "headless" or self.tool.mode == "headless":
            if self.current_proc and self.current_proc.returncode is None:
                self._headless_interrupt_flag = True
                proc = self.current_proc
                pid = getattr(proc, "pid", None)
                try:
                    self._stop_headless_process_tree(
                        proc,
                        wait_timeout_s=0.5,
                        action_label="interrupt",
                    )
                except Exception:
                    logger.exception("headless interrupt failed session_id=%s pid=%s", self.id, pid)
                    try:
                        proc.kill()
                    except Exception:
                        logger.exception("headless interrupt fallback kill failed")
            return
        if self.child and self.child.isalive():
            try:
                self.child.sendcontrol("c")
            except Exception:
                logger.exception("interactive interrupt failed")

    @staticmethod
    def _native_process_handle(proc: Any) -> Optional[subprocess.Popen]:
        candidate = getattr(getattr(proc, "_transport", None), "_proc", None)
        return candidate if candidate is not None else None

    def _poll_headless_native_returncode(self, proc: Any) -> Optional[int]:
        native_proc = self._native_process_handle(proc)
        if native_proc is None:
            return getattr(proc, "returncode", None)
        before_poll = getattr(proc, "returncode", None)
        try:
            native_returncode = native_proc.poll()
        except Exception:
            logger.exception(
                "headless native poll failed session_id=%s pid=%s",
                self.id,
                getattr(proc, "pid", None),
            )
            return getattr(proc, "returncode", None)
        effective_returncode = getattr(proc, "returncode", None)
        if effective_returncode is None:
            effective_returncode = native_returncode
        if before_poll is None and effective_returncode is not None:
            logger.warning(
                "headless native poll detected exited process session_id=%s pid=%s returncode=%s while wait_task pending",
                self.id,
                getattr(proc, "pid", None),
                effective_returncode,
            )
        return effective_returncode

    def _wait_headless_process_exit(self, proc: Any, *, timeout_s: float) -> str:
        wait_timeout = max(0.0, float(timeout_s))
        if getattr(proc, "returncode", None) is not None:
            return "exited"
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            active_loop = False
        else:
            active_loop = True
        if active_loop:
            return "active_loop"
        native_proc = self._native_process_handle(proc)
        if native_proc is not None:
            if wait_timeout <= 0:
                return "exited" if getattr(proc, "returncode", None) is not None else "timeout"
            wait_state = {"status": "timeout"}
            wait_done = threading.Event()

            def _wait_native_process() -> None:
                try:
                    native_proc.wait(timeout=wait_timeout)
                except subprocess.TimeoutExpired:
                    wait_state["status"] = "timeout"
                except Exception:
                    logger.exception(
                        "headless process wait failed session_id=%s pid=%s",
                        self.id,
                        getattr(proc, "pid", None),
                    )
                    wait_state["status"] = "error"
                else:
                    wait_state["status"] = "exited"
                finally:
                    wait_done.set()

            threading.Thread(
                target=_wait_native_process,
                name=f"headless-wait-{self.id}",
                daemon=True,
            ).start()
            wait_done.wait(timeout=wait_timeout)
            if wait_state["status"] == "exited":
                return "exited"
            if wait_state["status"] == "error":
                return "exited" if getattr(proc, "returncode", None) is not None else "timeout"
            return "exited" if getattr(proc, "returncode", None) is not None else "timeout"
        if wait_timeout <= 0:
            return "exited" if getattr(proc, "returncode", None) is not None else "timeout"
        wakeup = threading.Event()
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            if getattr(proc, "returncode", None) is not None:
                return "exited"
            wakeup.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        return "exited" if getattr(proc, "returncode", None) is not None else "timeout"

    def close_headless_process(self, *, wait_timeout_s: float = 1.0) -> None:
        proc = self.current_proc
        if proc is None:
            self._headless_interrupt_flag = False
            return
        try:
            if proc.returncode is not None:
                return
            self._stop_headless_process_tree(
                proc,
                wait_timeout_s=wait_timeout_s,
                action_label="close",
            )
        finally:
            self.current_proc = None
            self._headless_interrupt_flag = False
            if self._active_execution_backend == "headless":
                self._active_execution_backend = "none"

    def close(self, *, preserve_tmux: bool = False) -> None:
        if self._ssh_service is not None:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._ssh_service.close_all(workdir=self.workdir))
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            except RuntimeError:
                logging.getLogger(__name__).warning("SSH cleanup skipped: no running event loop")
            except Exception:
                logging.getLogger(__name__).exception("SSH cleanup failed")
        if self.child and self.child.isalive():
            try:
                self.child.close(force=True)
            except Exception:
                logging.getLogger(__name__).exception("interactive child close failed")
        self.close_headless_process()
        if preserve_tmux:
            return
        try:
            from app.services.cli_backends import TmuxExecutionBackend

            tmux_backend = TmuxExecutionBackend()
            active_cli = str(getattr(getattr(self, "cli", None), "active_cli", "") or "").strip()
            candidate_clis = [active_cli, *[str(name or "").strip() for name in (getattr(self.config, "tools", None) or {})]]
            for cli_name, backend_name in (getattr(self.cli, "execution_backends", None) or {}).items():
                if str(backend_name or "").strip().lower() == EXECUTION_BACKEND_TMUX:
                    candidate_clis.append(str(cli_name or "").strip())
            errors: list[BaseException] = []
            seen_clis: set[str] = set()

            def _close_tmux_candidates() -> None:
                original_cli = str(getattr(self.cli, "active_cli", "") or "").strip()
                original_tool = self.tool
                try:
                    try:
                        for cli_name in candidate_clis:
                            if not cli_name or cli_name in seen_clis:
                                continue
                            seen_clis.add(cli_name)
                            if cli_name in (getattr(self.config, "tools", None) or {}):
                                self.cli.active_cli = cli_name
                                self.tool = self.config.tools[cli_name]
                            paths = tmux_backend.paths(self)
                            should_close = (
                                str(getattr(self, "_active_execution_backend", "") or "").strip().lower() == "tmux"
                                or os.path.exists(paths["state_path"])
                            )
                            if should_close:
                                asyncio.run(TmuxExecutionBackend().close(self))
                    except BaseException as exc:
                        errors.append(exc)
                finally:
                    self.cli.active_cli = original_cli
                    self.tool = original_tool

            if candidate_clis:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    _close_tmux_candidates()
                    if errors:
                        raise errors[0]
                else:
                    thread = threading.Thread(target=_close_tmux_candidates, daemon=True)
                    thread.start()
                    thread.join(timeout=2.0)
                    if thread.is_alive():
                        logging.getLogger(__name__).warning("tmux backend close still running after timeout")
                    if errors:
                        raise errors[0]
        except Exception:
            logging.getLogger(__name__).exception("tmux backend close failed")

    def _maybe_update_resume(self, output: str) -> None:
        if not self.tool.resume_regex:
            return
        import re

        match = re.search(self.tool.resume_regex, strip_ansi(output))
        if match:
            self.resume_token = match.group(1)

    def _maybe_autoset_resume_regex(self, output: str) -> None:
        if self.tool.resume_regex:
            return
        regex = detect_resume_regex(output)
        if regex:
            with _CONFIG_SAVE_LOCK:
                self.tool.resume_regex = regex
                save_config(self.config)

    def _update_activity(
        self,
        text: str,
        *,
        allow_short: bool = False,
        tick_kind: Optional[str] = None,
        replace_last: bool = False,
    ) -> None:
        normalized_tick_kind = str(tick_kind or "").strip().lower() or None
        raw = strip_ansi(str(text or ""))
        compact = " ".join(raw.split())
        if not compact:
            return
        if normalized_tick_kind == "assistant_text" and is_time_only_text(compact):
            return
        now = time.time()
        self.last_output_ts = now
        tokens = extract_tick_tokens(raw)
        if tokens:
            candidate = str(tokens[-1] or "").strip()
            if len(candidate) < 6:
                if not allow_short:
                    return
                last = compact
            else:
                last = candidate
        else:
            last = compact
            if len(last.strip()) < 6 and not allow_short:
                return
        if not self.last_tick_value:
            # First observed tick should be visible in status immediately.
            self.last_tick_ts = now
            self.tick_seen = max(1, int(self.tick_seen or 0))
            append_session_tick(
                self,
                value=last,
                ts=now,
                allow_short=allow_short,
                kind=normalized_tick_kind,
                replace_last=replace_last,
            )
        elif last != self.last_tick_value:
            self.last_tick_ts = now
            replaced = append_session_tick(
                self,
                value=last,
                ts=now,
                allow_short=allow_short,
                kind=normalized_tick_kind,
                replace_last=replace_last,
            )
            if not replaced:
                self.tick_seen += 1
        else:
            # Keep activity heartbeat fresh even when the same marker repeats.
            self.last_tick_ts = now
        self.last_tick_value = last
        if normalized_tick_kind == "assistant_text":
            self.last_assistant_text_ts = now
            self.last_assistant_text_value = last

    def _on_qwen_event(self, event) -> None:
        """Handle Qwen JSONL monitor event."""
        try:
            progress_text = extract_progress_text(event)
            if progress_text:
                # Update activity with progress text
                self._update_activity(progress_text)
        except Exception:
            logging.getLogger(__name__).exception("Qwen event handler failed")

    def _start_qwen_monitor(self) -> None:
        """Start Qwen Code JSONL monitor for real-time progress."""
        if self._qwen_monitor:
            return  # Already started

        self._qwen_monitor = QwenJsonlMonitor(
            workdir=self.workdir,
            callback=self._on_qwen_event,
            poll_interval=0.3,  # Poll every 300ms for responsive progress
        )
        self._qwen_monitor.start()
        logging.getLogger(__name__).info("Qwen JSONL monitor started for session %s", self.id)

    def _stop_qwen_monitor(self) -> None:
        """Stop Qwen Code JSONL monitor."""
        if self._qwen_monitor:
            self._qwen_monitor.stop()
            self._qwen_monitor = None
            logging.getLogger(__name__).info("Qwen JSONL monitor stopped for session %s", self.id)

    def _on_gemini_session_detected(self, session_id: str) -> None:
        """Persist Gemini session id for future resume."""
        sid = str(session_id or "").strip()
        if sid:
            self.resume_token = sid

    def _on_gemini_event(self, event) -> None:
        """Handle Gemini session monitor events."""
        try:
            for item in list(getattr(event, "progress_items", []) or []):
                text = str(item or "").strip()
                if text:
                    self._update_activity(text)
        except Exception:
            logging.getLogger(__name__).exception("Gemini event handler failed")

    def _start_gemini_monitor(self, session_id: Optional[str], *, persist_resume: bool = True) -> None:
        """Start Gemini CLI session monitor for progress and session id."""
        if self._gemini_monitor:
            return

        self._gemini_monitor = GeminiJsonMonitor(
            workdir=self.workdir,
            callback=self._on_gemini_event,
            session_callback=self._on_gemini_session_detected if persist_resume else None,
            poll_interval=0.2,
            session_id=session_id,
        )
        self._gemini_monitor.start()
        logging.getLogger(__name__).info("Gemini JSON monitor started for session %s", self.id)

    def _stop_gemini_monitor(self) -> None:
        """Stop Gemini CLI session monitor."""
        if self._gemini_monitor:
            self._gemini_monitor.stop()
            self._gemini_monitor = None
            logging.getLogger(__name__).info("Gemini JSON monitor stopped for session %s", self.id)

    def _on_claude_session_detected(self, session_id: str) -> None:
        """Persist Claude transcript session id for future resume."""
        sid = str(session_id or "").strip()
        if sid:
            self.resume_token = sid

    def _on_claude_event(self, event) -> None:
        """Handle Claude transcript events."""
        try:
            for item in list(getattr(event, "progress_items", []) or []):
                text = str(item or "").strip()
                if text:
                    self._update_activity(text)
        except Exception:
            logging.getLogger(__name__).exception("Claude event handler failed")

    def _start_claude_monitor(self, session_id: Optional[str], *, persist_resume: bool = True) -> None:
        """Start Claude Code transcript monitor for progress and session id."""
        if self._claude_monitor:
            return

        self._claude_monitor = ClaudeJsonlMonitor(
            workdir=self.workdir,
            callback=self._on_claude_event,
            session_callback=self._on_claude_session_detected if persist_resume else None,
            poll_interval=0.3,
            username="claude-bot",
            session_id=session_id,
        )
        self._claude_monitor.start()
        logging.getLogger(__name__).info("Claude JSONL monitor started for session %s", self.id)

    def _stop_claude_monitor(self) -> None:
        """Stop Claude Code transcript monitor."""
        if self._claude_monitor:
            self._claude_monitor.stop()
            self._claude_monitor = None
            logging.getLogger(__name__).info("Claude JSONL monitor stopped for session %s", self.id)

    def _build_gemini_resume_template(self, cmd_template: List[str]) -> List[str]:
        """
        Convert gemini '--resume latest' into '--resume {resume}' when we already
        have a concrete token and no dedicated resume_cmd is configured.
        """
        cmd = list(cmd_template)
        for idx in range(len(cmd) - 1):
            if cmd[idx] == "--resume" and cmd[idx + 1] == "latest":
                cmd[idx + 1] = "{resume}"
                break
        return cmd

    def _ensure_codex_json_command(self, cmd: List[str]) -> List[str]:
        """
        Ensure codex headless execution emits JSONL on stdout so completion can be
        detected from semantic events instead of process shutdown.
        """
        out = [str(part) for part in (cmd or [])]
        if len(out) < 2 or out[0] != "codex" or out[1] != "exec" or "--json" in out:
            return out
        insert_at = 2
        if len(out) >= 4 and out[2] == "resume":
            insert_at = 4
        out.insert(min(insert_at, len(out)), "--json")
        return out

    def _ensure_gemini_stream_json_command(self, cmd: List[str]) -> List[str]:
        return self._ensure_stream_json_output_command(cmd, binary="gemini")

    def _ensure_qwen_stream_json_command(self, cmd: List[str]) -> List[str]:
        return self._ensure_stream_json_output_command(cmd, binary="qwen")

    def _ensure_claude_stream_json_command(self, cmd: List[str]) -> List[str]:
        out = [str(part) for part in (cmd or [])]
        if not out or out[0] != "claude":
            return out
        insert_at = 1
        if "-p" not in out and "--print" not in out:
            out.insert(insert_at, "-p")
            insert_at += 1
        if "--verbose" not in out:
            out.insert(insert_at, "--verbose")
            insert_at += 1
        if "--output-format" in out:
            idx = out.index("--output-format")
            if idx + 1 < len(out):
                out[idx + 1] = "stream-json"
                return out
            out.append("stream-json")
            return out
        out[insert_at:insert_at] = ["--output-format", "stream-json"]
        return out

    def _ensure_grok_streaming_json_command(self, cmd: List[str]) -> List[str]:
        out = [str(part) for part in (cmd or [])]
        if not out or out[0] != "grok":
            return out
        if "--output-format" in out:
            idx = out.index("--output-format")
            if idx + 1 < len(out):
                out[idx + 1] = "streaming-json"
                return out
            out.append("streaming-json")
            return out
        out[1:1] = ["--output-format", "streaming-json"]
        return out

    def _ensure_stream_json_output_command(self, cmd: List[str], *, binary: str) -> List[str]:
        """
        Ensure a headless CLI emits machine-readable JSONL on stdout.
        Keep this as a runtime guard so older configs still work during rollout.
        """
        out = [str(part) for part in (cmd or [])]
        if not out or out[0] != binary:
            return out
        if "--output-format" in out:
            idx = out.index("--output-format")
            if idx + 1 < len(out):
                out[idx + 1] = "stream-json"
                return out
            out.append("stream-json")
            return out
        out[1:1] = ["--output-format", "stream-json"]
        return out

    async def _recover_gemini_resume_token_from_list_sessions(self) -> None:
        """For Gemini, derive current resume token from `--list-sessions`."""
        _log = logging.getLogger("session.headless")
        gemini_bin = self.tool.cmd[0] if self.tool.cmd else "gemini"
        cmd = [gemini_bin, "--list-sessions"]
        env = os.environ.copy()
        if self.tool.env:
            for key, value in self.tool.env.items():
                if value is None:
                    continue
                env[key] = resolve_env_value(str(value))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except Exception:
            _log.exception("gemini list-sessions recovery failed")
            return
        text = (out or b"").decode(errors="ignore")
        stderr_text = (err or b"").decode(errors="ignore")
        combined = f"{text}\n{stderr_text}"
        token = self._extract_gemini_just_now_token(combined)
        if token:
            self.resume_token = token
            _log.info("gemini resume token recovered from --list-sessions: %s", token)

    def _extract_gemini_just_now_token(self, text: str) -> Optional[str]:
        # Preferred shape: "... (Just now) [<uuid>]"
        strict_matches = re.findall(r"\(Just now\).*?\[([0-9a-fA-F-]{36})\]\s*$", text, flags=re.MULTILINE)
        if strict_matches:
            return strict_matches[-1]
        # Support non-UUID token formats.
        loose_matches = re.findall(r"\(Just now\).*?\[([^\]]+)\]", text, flags=re.MULTILINE)
        if loose_matches:
            return loose_matches[-1].strip()
        return None

    def _recover_qwen_resume_token_from_chat_files(self) -> None:
        """
        For Qwen, resume token is the newest chat filename stem under:
        ~/.qwen/projects/<workdir-key>/chats/*.jsonl
        """
        _log = logging.getLogger("session.headless")
        home = os.path.expanduser("~")
        base = os.path.join(home, ".qwen", "projects")
        best_mtime = -1.0
        best_token: Optional[str] = None
        for key in self._qwen_project_key_candidates():
            chats_dir = os.path.join(base, key, "chats")
            if not os.path.isdir(chats_dir):
                continue
            try:
                for name in os.listdir(chats_dir):
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(chats_dir, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    if st.st_mtime > best_mtime:
                        best_mtime = st.st_mtime
                        best_token = os.path.splitext(name)[0]
            except OSError:
                continue
        if best_token:
            self.resume_token = best_token
            _log.info("qwen resume token recovered from chats dir: %s", best_token)

    def _qwen_project_key_candidates(self) -> List[str]:
        raw = os.path.realpath(self.workdir).rstrip(os.sep) or self.workdir
        slash_key = raw.replace(os.sep, "-")
        compact_key = re.sub(r"[^A-Za-z0-9]+", "-", raw)
        if raw.startswith(os.sep) and not compact_key.startswith("-"):
            compact_key = "-" + compact_key
        compact_key = compact_key.rstrip("-")
        # Keep order stable, drop duplicates.
        out: List[str] = []
        for key in (slash_key, compact_key):
            if key and key not in out:
                out.append(key)
        return out

    def is_active_by_tick(self, now: Optional[float] = None, window_sec: int = 3) -> bool:
        if not self.last_tick_ts:
            return False
        now = time.time() if now is None else now
        return (now - self.last_tick_ts) <= window_sec

    @staticmethod
    def _signal_process_group(pid: Optional[int], sig: int) -> bool:
        if pid is None:
            return False
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return True
        except Exception:
            return False

    @staticmethod
    def _iter_process_parent_pairs() -> list[tuple[int, int]]:
        proc_dir = "/proc"
        pairs: list[tuple[int, int]] = []
        try:
            names = os.listdir(proc_dir)
        except OSError:
            return pairs
        for name in names:
            if not name.isdigit():
                continue
            try:
                pid = int(name)
            except ValueError:
                continue
            try:
                with open(os.path.join(proc_dir, name, "stat"), encoding="utf-8") as fh:
                    stat = fh.read()
            except OSError:
                continue
            end = stat.rfind(")")
            if end < 0:
                continue
            fields = stat[end + 2:].split()
            if len(fields) < 2:
                continue
            try:
                pairs.append((pid, int(fields[1])))
            except ValueError:
                continue
        return pairs

    @classmethod
    def _headless_process_descendants(cls, pid: Optional[int]) -> list[int]:
        if pid is None:
            return []
        children_by_parent: dict[int, list[int]] = {}
        for child_pid, parent_pid in cls._iter_process_parent_pairs():
            children_by_parent.setdefault(parent_pid, []).append(child_pid)
        descendants: list[int] = []
        seen = {int(pid)}
        stack = list(children_by_parent.get(int(pid), []))
        while stack:
            child_pid = stack.pop()
            if child_pid in seen:
                continue
            seen.add(child_pid)
            descendants.append(child_pid)
            stack.extend(children_by_parent.get(child_pid, []))
        return descendants

    @staticmethod
    def _process_group_id(pid: int) -> Optional[int]:
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None
        except Exception:
            return None

    def _signal_headless_process_tree(self, pid: Optional[int], sig: int) -> bool:
        if pid is None:
            return False
        signaled = self._signal_process_group(pid, sig)
        signaled_groups: set[int] = {int(pid)} if signaled else set()
        descendants = self._headless_process_descendants(pid)
        current_group = os.getpgrp()
        for child_pid in descendants:
            group_id = self._process_group_id(child_pid)
            if group_id is None or group_id in signaled_groups:
                continue
            if group_id == current_group:
                try:
                    os.kill(child_pid, sig)
                    signaled = True
                except ProcessLookupError:
                    signaled = True
                except Exception:
                    logger.exception(
                        "headless descendant signal failed session_id=%s pid=%s signal=%s",
                        self.id,
                        child_pid,
                        sig,
                    )
                continue
            if self._signal_process_group(group_id, sig):
                signaled = True
                signaled_groups.add(group_id)
        if descendants:
            logger.info(
                "headless process tree signal sent session_id=%s root_pid=%s signal=%s descendants=%d",
                self.id,
                pid,
                sig,
                len(descendants),
            )
        return signaled

    def _stop_headless_process_tree(
        self,
        proc: Any,
        *,
        wait_timeout_s: float,
        action_label: str,
    ) -> None:
        pid = getattr(proc, "pid", None)
        wait_timeout = max(0.0, float(wait_timeout_s))
        group_signal_sent = self._signal_headless_process_tree(pid, signal.SIGTERM)
        if not group_signal_sent:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
            except Exception:
                logger.exception(
                    "headless %s terminate failed session_id=%s pid=%s",
                    action_label,
                    self.id,
                    pid,
                )
        if getattr(proc, "returncode", None) is not None:
            return

        wait_result = self._wait_headless_process_exit(proc, timeout_s=wait_timeout)
        if wait_result == "exited":
            return
        if wait_result == "active_loop":
            logger.warning(
                "headless %s degraded to direct kill session_id=%s pid=%s",
                action_label,
                self.id,
                pid,
            )
        else:
            logger.warning(
                "headless %s graceful timeout session_id=%s pid=%s timeout_s=%.1f",
                action_label,
                self.id,
                pid,
                wait_timeout,
            )

        group_kill_sent = (
            self._signal_headless_process_tree(pid, signal.SIGKILL)
            if group_signal_sent
            else False
        )
        if not group_kill_sent:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            except Exception:
                logger.exception(
                    "headless %s kill failed session_id=%s pid=%s",
                    action_label,
                    self.id,
                    pid,
                )
                return
        if self._wait_headless_process_exit(proc, timeout_s=wait_timeout) != "exited":
            logger.warning(
                "headless process survived %s kill session_id=%s pid=%s",
                action_label,
                self.id,
                pid,
            )

    def _cli_process_name(self) -> Optional[str]:
        if self.tool.cmd:
            primary = os.path.basename(self.tool.cmd[0])
            wrappers = {"python", "python3", "node", "bash", "sh", "npx", "pnpm", "yarn", "npm"}
            if primary in wrappers:
                for item in self.tool.cmd[1:]:
                    if not item or item.startswith("-"):
                        continue
                    return os.path.basename(item)
            return primary
        return None

    def _is_cli_process_alive(self, name: str) -> bool:
        if not name:
            return False
        quoted = shlex.quote(name)
        cmd = f"ps -ef | grep -w {quoted} | grep -v grep"
        try:
            completed = subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                timeout=2,
            )
            return bool((completed.stdout or "").strip())
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return False


class SessionManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self._state_repo = get_state_repository(self.config.defaults.state_path)
        # Chat-scoped sessions: different Telegram chats must not share sessions/active state.
        self.sessions_by_chat: Dict[int, Dict[str, Session]] = {}
        self._counter_by_chat: Dict[int, int] = {}
        # uid -> Session fast-lookup index (M9-uid). Keyed by session_runtime_uid(session).
        self._session_by_uid: Dict[str, "Session"] = {}
        # Optional callback invoked whenever the session inventory changes.
        # Signature: callback(chat_id) -> None
        self.on_session_change: Optional[Callable[[int], None]] = None
        self._persist_lock = threading.RLock()
        self._restore_sessions()

    def _is_tool_available(self, name: str) -> bool:
        return is_tool_available(self.config, name)

    def _available_tools(self) -> list[str]:
        return available_tools(self.config)

    def _pick_initial_cli(self, preferred: Optional[str] = None) -> Optional[str]:
        """
        Pick an initial active CLI for a new/restored session:
        - preferred (if provided and available)
        - defaults.default_cli (if set and available)
        - "qwen" (if available)
        - first available
        - else preferred/default/qwen even if unavailable (last resort)
        """
        preferred = str(preferred or "").strip() or None
        default_cli = str(getattr(self.config.defaults, "default_cli", None) or "").strip() or None
        candidates = [preferred, default_cli, "qwen"]
        available = self._available_tools()
        for c in candidates:
            if c and c in available:
                return c
        if available:
            return sorted(available)[0]
        # No available tools: fall back to a configured name to keep "active_cli" non-null.
        for c in candidates:
            if c and c in (self.config.tools or {}):
                return c
        return None

    def _ensure_chat(self, chat_id: int) -> None:
        if chat_id not in self.sessions_by_chat:
            self.sessions_by_chat[chat_id] = {}
        if chat_id not in self._counter_by_chat:
            self._counter_by_chat[chat_id] = 0

    def _index_session(self, session: "Session") -> None:
        """Add session to the uid index."""
        uid = session_runtime_uid(session)
        if uid:
            self._session_by_uid[uid] = session

    def _unindex_session(self, session: "Session") -> None:
        """Remove session from the uid index."""
        uid = session_runtime_uid(session)
        if uid and self._session_by_uid.get(uid) is session:
            del self._session_by_uid[uid]
        for key, indexed in list(self._session_by_uid.items()):
            if indexed is session:
                del self._session_by_uid[key]

    def _clear_session_sandbox_dir(self, scoped_key: str) -> bool:
        token = _sanitize_scoped_key_token(scoped_key)
        if not is_session_scoped_key(token):
            return False
        root = os.path.join(str(self.config.defaults.workdir), "_sandbox")
        try:
            real_root = os.path.realpath(root)
            target = sandbox_session_dir(str(self.config.defaults.workdir), token)
            real_target = os.path.realpath(target)
            if not real_target.startswith(real_root + os.sep):
                logging.getLogger(__name__).warning("refuse to clear sandbox outside root: %s", target)
                return False
            if os.path.isdir(real_target):
                shutil.rmtree(real_target)
                return True
            if os.path.exists(real_target):
                os.remove(real_target)
                return True
            return False
        except Exception:
            logging.getLogger(__name__).exception(
                "failed to clear sandbox dir for scoped_key=%s",
                token,
            )
            return False

    def _cleanup_legacy_session_sandbox_dir(self, session_id: str, *, scoped_key: str) -> bool:
        sid = _sanitize_scoped_key_token(session_id)
        scoped = _sanitize_scoped_key_token(scoped_key)
        if not sid or not scoped or sid == scoped:
            return False
        root = os.path.join(str(self.config.defaults.workdir), "_sandbox")
        try:
            legacy_target = legacy_sandbox_session_dir(str(self.config.defaults.workdir), sid)
            real_root = os.path.realpath(root)
            real_legacy_target = os.path.realpath(legacy_target)
            if not real_legacy_target.startswith(real_root + os.sep):
                logging.getLogger(__name__).warning(
                    "refuse to clear legacy sandbox outside root: %s",
                    legacy_target,
                )
                return False
            if os.path.isdir(real_legacy_target):
                shutil.rmtree(real_legacy_target)
                return True
            if os.path.exists(real_legacy_target):
                os.remove(real_legacy_target)
                return True
            return False
        except Exception:
            logging.getLogger(__name__).exception(
                "failed to cleanup legacy sandbox dir for session_id=%s scoped_key=%s",
                sid,
                scoped,
            )
            return False

    def create(
        self,
        chat_id: int,
        tool_name: Optional[str],
        workdir: str,
        *,
        conversation_scope: Optional[ConversationScope] = None,
        message_thread_id: Optional[int] = None,
    ) -> Session:
        initial = self._pick_initial_cli(tool_name)
        if not initial:
            raise RuntimeError("no CLI configured")
        tool = self.config.tools[initial]
        self._ensure_chat(chat_id)
        self._counter_by_chat[chat_id] += 1
        sid = f"s{self._counter_by_chat[chat_id]}"
        scoped_key = build_session_scoped_key(0 if str(chat_id) == "desktop" else chat_id, sid)
        # One-time migration cleanup for the old raw-session sandbox name, then
        # defensive cleanup for the canonical scoped sandbox path.
        self._cleanup_legacy_session_sandbox_dir(sid, scoped_key=scoped_key)
        self._clear_session_sandbox_dir(scoped_key)
        try:
            ensure_project_prompts(workdir)
        except Exception:
            logger.exception("project prompts bootstrap failed workdir=%s session_id=%s", workdir, sid)
        ensure_cli_proxy_gitignored(workdir)
        scope = conversation_scope or ConversationScope.from_parts(chat_id, message_thread_id)
        session = Session(
            id=sid,
            tool=tool,
            workdir=workdir,
            idle_timeout_sec=self.config.defaults.idle_timeout_sec,
            config=self.config,
            active_cli=initial,
            chat_id=0 if str(chat_id) == "desktop" else int(chat_id),
            conversation_scope=scope,
        )
        session.name = f"{tool.name}@{workdir}"
        # Do not load state by (tool, workdir): it is ambiguous when multiple sessions share them.
        self.sessions_by_chat[chat_id][sid] = session
        self._index_session(session)
        self._persist_sessions()
        self._fire_session_change(chat_id)
        return session

    def sessions_for_chat(self, chat_id: int) -> Dict[str, Session]:
        self._ensure_chat(chat_id)
        sessions = self.sessions_by_chat[chat_id]
        if len(sessions) <= 1:
            return sessions
        ordered = sorted(sessions.items(), key=lambda item: self._session_sort_key(item[0], item[1]))
        return {sid: session for sid, session in ordered}

    @staticmethod
    def _session_sort_key(session_id: str, session: Session) -> tuple[str, str, str]:
        workdir_raw = str(getattr(session, "workdir", "") or "").strip()
        normalized = os.path.normpath(workdir_raw) if workdir_raw else ""
        dirname = os.path.basename(normalized) if normalized else ""
        if not dirname:
            dirname = normalized or workdir_raw
        return (dirname.lower(), dirname, str(session_id or ""))

    def get(self, chat_id: int, session_id: str) -> Optional[Session]:
        candidates: list[Any] = [chat_id]
        if isinstance(chat_id, str):
            try:
                numeric_chat_id = int(chat_id)
            except (TypeError, ValueError):
                numeric_chat_id = None
            if numeric_chat_id is not None:
                candidates.append(numeric_chat_id)
        elif isinstance(chat_id, int):
            candidates.append(str(chat_id))
        for candidate in candidates:
            sessions = self.sessions_by_chat.get(candidate)
            if isinstance(sessions, dict):
                resolved = sessions.get(session_id)
                if resolved is not None:
                    return resolved
        self._ensure_chat(chat_id)
        return self.sessions_by_chat[chat_id].get(session_id)

    def get_by_uid(self, session_uid: str) -> Optional[Session]:
        token = str(session_uid or "").strip()
        if not token:
            return None
        # Fast path: O(1) index lookup (M9-uid).
        indexed = self._session_by_uid.get(token)
        if indexed is not None:
            if session_runtime_uid(indexed) == token:
                return indexed
            self._session_by_uid.pop(token, None)
        if token.startswith("chat:"):
            chat_parts = token.split(":", 2)
            if len(chat_parts) == 3:
                try:
                    resolved = self.get(int(chat_parts[1]), str(chat_parts[2] or "").strip())
                except Exception:
                    resolved = None
                if resolved is not None and session_runtime_uid(resolved) == token:
                    self._index_session(resolved)
                    return resolved
        if token.startswith("desktop:"):
            desktop_session_id = token.split(":", 1)[1].strip()
            if desktop_session_id:
                desktop_session = self.get("desktop", desktop_session_id)
                if desktop_session is not None and session_runtime_uid(desktop_session) == token:
                    self._index_session(desktop_session)
                    return desktop_session
        # Fallback: linear scan (handles index de-sync and scope-uid / raw-id matches).
        scope_matches: list[Session] = []
        for sessions in self.sessions_by_chat.values():
            for session in sessions.values():
                if session_runtime_uid(session) == token:
                    self._index_session(session)
                    return session
                scope = getattr(session, "scope", None)
                if scope is None:
                    scope = getattr(session, "conversation_scope", None)
                scope_uid = str(getattr(scope, "session_uid", "") or "").strip()
                if scope_uid == token:
                    scope_matches.append(session)
                # Keep fake/test objects without any scope addressable by raw id.
                if not scope_uid and str(getattr(session, "id", "") or "").strip() == token:
                    return session
        if len(scope_matches) == 1:
            return scope_matches[0]
        return None

    def get_by_scope(self, chat_id: int, message_thread_id: Optional[int] = None) -> Optional[Session]:
        scope_chat_id = int(chat_id)
        target_thread_id = int(message_thread_id) if message_thread_id is not None else None
        matches: list[Session] = []
        for sessions in self.sessions_by_chat.values():
            for session in sessions.values():
                scope = getattr(session, "conversation_scope", None)
                if not isinstance(scope, ConversationScope):
                    continue
                if int(scope.chat_id) != scope_chat_id:
                    continue
                current_thread_id = int(scope.message_thread_id) if scope.message_thread_id is not None else None
                if current_thread_id != target_thread_id:
                    continue
                matches.append(session)
        if len(matches) == 1:
            return matches[0]
        return None

    def close(self, chat_id: int, session_id: str) -> bool:
        self._ensure_chat(chat_id)
        session = self.sessions_by_chat[chat_id].pop(session_id, None)
        if not session:
            return False
        self._unindex_session(session)
        session.close()
        self._clear_session_sandbox_dir(session_scoped_key(session))
        self._persist_sessions()
        try:
            with self._persist_lock:
                self._state_repo.delete_session(chat_id=chat_id, session_id=session_id)
        except Exception:
            logger.exception("failed to remove session from persisted storage chat_id=%s session_id=%s", chat_id, session_id)
        self._fire_session_change(chat_id)
        return True

    def close_by_uid(self, session_uid: str) -> bool:
        session = self.get_by_uid(session_uid)
        if not session:
            return False
        for chat_id, sessions in self.sessions_by_chat.items():
            for managed_session in sessions.values():
                if managed_session is session:
                    return self.close(chat_id, session.id)
        return False

    def _fire_session_change(self, chat_id: int) -> None:
        """Invoke the on_session_change callback if registered."""
        cb = self.on_session_change
        if cb:
            try:
                cb(chat_id)
            except Exception:
                logging.exception("on_session_change callback failed")

    @staticmethod
    def _normalize_queue_items_for_persistence(raw_queue: Any) -> list[Dict[str, Any]]:
        queue_items: list[Dict[str, Any]] = []
        for item in raw_queue or []:
            try:
                payload = normalize_queue_item_payload(item, fallback_dest={"kind": "telegram"})
            except (TypeError, ValueError):
                logger.exception("invalid session queue item skipped during persistence")
                continue
            if not payload.get("text"):
                continue
            queue_items.append(payload)
        return queue_items

    @staticmethod
    def _serialize_session_payload(session: Session) -> Dict[str, Any]:
        scope = getattr(session, "conversation_scope", None)
        if not isinstance(scope, (ConversationScope, DesktopScope)):
            scope = ConversationScope.from_parts(getattr(session, "chat_id", 0) or 0)
        queue_items = SessionManager._normalize_queue_items_for_persistence(session.queue)
        cli_payload = {
            "active_cli": session.cli.active_cli or session.tool.name,
            "resume_tokens": dict(session.cli.resume_tokens or {}),
            "tmux_users": dict(getattr(session.cli, "tmux_users", None) or {}),
            "cli_work_type": session.cli.cli_work_type,
            "auto_commands_ran": bool(session.cli.auto_commands_ran),
        }
        git_payload = {
            "busy": bool(session.git.busy),
            "conflict": bool(session.git.conflict),
            "conflict_files": list(session.git.conflict_files or []),
            "conflict_kind": session.git.conflict_kind,
        }
        orchestrator_payload = {
            "enabled": bool(session.orchestrator.enabled),
            "pending_input": session.orchestrator.pending_input,
            "last_mode_output": session.orchestrator.last_mode_output,
            "last_mode_id": session.orchestrator.last_mode_id,
        }
        _sdd = getattr(session, "sdd", None)
        if not isinstance(_sdd, SddState):
            _sdd = SddState()
        sdd_payload = {
            "feature_slug": _sdd.feature_slug,
            "spec_dir": _sdd.spec_dir,
            "phase": _sdd.phase,
            "pending_gate": _sdd.pending_gate,
            "constitution_path": _sdd.constitution_path,
            "source_intent": _sdd.source_intent,
            "last_action": _sdd.last_action,
            "project_init_status": _sdd.project_init_status,
            "project_init_step": _sdd.project_init_step,
            "project_init_kind": _sdd.project_init_kind,
            "project_init_started_at": _sdd.project_init_started_at,
            "project_init_finished_at": _sdd.project_init_finished_at,
            "project_init_error": _sdd.project_init_error,
            "project_profile_path": _sdd.project_profile_path,
            "project_init_snapshot_path": _sdd.project_init_snapshot_path,
        }
        modes_payload = {
            "active_mode": session.modes.active_mode,
            "analyst_mode": str(session.modes.analyst_mode or "spec"),
            "analyst_template_id": str(session.modes.analyst_template_id or "default"),
            "manager_quiet_mode": bool(session.modes.manager_quiet_mode),
            "agent_memory": dict(session.modes.agent_memory or {}),
            "ssh_remote_enabled": bool(session.modes.ssh_remote_enabled),
            "remote_control_enabled": bool(session.modes.remote_control_enabled),
            "remote_control_host_alias": session.modes.remote_control_host_alias,
        }
        return {
            "workdir": session.workdir,
            "name": session.name,
            "scoped_key": session_scoped_key(session),
            "cli": cli_payload,
            "git": git_payload,
            "orchestrator": orchestrator_payload,
            "sdd": sdd_payload,
            "modes": modes_payload,
            "executor_profile": getattr(session, "executor_profile", None),
            "summary": getattr(session, "state_summary", None),
            "updated_at": getattr(session, "state_updated_at", None),
            "queue": queue_items,
            "project_root": getattr(session, "project_root", None),
            "conversation_scope": scope.to_payload(),
            "message_thread_id": getattr(scope, "message_thread_id", None),
            "session_uid": scope.session_uid,
            "session_surface": scope.session_surface,
        }

    def _serialize_chat_entry(self, chat_id: int) -> Dict[str, Any]:
        self._ensure_chat(chat_id)
        sessions = self.sessions_by_chat.get(chat_id, {})
        data: Dict[str, Any] = {}
        for sid, session in dict(sessions).items():
            data[str(sid)] = self._serialize_session_payload(session)
        return {
            "sessions": data,
            "counter": int(self._counter_by_chat.get(chat_id, 0)),
        }

    def serialize_chat_entry_for_persist(self, chat_id: int, session_id: str) -> Optional[Dict[str, Any]]:
        """Сериализует chat-entry на ВЫЗЫВАЮЩЕМ потоке (ожидается event loop).

        H1: _serialize_session_payload итерирует живые session.queue/dict, поэтому
        снимать снапшот можно только на loop-потоке — параллельная мутация очереди
        из корутин при сериализации в worker-потоке ломает обход
        ("deque mutated during iteration"). Возвращает None, если сессии нет.
        """
        self._ensure_chat(chat_id)
        sid = str(session_id or "").strip()
        if not sid:
            return None
        if self.sessions_by_chat.get(chat_id, {}).get(sid) is None:
            return None
        return self._serialize_chat_entry(chat_id)

    def write_chat_entry(self, chat_id: int, entry: Dict[str, Any]) -> bool:
        """Пишет уже сериализованную chat-entry в state-repo. Безопасно из worker-потока."""
        try:
            with self._persist_lock:
                self._state_repo.replace_chat_entry(chat_id=chat_id, entry=entry)
            return True
        except Exception:
            logger.exception("failed to write chat entry chat_id=%s", chat_id)
            return False

    def persist_session(self, chat_id: int, session_id: str) -> bool:
        entry = self.serialize_chat_entry_for_persist(chat_id, session_id)
        if entry is None:
            return False
        if self.write_chat_entry(chat_id, entry):
            return True
        self._persist_sessions()
        return False

    def _persist_sessions(self) -> None:
        try:
            with self._persist_lock:
                sessions_by_chat_snapshot = {
                    chat_id: dict(sessions)
                    for chat_id, sessions in self.sessions_by_chat.items()
                }
                counter_snapshot = dict(self._counter_by_chat)

                for chat_id, sessions in sessions_by_chat_snapshot.items():
                    data: Dict[str, Any] = {}
                    for sid, s in sessions.items():
                        data[sid] = self._serialize_session_payload(s)
                    self._state_repo.replace_chat_entry(
                        chat_id=chat_id,
                        entry={
                            "sessions": data,
                            "counter": int(counter_snapshot.get(chat_id, 0)),
                        },
                    )
        except Exception:
            logger.exception("failed to persist sessions state")

    def _restore_sessions(self) -> None:
        # Preferred: chat-scoped storage.
        try:
            by_chat = self._state_repo.load_sessions_by_chat()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            by_chat = {}

        if isinstance(by_chat, dict) and by_chat:
            for chat_key, entry in by_chat.items():
                if str(chat_key) == "desktop":
                    chat_id = "desktop"
                else:
                    try:
                        chat_id = int(chat_key)
                    except Exception:
                        continue
                if not isinstance(entry, dict):
                    continue
                sessions = entry.get("sessions", {}) or {}
                if not isinstance(sessions, dict):
                    continue
                self._ensure_chat(chat_id)
                try:
                    self._counter_by_chat[chat_id] = int(entry.get("counter") or 0)
                except Exception:
                    self._counter_by_chat[chat_id] = 0
                for sid, val in sessions.items():
                    if not isinstance(val, dict):
                        continue
                    cli_payload = val.get("cli") if isinstance(val.get("cli"), dict) else {}
                    modes_payload = val.get("modes") if isinstance(val.get("modes"), dict) else {}
                    orchestrator_payload = val.get("orchestrator") if isinstance(val.get("orchestrator"), dict) else {}
                    git_payload = val.get("git") if isinstance(val.get("git"), dict) else {}
                    sdd_payload = val.get("sdd") if isinstance(val.get("sdd"), dict) else {}

                    requested_cli = str(cli_payload.get("active_cli") or val.get("active_cli") or "").strip()
                    active_cli = requested_cli
                    workdir = val.get("workdir")
                    if not workdir:
                        continue
                    fallback_cli = None
                    if active_cli and active_cli in self.config.tools and not self._is_tool_available(active_cli):
                        fallback_cli = pick_runtime_available_cli(self.config, preferred=active_cli)
                    elif not active_cli or active_cli not in self.config.tools:
                        fallback_cli = pick_runtime_available_cli(self.config, preferred=active_cli)
                    if fallback_cli:
                        active_cli = fallback_cli
                    elif not active_cli or active_cli not in self.config.tools:
                        active_cli = self._pick_initial_cli(active_cli) or ""
                    if not active_cli or active_cli not in self.config.tools:
                        continue

                    resume_tokens = cli_payload.get("resume_tokens", val.get("resume_tokens"))
                    if not isinstance(resume_tokens, dict):
                        resume_tokens = {}
                    tmux_users = cli_payload.get("tmux_users")
                    if not isinstance(tmux_users, dict):
                        tmux_users = {}
                    tmux_users = {
                        str(cli_name): str(tmux_user or "").strip() or None
                        for cli_name, tmux_user in tmux_users.items()
                        if str(cli_name or "").strip()
                    }
                    cli_work_type = cli_payload.get("cli_work_type", val.get("cli_work_type"))
                    cli_work_type = str(cli_work_type).strip() if cli_work_type is not None else None
                    auto_commands_ran = bool(cli_payload.get("auto_commands_ran", val.get("auto_commands_ran", False)))

                    analyst_template_id = modes_payload.get("analyst_template_id", val.get("analyst_template_id", "default"))
                    manager_quiet_mode = bool(modes_payload.get("manager_quiet_mode", val.get("manager_quiet_mode", False)))
                    agent_memory = modes_payload.get("agent_memory", val.get("agent_memory", {}))

                    git_busy = bool(git_payload.get("busy", val.get("git_busy", False)))
                    git_conflict = bool(git_payload.get("conflict", val.get("git_conflict", False)))
                    git_conflict_files = git_payload.get("conflict_files", val.get("git_conflict_files", []))
                    git_conflict_kind = git_payload.get("conflict_kind", val.get("git_conflict_kind"))

                    session_scope_payload = (
                        dict(val.get("conversation_scope") or {})
                        if isinstance(val.get("conversation_scope"), dict)
                        else {}
                    )
                    session_surface = str(val.get("session_surface") or session_scope_payload.get("session_surface") or "").strip()
                    session_uid = str(val.get("session_uid") or session_scope_payload.get("session_uid") or "").strip()
                    if session_surface == "desktop" or session_uid.startswith("desktop:"):
                        conversation_scope = DesktopScope(
                            str(session_scope_payload.get("project_slug") or "desktop"),
                            str(sid),
                        )
                        session_chat_id = 0
                    else:
                        conversation_scope = ConversationScope.from_payload(chat_id, val)
                        session_chat_id = 0 if str(chat_id) == "desktop" else int(chat_id)
                    session = Session(
                        id=str(sid),
                        tool=self.config.tools[active_cli],
                        workdir=workdir,
                        idle_timeout_sec=self.config.defaults.idle_timeout_sec,
                        config=self.config,
                        active_cli=active_cli,
                        resume_tokens=resume_tokens,
                        auto_commands_ran=auto_commands_ran,
                        cli_work_type=cli_work_type,
                        git_busy=git_busy,
                        git_conflict=git_conflict,
                        git_conflict_files=git_conflict_files if isinstance(git_conflict_files, list) else [],
                        git_conflict_kind=str(git_conflict_kind).strip() if git_conflict_kind is not None else None,
                        analyst_template_id=str(analyst_template_id or "default"),
                        manager_quiet_mode=bool(manager_quiet_mode),
                        agent_memory=dict(agent_memory) if isinstance(agent_memory, dict) else {},
                        chat_id=session_chat_id,
                        conversation_scope=conversation_scope,
                        scoped_key=str(val.get("scoped_key") or "").strip() or None,
                    )
                    session.cli.tmux_users.update(tmux_users)
                    if requested_cli and requested_cli != active_cli:
                        if requested_cli in self.config.tools:
                            try:
                                session._close_tmux_for_cli(requested_cli)
                            except Exception:
                                logger.exception(
                                    "startup CLI fallback could not close previous tmux session_id=%s "
                                    "previous_cli=%s fallback_cli=%s",
                                    sid,
                                    requested_cli,
                                    active_cli,
                                )
                                session.cli.active_cli = requested_cli
                                session.tool = self.config.tools[requested_cli]
                                active_cli = requested_cli
                        else:
                            removed_tool = ToolConfig(
                                name=requested_cli,
                                mode="headless",
                                cmd=[],
                                enabled=False,
                                tmux_user=session.cli.tmux_users.get(requested_cli),
                            )
                            try:
                                session._close_tmux_for_cli(
                                    requested_cli,
                                    tool_override=removed_tool,
                                )
                            except Exception:
                                logger.exception(
                                    "startup CLI fallback could not close removed CLI tmux "
                                    "session_id=%s previous_cli=%s fallback_cli=%s",
                                    sid,
                                    requested_cli,
                                    active_cli,
                                )
                                session.cli.active_cli = requested_cli
                                session.tool = removed_tool
                                active_cli = requested_cli
                    session.name = val.get("name") or f"{active_cli}@{workdir}"
                    session.state_summary = val.get("summary")
                    try:
                        session.state_updated_at = float(val.get("updated_at")) if val.get("updated_at") is not None else None
                    except Exception:
                        session.state_updated_at = None
                    active_mode = modes_payload.get("active_mode", val.get("active_mode"))
                    active_mode = str(active_mode).strip() if active_mode is not None else None
                    session.modes.active_mode = active_mode
                    analyst_mode = modes_payload.get("analyst_mode", "spec")
                    session.modes.analyst_mode = str(analyst_mode or "spec").strip() or "spec"
                    session.modes.ssh_remote_enabled = bool(
                        modes_payload.get("ssh_remote_enabled", False)
                    )
                    session.modes.remote_control_enabled = bool(
                        modes_payload.get("remote_control_enabled", False)
                    )
                    rc_alias = modes_payload.get("remote_control_host_alias")
                    session.modes.remote_control_host_alias = (
                        str(rc_alias).strip() if rc_alias is not None else None
                    ) or None
                    executor_profile = val.get("executor_profile")
                    executor_profile = str(executor_profile).strip() if executor_profile is not None else None
                    session.executor_profile = executor_profile
                    session.orchestrator.enabled = bool(
                        orchestrator_payload.get("enabled", val.get("advanced_orchestrator_enabled", False))
                    )
                    session.orchestrator.pending_input = orchestrator_payload.get(
                        "pending_input",
                        val.get("orchestrator_pending_input"),
                    )
                    last_mode_output = orchestrator_payload.get("last_mode_output", val.get("orchestrator_last_mode_output"))
                    session.orchestrator.last_mode_output = str(last_mode_output) if last_mode_output is not None else None
                    last_mode_id = orchestrator_payload.get("last_mode_id", val.get("orchestrator_last_mode_id"))
                    session.orchestrator.last_mode_id = str(last_mode_id).strip() if last_mode_id is not None else None
                    raw_phase = sdd_payload.get("phase")
                    session.sdd.phase = str(raw_phase).strip() or "idle" if raw_phase is not None else "idle"
                    session.sdd.feature_slug = sdd_payload.get("feature_slug") or None
                    session.sdd.spec_dir = sdd_payload.get("spec_dir") or None
                    session.sdd.pending_gate = sdd_payload.get("pending_gate") or None
                    session.sdd.constitution_path = sdd_payload.get("constitution_path") or None
                    session.sdd.source_intent = sdd_payload.get("source_intent") or None
                    session.sdd.last_action = str(sdd_payload.get("last_action") or "")
                    session.sdd.project_init_status = str(sdd_payload.get("project_init_status") or "idle")
                    session.sdd.project_init_step = str(sdd_payload.get("project_init_step") or "")
                    session.sdd.project_init_kind = str(sdd_payload.get("project_init_kind") or "")
                    try:
                        session.sdd.project_init_started_at = (
                            float(sdd_payload.get("project_init_started_at"))
                            if sdd_payload.get("project_init_started_at") is not None
                            else None
                        )
                    except Exception:
                        session.sdd.project_init_started_at = None
                    try:
                        session.sdd.project_init_finished_at = (
                            float(sdd_payload.get("project_init_finished_at"))
                            if sdd_payload.get("project_init_finished_at") is not None
                            else None
                        )
                    except Exception:
                        session.sdd.project_init_finished_at = None
                    session.sdd.project_init_error = str(sdd_payload.get("project_init_error") or "")
                    session.sdd.project_profile_path = str(sdd_payload.get("project_profile_path") or "")
                    session.sdd.project_init_snapshot_path = str(sdd_payload.get("project_init_snapshot_path") or "")
                    session.project_root = val.get("project_root")
                    if requested_cli and requested_cli != active_cli:
                        remember_session_cli_switch_notice(session, requested_cli, active_cli)
                    session.queue = deque(self._normalize_queue_items_for_persistence(val.get("queue", [])))
                    self._cleanup_legacy_session_sandbox_dir(session.id, scoped_key=session.scoped_key or "")
                    self.sessions_by_chat[chat_id][str(sid)] = session
                    self._index_session(session)
            return

        return


def run_tool_help(tool: ToolConfig, workdir: str, idle_timeout_sec: int, lang: str = "ru") -> str:
    cmd_template = tool.interactive_cmd or tool.cmd
    env = os.environ.copy()
    if tool.env:
        for k, v in tool.env.items():
            if v is None:
                continue
            env[k] = resolve_env_value(str(v))
    timeout = min(idle_timeout_sec, 20)
    child = pexpect.spawn(
        cmd_template[0],
        cmd_template[1:],
        cwd=workdir,
        encoding="utf-8",
        echo=False,
        timeout=timeout,
        env=env,
    )
    help_cmd = tool.help_cmd or "/help"
    child.sendline(help_cmd)
    if tool.prompt_regex:
        child.expect(tool.prompt_regex)
        output = child.before
    else:
        try:
            child.expect(pexpect.TIMEOUT)
        except Exception:
            logging.getLogger(__name__).exception("help probe: timeout wait failed")
        output = child.before
    try:
        child.close(force=True)
    except Exception:
        logging.getLogger(__name__).exception("help probe: child close failed")
    from i18n import t
    return output or t("msg.session.help_no_data", lang)
