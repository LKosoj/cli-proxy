from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from utils.cli import resolve_env_value
from utils.text import strip_ansi

from .models import ExecutionBackendStatus, ExecutionResult
from .transcript_reader import CliTranscriptReader, TranscriptLocator
from .tmux_driver import TmuxDriver, TmuxDriverError, resolve_user_identity, write_prompt_temp
from .tmux_parser import build_prompt_with_markers, done_marker, normalize_terminal_text, parse_tmux_delta


_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_RESUME_CONTROL_FLAGS_BY_CLI: dict[str, set[str]] = {
    "claude": {"--resume", "-r", "--continue", "-c"},
    "gemini": {"--resume", "-r"},
    "qwen": {"--resume", "-r", "--continue", "-c"},
    "grok": {"--resume", "-r", "--continue", "-c"},
}
_SESSION_ID_CONTROL_FLAGS_BY_CLI: dict[str, set[str]] = {
    "claude": {"--session-id"},
    "qwen": {"--session-id"},
    "grok": {"--session-id"},
}
_RESUME_FLAGS_BY_CLI: dict[str, list[str]] = {
    "claude": ["--resume"],
    "gemini": ["--resume"],
    "qwen": ["--resume"],
    "grok": ["--resume"],
}
_SESSION_ID_FLAGS_BY_CLI: dict[str, list[str]] = {
    "claude": ["--session-id"],
    "qwen": ["--session-id"],
    "grok": ["--session-id"],
}
_READY_WAIT_CLI_NAMES = {"claude", "codex", "qwen", "grok"}
_SINGLE_LINE_PROMPT_CLI_NAMES = {"codex", "qwen", "grok"}
_RECOVERY_DEST_KEYS = (
    "kind",
    "chat_id",
    "user_id",
    "message_thread_id",
    "direct_messages_topic_id",
    "session_uid",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TmuxRecoveryRequest:
    request_id: str
    started_at: float
    offset: int
    prompt: str
    dest: dict[str, Any]
    transcript_locator: Optional[TranscriptLocator] = None


def _session_uid(session: Any) -> str:
    scope = getattr(session, "scope", None) or getattr(session, "conversation_scope", None)
    token = str(getattr(scope, "session_uid", "") or "").strip()
    if token:
        session_id = str(getattr(session, "id", "") or "").strip()
        if token.startswith("chat:") and token.count(":") == 1 and session_id:
            return f"{token}:{session_id}"
        return token
    return str(getattr(session, "id", "") or "session").strip() or "session"


def _stable_session_identity(session: Any) -> str:
    session_id = str(getattr(session, "id", "") or "session").strip() or "session"
    scope = getattr(session, "scope", None) or getattr(session, "conversation_scope", None)
    chat_id = getattr(scope, "chat_id", None)
    if chat_id is not None:
        try:
            chat_token = str(int(chat_id))
        except Exception:
            chat_token = _safe_token(str(chat_id), max_len=24)
        return f"chat:{chat_token}:session:{session_id}"
    token = str(getattr(scope, "session_uid", "") or "").strip()
    if token:
        return f"{token}:session:{session_id}"
    return f"session:{session_id}"


def _safe_token(value: str, *, max_len: int = 42) -> str:
    token = _SAFE_TOKEN_RE.sub("-", str(value or "").strip()).strip("-._")
    return (token or "session")[:max_len]


def _active_cli(session: Any) -> str:
    return str(
        getattr(getattr(session, "cli", None), "active_cli", "")
        or getattr(getattr(session, "tool", None), "name", "")
        or "cli"
    ).strip()


def tmux_runtime_paths(session: Any) -> dict[str, str]:
    workdir = str(getattr(session, "workdir", "") or os.getcwd())
    active_cli = _active_cli(session)
    stable_identity = _stable_session_identity(session)
    identity = f"{stable_identity}|{active_cli}|{os.path.realpath(workdir)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    safe_key = f"{_safe_token(stable_identity)}-{_safe_token(active_cli, max_len=16)}-{digest}"
    runtime_dir = os.path.join(workdir, ".cli-proxy", "runtime", "tmux", safe_key)
    session_name = f"cli-proxy-{_safe_token(active_cli, max_len=16)}-{digest}"
    return {
        "runtime_dir": runtime_dir,
        "state_path": os.path.join(runtime_dir, "state.json"),
        "pane_log": os.path.join(runtime_dir, "pane.log"),
        "last_request_path": os.path.join(runtime_dir, "last_request.json"),
        "session_name": session_name,
        "pane_target": f"{session_name}:0.0",
    }


def _driver_for_session(session: Any) -> TmuxDriver:
    user = str(getattr(getattr(session, "tool", None), "tmux_user", "") or "").strip() or None
    return TmuxDriver(user=user)


def build_tmux_attach_command(session: Any) -> str:
    session_name = tmux_runtime_paths(session)["session_name"]
    return shlex.join(_driver_for_session(session).command("attach-session", "-r", "-t", session_name))


def _get_resume_token(session: Any) -> Optional[str]:
    token = str(getattr(session, "resume_token", "") or "").strip()
    if token:
        return token
    tokens = getattr(getattr(session, "cli", None), "resume_tokens", None)
    if isinstance(tokens, dict):
        token = str(tokens.get(_active_cli(session)) or "").strip()
        if token:
            return token
    return None


def _set_resume_token(session: Any, token: str) -> None:
    value = str(token or "").strip()
    if not value:
        return
    try:
        setattr(session, "resume_token", value)
    except Exception:
        logger.exception("failed to set tmux resume token session_id=%s", getattr(session, "id", "?"))
    tokens = getattr(getattr(session, "cli", None), "resume_tokens", None)
    if isinstance(tokens, dict):
        tokens[_active_cli(session)] = value


def _is_claude_command(command: list[str]) -> bool:
    return bool(command) and os.path.basename(str(command[0])) == "claude"


def _command_cli_name(session: Any, command: list[str]) -> str:
    explicit = str(getattr(getattr(session, "tool", None), "name", "") or "").strip().lower()
    if explicit:
        return explicit
    if command:
        return os.path.basename(str(command[0])).lower()
    return _active_cli(session).lower()


def _has_flag(command: list[str], flags: set[str]) -> bool:
    return any(
        part in flags
        or any(str(part).startswith(f"{flag}=") for flag in flags)
        for part in command
    )


def _has_resume_control(command: list[str], cli_name: str) -> bool:
    if cli_name == "codex" and len(command) > 1 and str(command[1]) == "resume":
        return True
    return _has_flag(command, _RESUME_CONTROL_FLAGS_BY_CLI.get(cli_name, set()))


def _has_session_id_control(command: list[str], cli_name: str) -> bool:
    return _has_flag(command, _SESSION_ID_CONTROL_FLAGS_BY_CLI.get(cli_name, set()))


def _has_fresh_session_control(command: list[str], cli_name: str) -> bool:
    return _has_resume_control(command, cli_name) or _has_session_id_control(command, cli_name)


def _without_session_id_control(command: list[str], cli_name: str) -> list[str]:
    flags = _SESSION_ID_CONTROL_FLAGS_BY_CLI.get(cli_name, set())
    if not flags:
        return command
    result: list[str] = []
    skip_next = False
    for part in command:
        item = str(part)
        if skip_next:
            skip_next = False
            continue
        if item in flags:
            skip_next = True
            continue
        if any(item.startswith(f"{flag}=") for flag in flags):
            continue
        result.append(item)
    return result


def _replace_command_placeholder(command: list[str], placeholder: str, value: str) -> tuple[list[str], bool]:
    changed = False
    result: list[str] = []
    for part in command:
        item = str(part)
        if placeholder in item:
            item = item.replace(placeholder, value)
            changed = True
        result.append(item)
    return result, changed


def _command_with_resume(session: Any, command: list[str], token: str) -> list[str]:
    tool = getattr(session, "tool", None)
    cli_name = _command_cli_name(session, command)
    resume_command = [str(part) for part in (getattr(tool, "interactive_resume_cmd", None) or [])]
    if resume_command:
        resume_command, replaced = _replace_command_placeholder(resume_command, "{resume}", token)
        if replaced:
            return resume_command
        return resume_command + [token]

    command, replaced = _replace_command_placeholder(command, "{resume}", token)
    if replaced or _has_resume_control(command, cli_name):
        return command
    command = _without_session_id_control(command, cli_name)
    if cli_name == "codex" and command:
        return [command[0], "resume", *command[1:], token]
    flags = _RESUME_FLAGS_BY_CLI.get(cli_name)
    if flags:
        return command + [flags[0], token]
    raise TmuxDriverError(f"interactive resume command is not configured for CLI '{cli_name}'")


def _command_with_session_id(session: Any, command: list[str], token: str) -> tuple[list[str], bool]:
    cli_name = _command_cli_name(session, command)
    command, replaced = _replace_command_placeholder(command, "{session_id}", token)
    if replaced:
        return command, True
    if _has_fresh_session_control(command, cli_name):
        return command, False
    flags = _SESSION_ID_FLAGS_BY_CLI.get(cli_name)
    if flags:
        return command + [flags[0], token], True
    return command, False


def _state_resume_token(state: dict[str, Any]) -> Optional[str]:
    for key in ("resume_token", "claude_resume_token"):
        token = str(state.get(key) or "").strip()
        if token:
            return token
    return None


def _capture_resume_token_from_output(session: Any, output: str) -> bool:
    previous = _get_resume_token(session)
    regex = str(getattr(getattr(session, "tool", None), "resume_regex", "") or "").strip()
    if not regex:
        return False
    match = re.search(regex, strip_ansi(output))
    if not match:
        return False
    token = match.group(1)
    _set_resume_token(session, token)
    return token != previous


def _new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _request_recovery_payload(request_context: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(request_context, dict):
        return {"delivery_state": "unmanaged"}
    raw_dest = request_context.get("dest")
    dest = {
        key: raw_dest.get(key)
        for key in _RECOVERY_DEST_KEYS
        if isinstance(raw_dest, dict) and key in raw_dest
    }
    return {
        "prompt": str(request_context.get("prompt") or ""),
        "dest": dest,
        "delivery_state": "pending",
    }


def _transcript_locator_from_payload(payload: dict[str, Any]) -> Optional[TranscriptLocator]:
    provider = str(payload.get("transcript_provider") or "").strip()
    path = str(payload.get("transcript_path") or "").strip()
    if not provider or not path:
        return None
    try:
        offset = max(0, int(payload.get("transcript_offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    return TranscriptLocator(
        provider=provider,
        path=path,
        start_offset=offset,
        session_id=str(payload.get("transcript_session_id") or "").strip(),
    )


def _prepare_interactive_command(session: Any, state: dict[str, Any], *, force_fresh: bool = False) -> list[str]:
    tool = getattr(session, "tool", None)
    command = [str(part) for part in (getattr(tool, "interactive_cmd", None) or [])]
    if not command:
        raise TmuxDriverError("interactive_cmd is required for tmux backend")
    state_stopped = str(state.get("state") or "").strip().lower() == "stopped"
    if not force_fresh and not state_stopped and not _get_resume_token(session):
        state_token = _state_resume_token(state)
        if state_token:
            _set_resume_token(session, state_token)

    resume_token = None if force_fresh else _get_resume_token(session)
    if resume_token:
        return _command_with_resume(session, command, resume_token)

    session_id = str(uuid.uuid4())
    command, token_assigned = _command_with_session_id(session, command, session_id)
    if token_assigned:
        _set_resume_token(session, session_id)
    return command


def _legacy_claude_resume_token(session: Any) -> Optional[str]:
    command = [str(part) for part in (getattr(getattr(session, "tool", None), "interactive_cmd", None) or [])]
    if not _is_claude_command(command):
        return None
    return _get_resume_token(session)


def _start_command(session: Any, state: Optional[dict[str, Any]] = None, *, force_fresh: bool = False) -> list[str]:
    command = _prepare_interactive_command(session, state or {}, force_fresh=force_fresh)
    env_cmd = ["env", "-u", "CLAUDECODE"]
    for key in sorted(os.environ):
        if key.startswith("CLAUDE_CODE_"):
            env_cmd.extend(["-u", key])
    tool = getattr(session, "tool", None)
    for key, raw_value in (getattr(tool, "env", None) or {}).items():
        value = resolve_env_value(raw_value)
        if value is not None:
            env_cmd.append(f"{key}={value}")
    return env_cmd + command


def _chmod_or_ignore(path: Path, mode_bits: int) -> None:
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        os.chmod(path, current | mode_bits)
    except OSError:
        pass


def _chown_chmod_for_user(path: Path, *, uid: int, gid: int, mode: int) -> None:
    try:
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    except OSError as exc:
        raise TmuxDriverError(f"failed to prepare tmux path permissions for {path}: {exc}") from exc


def _ensure_shared_runtime_permissions(paths: dict[str, str], *, user: str) -> None:
    identity = resolve_user_identity(user)
    if identity is None:
        return
    uid, gid = identity
    runtime_dir = Path(paths["runtime_dir"])
    _chmod_or_ignore(runtime_dir.parent.parent.parent, stat.S_IXGRP | stat.S_IXOTH)
    for current in (runtime_dir.parent.parent, runtime_dir.parent, runtime_dir):
        current.mkdir(parents=True, exist_ok=True)
        _chown_chmod_for_user(current, uid=uid, gid=gid, mode=stat.S_IRWXU)
    pane_log = Path(paths["pane_log"])
    pane_log.touch(exist_ok=True)
    _chown_chmod_for_user(pane_log, uid=uid, gid=gid, mode=stat.S_IRUSR | stat.S_IWUSR)


def _ensure_private_runtime_permissions(paths: dict[str, str]) -> None:
    runtime_dir = Path(paths["runtime_dir"])
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime_dir, stat.S_IRWXU)
        fd = os.open(paths["pane_log"], os.O_CREAT | os.O_APPEND, stat.S_IRUSR | stat.S_IWUSR)
        try:
            os.close(fd)
        except OSError:
            pass
        os.chmod(paths["pane_log"], stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise TmuxDriverError(f"failed to prepare private tmux runtime permissions: {exc}") from exc


class TmuxExecutionBackend:
    name = "tmux"

    def __init__(
        self,
        *,
        driver: Optional[TmuxDriver] = None,
        poll_interval_sec: float = 0.25,
        idle_fallback_sec: Optional[float] = None,
        startup_timeout_sec: float = 60.0,
        quiet_timeout_sec: float = 300.0,  # 5 min default: if pane.log stops growing, treat as finished (for long tasks without DONE marker)
    ):
        self.driver = driver
        self.poll_interval_sec = float(poll_interval_sec)
        self.idle_fallback_sec = idle_fallback_sec
        self.startup_timeout_sec = float(startup_timeout_sec)
        self.quiet_timeout_sec = float(quiet_timeout_sec)

    def _driver(self, session: Any) -> TmuxDriver:
        return self.driver or _driver_for_session(session)

    def paths(self, session: Any) -> dict[str, str]:
        return tmux_runtime_paths(session)

    @staticmethod
    def _pane_lock(session: Any) -> asyncio.Lock:
        lock = getattr(session, "_tmux_pane_lock", None)
        if isinstance(lock, asyncio.Lock):
            return lock
        lock = asyncio.Lock()
        setattr(session, "_tmux_pane_lock", lock)
        return lock

    @staticmethod
    def _has_active_runtime_state(session: Any, state: dict[str, Any]) -> bool:
        active_backend = str(getattr(session, "_active_execution_backend", "") or "").strip().lower()
        return bool(
            active_backend == "tmux"
            and str(state.get("state") or "").strip().lower() == "active"
            and str(state.get("active_request_id") or "").strip()
        )

    async def is_active(self, session: Any) -> bool:
        paths = self.paths(session)
        state = self._read_state(paths)
        if not self._has_active_runtime_state(session, state):
            return False
        return bool(await self._driver(session).has_session(paths["session_name"]))

    async def send_input(self, session: Any, text: str) -> None:
        prompt = str(text or "")
        if not prompt.strip():
            raise TmuxDriverError("cannot send empty input to active tmux session")

        paths = self.paths(session)
        driver = self._driver(session)
        async with self._pane_lock(session):
            if not await self.is_active(session):
                raise TmuxDriverError("active tmux session is unavailable")

            buffer_name = f"cli-proxy-steer-{_new_request_id()}"
            prompt_path = write_prompt_temp(paths["runtime_dir"], prompt, owner_user=getattr(driver, "user", None))
            buffer_loaded = False
            buffer_pasted = False
            try:
                await driver.load_buffer(prompt_path, buffer_name=buffer_name)
                buffer_loaded = True
                await driver.paste_buffer(paths["pane_target"], buffer_name=buffer_name, delete=True)
                buffer_pasted = True
                if self._uses_ready_wait(session):
                    await self._wait_for_pasted_text(session, paths, prompt, max_wait_sec=3.0)
                await driver.send_enter(paths["pane_target"])
            finally:
                if buffer_loaded and not buffer_pasted:
                    try:
                        await driver.delete_buffer(buffer_name=buffer_name)
                    except Exception:
                        logger.exception("failed to delete tmux steer buffer buffer=%s", buffer_name)
                try:
                    os.remove(prompt_path)
                except OSError:
                    logger.exception("failed to remove tmux steer prompt path=%s", prompt_path)

            state = self._read_state(paths)
            if self._has_active_runtime_state(session, state):
                state["last_activity_at"] = time.time()
                self._write_state(paths, state)

    async def _ensure_started(self, session: Any, paths: dict[str, str], *, force_fresh: bool = False) -> bool:
        driver = self._driver(session)
        Path(paths["runtime_dir"]).mkdir(parents=True, exist_ok=True)
        if getattr(driver, "user", None):
            _ensure_shared_runtime_permissions(paths, user=str(driver.user))
        else:
            _ensure_private_runtime_permissions(paths)
        state = self._read_state(paths)
        has_session = await driver.has_session(paths["session_name"])
        created_session = False
        if not has_session:
            await driver.new_session(
                paths["session_name"],
                workdir=str(getattr(session, "workdir", "") or os.getcwd()),
                command=_start_command(session, state, force_fresh=force_fresh),
            )
            created_session = True
        if created_session:
            await driver.pipe_pane(paths["pane_target"], paths["pane_log"])
        resume_token = _get_resume_token(session)
        cli_state = getattr(session, "cli", None)
        tmux_users = getattr(cli_state, "tmux_users", None)
        if isinstance(tmux_users, dict):
            tmux_users[_active_cli(session)] = str(getattr(driver, "user", "") or "").strip() or None
        self._write_state(
            paths,
            {
                "schema_version": 1,
                "backend": self.name,
                "session_name": paths["session_name"],
                "pane_target": paths["pane_target"],
                "session_runtime_uid": _session_uid(session),
                "active_cli": _active_cli(session),
                "workdir": str(getattr(session, "workdir", "") or ""),
                "tmux_user": str(getattr(driver, "user", "") or "").strip() or None,
                "resume_token": resume_token,
                "claude_resume_token": _legacy_claude_resume_token(session),
                "last_activity_at": time.time(),
                "state": "idle",
            },
        )
        return created_session

    @staticmethod
    def _interactive_cli_name(session: Any) -> str:
        command = [str(part) for part in (getattr(getattr(session, "tool", None), "interactive_cmd", None) or [])]
        return _command_cli_name(session, command)

    @classmethod
    def _uses_claude_code(cls, session: Any) -> bool:
        return cls._interactive_cli_name(session) == "claude"

    @classmethod
    def _uses_claude_screen_reader(cls, session: Any) -> bool:
        if not cls._uses_claude_code(session):
            return False
        tool = getattr(session, "tool", None)
        configured_commands = [
            *list(getattr(tool, "interactive_cmd", None) or []),
            *list(getattr(tool, "interactive_resume_cmd", None) or []),
        ]
        return "--ax-screen-reader" in configured_commands

    @classmethod
    def _uses_ready_wait(cls, session: Any) -> bool:
        return cls._interactive_cli_name(session) in _READY_WAIT_CLI_NAMES

    @classmethod
    def _uses_single_line_prompt(cls, session: Any) -> bool:
        return cls._interactive_cli_name(session) in _SINGLE_LINE_PROMPT_CLI_NAMES

    @staticmethod
    def _is_interactive_ready(session: Any, pane: str) -> bool:
        cli_name = TmuxExecutionBackend._interactive_cli_name(session)
        text = normalize_terminal_text(pane)
        lower = text.lower()
        compact = "".join(lower.split())
        if "doyoutrust" in compact:
            raise TmuxDriverError(
                f"{cli_name} workspace trust prompt is blocking tmux backend; "
                f"open {cli_name} once in this workdir and trust it"
            )
        if cli_name == "claude":
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            screen_reader_ready = bool(
                lines
                and lines[-1] == "$"
                and TmuxExecutionBackend._uses_claude_screen_reader(session)
            )
            return "❯" in text or screen_reader_ready
        if cli_name == "codex":
            if "starting mcp servers" in lower:
                return False
            return "›" in text or "❯" in text
        if cli_name == "qwen":
            if "инициализация" in lower or "initializing" in lower:
                return False
            return "введите сообщение" in lower or "enter message" in lower or "❯" in text
        if cli_name == "grok":
            return "❯" in text or ("grok build" in lower and "ctrl+c" in lower)
        return True

    async def _wait_for_interactive_ready(self, session: Any, paths: dict[str, str]) -> None:
        driver = self._driver(session)
        deadline = time.time() + max(0.1, self.startup_timeout_sec)
        last_pane = ""
        while time.time() < deadline:
            try:
                pane = await driver.capture_pane(paths["pane_target"])
                last_pane = pane
            except Exception:
                pane = ""
            if self._is_interactive_ready(session, pane):
                return
            await asyncio.sleep(min(self.poll_interval_sec, 0.25))
        snippet = (last_pane or "").strip()[-300:]
        raise TmuxDriverError(
            f"{self._interactive_cli_name(session)} interactive prompt did not become ready; last pane: {snippet!r}"
        )

    async def _wait_for_pasted_text(
        self,
        session: Any,
        paths: dict[str, str],
        text: str,
        *,
        max_wait_sec: float = 10.0,
    ) -> None:
        driver = self._driver(session)
        needle = "".join(normalize_terminal_text(text).split())[-160:]
        if not needle:
            return
        deadline = time.time() + min(max(0.5, self.startup_timeout_sec), max_wait_sec)
        while time.time() < deadline:
            try:
                pane = await driver.capture_pane(paths["pane_target"])
            except Exception:
                pane = ""
            if needle in "".join(normalize_terminal_text(pane).split()):
                await asyncio.sleep(min(max(self.poll_interval_sec, 0.1), 0.5))
                return
            await asyncio.sleep(min(self.poll_interval_sec, 0.25))

    @staticmethod
    def _read_state(paths: dict[str, str]) -> dict[str, Any]:
        try:
            with open(paths["state_path"], "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write_state(paths: dict[str, str], payload: dict[str, Any]) -> None:
        Path(paths["runtime_dir"]).mkdir(parents=True, exist_ok=True)
        tmp = f"{paths['state_path']}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, paths["state_path"])

    @staticmethod
    def _write_last_request(paths: dict[str, str], payload: dict[str, Any]) -> None:
        Path(paths["runtime_dir"]).mkdir(parents=True, exist_ok=True)
        tmp = f"{paths['last_request_path']}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, paths["last_request_path"])

    @staticmethod
    def _read_last_request(paths: dict[str, str]) -> dict[str, Any]:
        try:
            with open(paths["last_request_path"], "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.exception("failed to read tmux last request path=%s", paths["last_request_path"])
            return {}

    @staticmethod
    def _read_request_delta(paths: dict[str, str], offset: int) -> str:
        log_path = paths["pane_log"]
        if not os.path.exists(log_path):
            return ""
        size = os.path.getsize(log_path)
        safe_offset = max(0, int(offset))
        if safe_offset > size:
            safe_offset = 0
        with open(log_path, "rb") as handle:
            handle.seek(safe_offset)
            return handle.read().decode("utf-8", errors="replace")

    @classmethod
    def mark_request_delivered(cls, session: Any, request_id: str) -> bool:
        paths = tmux_runtime_paths(session)
        payload = cls._read_last_request(paths)
        if str(payload.get("request_id") or "").strip() != str(request_id or "").strip():
            return False
        payload.update({"delivery_state": "delivered", "delivered_at": time.time()})
        cls._write_last_request(paths, payload)
        return True

    @classmethod
    def _persist_transcript_locator(
        cls,
        paths: dict[str, str],
        request_id: str,
        locator: TranscriptLocator,
    ) -> bool:
        payload = cls._read_last_request(paths)
        if str(payload.get("request_id") or "").strip() != str(request_id or "").strip():
            return False
        payload.update(
            {
                "transcript_provider": locator.provider,
                "transcript_path": locator.path,
                "transcript_offset": locator.start_offset,
                "transcript_session_id": locator.session_id,
            }
        )
        cls._write_last_request(paths, payload)
        return True

    @staticmethod
    def _build_transcript_reader(
        session: Any,
        request: TmuxRecoveryRequest,
    ) -> Optional[CliTranscriptReader]:
        tool = getattr(session, "tool", None)
        return CliTranscriptReader(
            cli_name=_active_cli(session),
            request_id=request.request_id,
            workdir=str(getattr(session, "workdir", "") or ""),
            started_at=request.started_at,
            username=str(getattr(tool, "tmux_user", "") or ""),
            session_id=_get_resume_token(session) or "",
            locator=request.transcript_locator,
        )

    @staticmethod
    def _find_qwen_transcript_path(session: Any) -> Optional[str]:
        """Discover qwen session jsonl for the workdir, similar to QwenJsonlMonitor."""
        try:
            from app.services.qwen_jsonl_monitor import QWEN_CHAT_BASE_DIR
            workdir = str(getattr(session, "workdir", "") or "")
            if not workdir:
                return None
            raw = os.path.realpath(workdir).rstrip(os.sep) or workdir
            slash_key = raw.replace(os.sep, "-")
            compact_key = re.sub(r"[^A-Za-z0-9]+", "-", raw)
            if raw.startswith(os.sep) and not compact_key.startswith("-"):
                compact_key = "-" + compact_key
            compact_key = compact_key.rstrip("-")
            for project_key in (slash_key, compact_key):
                chat_dir = QWEN_CHAT_BASE_DIR / project_key / "chats"
                if chat_dir.exists():
                    jsonl_files = sorted(
                        chat_dir.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if jsonl_files:
                        return str(jsonl_files[0])
            return None
        except Exception:
            return None

    async def get_recovery_request(self, session: Any) -> Optional[TmuxRecoveryRequest]:
        paths = self.paths(session)
        state = self._read_state(paths)
        if not state:
            return None
        state_name = str(state.get("state") or "").strip().lower()
        active_request_id = str(state.get("active_request_id") or "").strip()
        payload = self._read_last_request(paths)
        payload_request_id = str(payload.get("request_id") or "").strip()
        driver = self._driver(session)
        if not await driver.has_session(paths["session_name"]):
            if state_name in {"active", "idle", "running"}:
                missing_state = "failed" if state_name == "active" else "stopped"
                state.update({"state": missing_state, "active_request_id": None, "last_activity_at": time.time()})
                self._write_state(paths, state)
            return None
        request_id = active_request_id if state_name == "active" and active_request_id else payload_request_id
        if not request_id:
            return None
        delivery_state = str(payload.get("delivery_state") or "").strip().lower()
        if delivery_state in {"delivered", "unmanaged"}:
            return None

        metadata_matches = not payload_request_id or payload_request_id == request_id
        try:
            started_at = float(payload.get("started_at") or state.get("last_activity_at") or time.time())
        except (TypeError, ValueError):
            started_at = time.time()
        try:
            offset = int(payload.get("offset") or 0) if metadata_matches else 0
        except (TypeError, ValueError):
            offset = 0
        prompt = str(payload.get("prompt") or "") if metadata_matches else ""
        raw_dest = payload.get("dest") if metadata_matches else None
        dest = dict(raw_dest) if isinstance(raw_dest, dict) else {}
        recovery = TmuxRecoveryRequest(
            request_id=request_id,
            started_at=started_at,
            offset=max(0, offset),
            prompt=prompt,
            dest=dest,
            transcript_locator=_transcript_locator_from_payload(payload) if metadata_matches else None,
        )
        if state_name == "active":
            return recovery
        if delivery_state != "pending":
            return None
        parsed = parse_tmux_delta(
            self._read_request_delta(paths, recovery.offset),
            recovery.request_id,
            claude_screen_reader=self._uses_claude_screen_reader(session),
        )
        if parsed.complete:
            return recovery
        try:
            transcript_reader = self._build_transcript_reader(session, recovery)
            transcript = await asyncio.to_thread(transcript_reader.poll) if transcript_reader is not None else None
        except Exception:
            logger.exception(
                "structured transcript recovery check failed session_id=%s request_id=%s",
                getattr(session, "id", "?"),
                recovery.request_id,
            )
            return None
        return recovery if transcript is not None and transcript.complete else None

    async def _handle_request_cancellation(self, session: Any, paths: dict[str, str]) -> None:
        if bool(getattr(session, "_preserve_tmux_on_shutdown", False)):
            logger.info(
                "tmux request monitor detached for shutdown session_id=%s request_id=%s",
                getattr(session, "id", "?"),
                self._read_state(paths).get("active_request_id"),
            )
            return
        sent = False
        try:
            sent = bool(await self._driver(session).send_ctrl_c(paths["pane_target"]))
        except Exception:
            logger.exception("failed to interrupt cancelled tmux request session_id=%s", getattr(session, "id", "?"))
        state = self._read_state(paths)
        state.update(
            {
                "state": "idle" if sent else "failed",
                "active_request_id": None,
                "last_activity_at": time.time(),
            }
        )
        self._write_state(paths, state)

    async def _monitor_request(
        self,
        session: Any,
        paths: dict[str, str],
        request: TmuxRecoveryRequest,
    ) -> ExecutionResult:
        idle_probe_interval = self.idle_fallback_sec
        if idle_probe_interval is None:
            idle_probe_interval = max(1.0, float(getattr(session, "idle_timeout_sec", 100) or 100))
        quiet_timeout = self.quiet_timeout_sec
        driver = self._driver(session)
        log_path = paths["pane_log"]
        last_size = request.offset
        last_change = time.time()
        pane_latest_text = ""
        transcript_latest_text = ""
        latest_text = ""
        last_reported_text = ""
        last_progress_text = ""
        complete = False
        completion_source = "pane"
        transcript_authoritative = False
        transcript_reader = None
        persisted_locator = request.transcript_locator
        last_transcript_size = 0
        transcript_path = None

        # Multi-transcript liveness tracking (main + Claude subagents).
        # Subagent JSONLs are written to <session>/subagents/*.jsonl and must
        # count for quiet_timeout to avoid dropping long-running planner tasks.
        watched_transcript_paths: list[str] = []
        last_transcript_sizes: dict[str, int] = {}

        def _refresh_watched_transcripts() -> bool:
            """Refresh list of watched transcripts. Returns True if any new file appeared."""
            nonlocal watched_transcript_paths, last_transcript_sizes, last_transcript_size
            current: list[str] = []
            if transcript_reader is not None:
                try:
                    current = list(transcript_reader.get_all_relevant_paths())
                except Exception:
                    current = []
            # Always include the primary transcript_path (qwen etc. may not go through reader locator)
            if transcript_path and transcript_path not in current:
                current.append(transcript_path)
            # Preserve any paths we were already watching (new subagents may appear)
            for old_p in list(last_transcript_sizes.keys()):
                if old_p not in current:
                    current.append(old_p)
            watched_transcript_paths = current

            new_activity = False
            for p in watched_transcript_paths:
                if p and os.path.exists(p):
                    try:
                        if p not in last_transcript_sizes:
                            last_transcript_sizes[p] = os.path.getsize(p)
                            new_activity = True  # new transcript file appeared = liveness signal
                        # keep legacy var in sync for the primary one
                        if p == transcript_path:
                            last_transcript_size = last_transcript_sizes[p]
                    except Exception:
                        pass
            return new_activity

        try:
            transcript_reader = self._build_transcript_reader(session, request)
            if transcript_reader is not None and getattr(transcript_reader, 'locator', None) and transcript_reader.locator:
                transcript_path = transcript_reader.locator.path
                if transcript_path and os.path.exists(transcript_path):
                    last_transcript_size = os.path.getsize(transcript_path)
            # For qwen (and any CLI without transcript_reader support), discover its jsonl
            if not transcript_path:
                cli_name = _active_cli(session)
                if cli_name == "qwen":
                    qwen_path = self._find_qwen_transcript_path(session)
                    if qwen_path and os.path.exists(qwen_path):
                        transcript_path = qwen_path
                        last_transcript_size = os.path.getsize(qwen_path)

            _refresh_watched_transcripts()
        except Exception:
            logger.exception(
                "failed to initialize structured transcript reader session_id=%s request_id=%s",
                getattr(session, "id", "?"),
                request.request_id,
            )
        try:
            while True:
                await asyncio.sleep(self.poll_interval_sec)
                size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
                if size != last_size:
                    last_size = size
                    last_change = time.time()

                # Track transcript JSONL growth for liveness (main + any Claude subagents).
                # Subagent .jsonl files under <sessid>/subagents/ are now considered.
                # This keeps the request alive while background planners / sub-agents write.
                if _refresh_watched_transcripts():
                    last_change = time.time()
                grew = False
                for tpath in watched_transcript_paths:
                    if tpath and os.path.exists(tpath):
                        try:
                            tsize = os.path.getsize(tpath)
                            prev = last_transcript_sizes.get(tpath, 0)
                            if tsize != prev:
                                last_transcript_sizes[tpath] = tsize
                                grew = True
                            # Also keep legacy single-value in sync for main path
                            if tpath == transcript_path:
                                last_transcript_size = tsize
                        except Exception:
                            pass
                if grew:
                    last_change = time.time()
                delta = self._read_request_delta(paths, request.offset)
                if delta:
                    _capture_resume_token_from_output(session, delta)
                parsed = parse_tmux_delta(
                    delta,
                    request.request_id,
                    claude_screen_reader=self._uses_claude_screen_reader(session),
                )
                pane_latest_text = parsed.text or pane_latest_text

                # Claude-specific: the choice question TUI (Enter selection, ☐ lists, esc prompts) lives in the
                # raw pane output. claude_screen_reader extraction + transcript preference can mask it.
                # Compute raw pane text (no claude extraction) for reliable choice detection.
                pane_raw_for_choice = pane_latest_text
                if self._uses_claude_screen_reader(session):
                    try:
                        raw_p = parse_tmux_delta(delta, request.request_id, claude_screen_reader=False)
                        pane_raw_for_choice = raw_p.text or pane_latest_text
                    except Exception:
                        pass

                transcript_update = None
                if transcript_reader is not None:
                    try:
                        transcript_update = await asyncio.to_thread(transcript_reader.poll)
                    except Exception:
                        logger.exception(
                            "structured transcript reader failed session_id=%s request_id=%s",
                            getattr(session, "id", "?"),
                            request.request_id,
                        )
                        transcript_reader = None
                if transcript_update is not None:
                    transcript_authoritative = transcript_authoritative or (
                        transcript_update.available and transcript_update.recognized
                    )
                    if transcript_update.assistant_text:
                        transcript_latest_text = transcript_update.assistant_text
                    if transcript_update.progress_text and transcript_update.progress_text != last_progress_text:
                        last_progress_text = transcript_update.progress_text
                        update_activity = getattr(session, "_update_activity", None)
                        if callable(update_activity):
                            try:
                                update_activity(last_progress_text)
                            except Exception:
                                logger.exception(
                                    "structured transcript progress update failed session_id=%s",
                                    getattr(session, "id", "?"),
                                )
                    if transcript_update.session_id and transcript_update.session_id != _get_resume_token(session):
                        _set_resume_token(session, transcript_update.session_id)
                        active_state = self._read_state(paths)
                        active_state.update(
                            {
                                "resume_token": _get_resume_token(session),
                                "claude_resume_token": _legacy_claude_resume_token(session),
                            }
                        )
                        self._write_state(paths, active_state)
                    if transcript_update.locator is not None and transcript_update.locator != persisted_locator:
                        if self._persist_transcript_locator(paths, request.request_id, transcript_update.locator):
                            persisted_locator = transcript_update.locator
                    # (re)check transcript path after poll in case it was discovered
                    if transcript_reader is not None and getattr(transcript_reader, 'locator', None) and transcript_reader.locator:
                        new_tpath = transcript_reader.locator.path
                        if new_tpath and new_tpath != transcript_path:
                            transcript_path = new_tpath
                            if os.path.exists(transcript_path):
                                last_transcript_size = os.path.getsize(transcript_path)
                    # Refresh subagent paths too (new subagents can appear mid-run)
                    if _refresh_watched_transcripts():
                        last_change = time.time()
                    if transcript_update.complete:
                        # Honor transcript complete only if the explicit DONE marker for this request is present.
                        # This prevents early termination on intermediate "turn end" / task_complete for long-running tasks.
                        # The DONE marker is the common signal across all CLIs (injected by build_prompt_with_markers).
                        dm = done_marker(request.request_id)
                        marker_seen = dm in (transcript_latest_text or "") or dm in (pane_latest_text or "")
                        if marker_seen:
                            complete = True
                            completion_source = "transcript"
                        else:
                            # Intermediate response complete in transcript, but no DONE yet -> keep monitoring
                            complete = False

                latest_text = transcript_latest_text or pane_latest_text
                # Choice question detection (for Claude and other CLIs):
                # - Raw pane (pre screen-reader extract) is checked because Claude TUI choice prompts
                #   (selection menus, esc prompts) are not inside "claude:" transcript events.
                # - If detected in pane, prefer that text for latest_text so preview + final send_output
                #   route the prompt to _is_cli... -> _send_ask_question (Telegram buttons).
                # - Force complete=False so monitoring continues until user feeds the choice via send_input.
                try:
                    # Local import avoids any potential circular at module load time.
                    from sessions.session_run_service import SessionRunService as _SRS
                    choice_in_pane = _SRS._is_cli_choice_question(pane_raw_for_choice)
                    choice_in_transcript = _SRS._is_cli_choice_question(transcript_latest_text or "")
                    if choice_in_pane:
                        latest_text = pane_raw_for_choice or latest_text
                    if choice_in_pane or choice_in_transcript:
                        complete = False
                except Exception:
                    # Fallback to legacy string check if import/func unavailable
                    if "Enter selection [" in (latest_text or "") or "or Escape to cancel" in (latest_text or "") or "☐ " in (latest_text or ""):
                        complete = False
                if latest_text and latest_text != last_reported_text:
                    last_reported_text = latest_text
                    setattr(session, "last_output_ts", time.time())
                    setattr(session, "last_assistant_text_ts", time.time())
                    setattr(session, "last_assistant_text_value", latest_text)
                if complete:
                    break
                if parsed.complete and not transcript_authoritative:
                    complete = True
                    completion_source = "pane"
                    break

                # Cross-CLI guard for long-running tasks:
                # Even if transcript or pane parser signals "complete" (e.g. turn end),
                # continue monitoring if we haven't seen the DONE marker yet
                # AND the pane does not look ready (ongoing work).
                if complete and not (done_marker(request.request_id) in (latest_text or "")):
                    try:
                        current_pane = await driver.capture_pane(paths["pane_target"])
                    except Exception:
                        current_pane = pane_latest_text or ""
                    if not self._is_interactive_ready(session, current_pane):
                        # still busy according to CLI's ready prompt detection
                        complete = False
                        # continue polling for more output / the real DONE
                if complete:
                    break

                # Quiet timeout: if neither pane.log nor (preferably) the session JSONL (incl. subagents) has grown
                # for quiet_timeout (default 5min), treat as finished. Growth of any related JSONL resets the timer.
                if time.time() - last_change >= quiet_timeout:
                    if not complete:
                        complete = True
                        has_json = bool(watched_transcript_paths or transcript_path)
                        completion_source = "json-quiet-timeout" if has_json else "pane-quiet-timeout"
                    break

                if time.time() - last_change >= idle_probe_interval:
                    if not await driver.has_session(paths["session_name"]):
                        logger.warning(
                            "tmux session disappeared while request was active session_id=%s request_id=%s",
                            getattr(session, "id", "?"),
                            request.request_id,
                        )
                        break
                    last_change = time.time()
        except asyncio.CancelledError:
            await self._handle_request_cancellation(session, paths)
            raise

        finished_at = time.time()
        state = self._read_state(paths)
        state.update(
            {
                "state": "idle" if complete else "failed",
                "active_request_id": None,
                "last_activity_at": finished_at,
                "resume_token": _get_resume_token(session),
                "claude_resume_token": _legacy_claude_resume_token(session),
            }
        )
        self._write_state(paths, state)
        return ExecutionResult(
            text=latest_text.strip(),
            backend=self.name,
            request_id=request.request_id,
            started_at=request.started_at,
            finished_at=finished_at,
            abnormal_stop=not complete,
            diagnostics={
                "session_name": paths["session_name"],
                "pane_target": paths["pane_target"],
                "completion_source": completion_source,
                "transcript_path": persisted_locator.path if persisted_locator is not None else "",
                "failure_reason": "" if complete else "tmux_session_missing",
            },
        )

    async def recover(self, session: Any, request: TmuxRecoveryRequest) -> ExecutionResult:
        paths = self.paths(session)
        if not await self._driver(session).has_session(paths["session_name"]):
            raise TmuxDriverError(f"tmux session disappeared during recovery: {paths['session_name']}")
        return await self._monitor_request(session, paths, request)

    async def run(
        self,
        session: Any,
        prompt: str,
        *,
        image_path: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
        force_fresh: bool = False,
        request_context: Optional[dict[str, Any]] = None,
    ) -> ExecutionResult:
        if image_path or image_paths:
            raise RuntimeError("tmux backend does not support image requests in v1")
        paths = self.paths(session)
        if force_fresh:
            await self.close(session)
        await self._ensure_started(session, paths, force_fresh=force_fresh)
        if self._uses_ready_wait(session):
            try:
                await self._wait_for_interactive_ready(session, paths)
            except Exception:
                failed_at = time.time()
                state = self._read_state(paths)
                state.update(
                    {
                        "state": "failed",
                        "active_request_id": None,
                        "last_activity_at": failed_at,
                    }
                )
                self._write_state(paths, state)
                raise

        request_id = _new_request_id()
        started_at = time.time()
        log_path = paths["pane_log"]
        offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        wrapped = build_prompt_with_markers(prompt, request_id, multiline=not self._uses_single_line_prompt(session))
        driver = self._driver(session)
        buffer_name = f"cli-proxy-{request_id}"
        prompt_path = write_prompt_temp(paths["runtime_dir"], wrapped, owner_user=getattr(driver, "user", None))
        state = self._read_state(paths)
        state.update(
            {
                "state": "active",
                "active_request_id": request_id,
                "last_activity_at": started_at,
            }
        )
        self._write_state(paths, state)
        self._write_last_request(
            paths,
            {
                "request_id": request_id,
                "started_at": started_at,
                "offset": offset,
                **_request_recovery_payload(request_context),
            },
        )

        buffer_loaded = False
        buffer_pasted = False
        try:
            async with self._pane_lock(session):
                await driver.load_buffer(prompt_path, buffer_name=buffer_name)
                buffer_loaded = True
                await driver.paste_buffer(paths["pane_target"], buffer_name=buffer_name, delete=True)
                buffer_pasted = True
                if self._uses_ready_wait(session):
                    await self._wait_for_pasted_text(session, paths, request_id)
                await driver.send_enter(paths["pane_target"])
        except asyncio.CancelledError:
            await self._handle_request_cancellation(session, paths)
            raise
        except Exception:
            failed_at = time.time()
            state = self._read_state(paths)
            state.update(
                {
                    "state": "failed",
                    "active_request_id": None,
                    "last_activity_at": failed_at,
                }
            )
            self._write_state(paths, state)
            raise
        finally:
            if buffer_loaded and not buffer_pasted:
                try:
                    await driver.delete_buffer(buffer_name=buffer_name)
                except Exception:
                    logger.exception("failed to delete tmux request buffer buffer=%s", buffer_name)
            try:
                os.remove(prompt_path)
            except OSError:
                pass
        return await self._monitor_request(
            session,
            paths,
            TmuxRecoveryRequest(
                request_id=request_id,
                started_at=started_at,
                offset=offset,
                prompt=str((request_context or {}).get("prompt") or ""),
                dest=dict((request_context or {}).get("dest") or {}),
            ),
        )

    async def interrupt(self, session: Any) -> bool:
        paths = self.paths(session)
        try:
            sent = await self._driver(session).send_ctrl_c(paths["pane_target"])
        except Exception:
            state = self._read_state(paths)
            if state:
                state.update({"state": "failed", "active_request_id": None, "last_activity_at": time.time()})
                self._write_state(paths, state)
            raise
        state = self._read_state(paths)
        if state:
            state.update(
                {
                    "state": "idle" if sent else "failed",
                    "active_request_id": None,
                    "last_activity_at": time.time(),
                }
            )
            self._write_state(paths, state)
        return sent

    async def close(self, session: Any) -> bool:
        paths = self.paths(session)
        existing_state = self._read_state(paths)
        driver = self._driver(session)
        killed = await driver.kill_session(paths["session_name"])
        if not killed and await driver.has_session(paths["session_name"]):
            raise TmuxDriverError(f"tmux session is still running after close: {paths['session_name']}")
        if not killed and not existing_state:
            return False
        state = existing_state or {
            "backend": self.name,
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
            "tmux_user": str(getattr(driver, "user", "") or "").strip() or None,
        }
        state.update({"state": "stopped", "active_request_id": None, "last_activity_at": time.time()})
        self._write_state(paths, state)
        return killed

    async def status(self, session: Any) -> ExecutionBackendStatus:
        paths = self.paths(session)
        state = self._read_state(paths)
        if not state:
            return ExecutionBackendStatus(
                backend=self.name,
                state="stopped",
                session_name=paths["session_name"],
                pane_target=paths["pane_target"],
                runtime_dir=paths["runtime_dir"],
            )
        tmux_state = str(state.get("state") or "unknown")
        if tmux_state not in {"running", "active", "idle", "failed", "stopped", "unknown"}:
            tmux_state = "unknown"
        return ExecutionBackendStatus(
            backend=self.name,
            state=tmux_state,  # type: ignore[arg-type]
            session_name=str(state.get("session_name") or paths["session_name"]),
            pane_target=str(state.get("pane_target") or paths["pane_target"]),
            last_activity_at=state.get("last_activity_at"),
            runtime_dir=paths["runtime_dir"],
        )
