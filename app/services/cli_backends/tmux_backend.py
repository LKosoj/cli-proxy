from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from utils.cli import resolve_env_value
from utils.text import strip_ansi

from .models import ExecutionBackendStatus, ExecutionResult
from .tmux_driver import TmuxDriver, TmuxDriverError, resolve_user_identity, write_prompt_temp
from .tmux_parser import build_prompt_with_markers, normalize_terminal_text, parse_tmux_delta


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
}
_READY_WAIT_CLI_NAMES = {"claude", "codex", "qwen", "grok"}
_SINGLE_LINE_PROMPT_CLI_NAMES = {"codex", "qwen", "grok"}


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
        pass
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


def _prepare_interactive_command(session: Any, state: dict[str, Any], *, force_fresh: bool = False) -> list[str]:
    tool = getattr(session, "tool", None)
    command = [str(part) for part in (getattr(tool, "interactive_cmd", None) or [])]
    if not command:
        raise TmuxDriverError("interactive_cmd is required for tmux backend")
    if not force_fresh and not _get_resume_token(session):
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
        startup_timeout_sec: float = 30.0,
    ):
        self.driver = driver
        self.poll_interval_sec = float(poll_interval_sec)
        self.idle_fallback_sec = idle_fallback_sec
        self.startup_timeout_sec = float(startup_timeout_sec)

    def _driver(self, session: Any) -> TmuxDriver:
        return self.driver or _driver_for_session(session)

    def paths(self, session: Any) -> dict[str, str]:
        return tmux_runtime_paths(session)

    async def _ensure_started(self, session: Any, paths: dict[str, str], *, force_fresh: bool = False) -> bool:
        driver = self._driver(session)
        Path(paths["runtime_dir"]).mkdir(parents=True, exist_ok=True)
        if getattr(driver, "user", None):
            _ensure_shared_runtime_permissions(paths, user=str(driver.user))
        else:
            _ensure_private_runtime_permissions(paths)
        state = self._read_state(paths)
        has_session = await driver.has_session(paths["session_name"])
        state_name = str(state.get("state") or "").strip().lower()
        if has_session and state_name in {"", "active", "failed", "stopped", "unknown"}:
            await driver.kill_session(paths["session_name"])
            has_session = False
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
            return "❯" in text
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
        while time.time() < deadline:
            try:
                pane = await driver.capture_pane(paths["pane_target"])
            except Exception:
                pane = ""
            if self._is_interactive_ready(session, pane):
                return
            await asyncio.sleep(min(self.poll_interval_sec, 0.25))
        raise TmuxDriverError(f"{self._interactive_cli_name(session)} interactive prompt did not become ready")

    async def _wait_for_pasted_prompt(self, session: Any, paths: dict[str, str], request_id: str) -> None:
        driver = self._driver(session)
        deadline = time.time() + min(max(0.5, self.startup_timeout_sec), 10.0)
        while time.time() < deadline:
            try:
                pane = await driver.capture_pane(paths["pane_target"])
            except Exception:
                pane = ""
            if request_id in pane or f"DONE:{request_id}" in pane:
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
        with open(paths["last_request_path"], "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)

    async def run(
        self,
        session: Any,
        prompt: str,
        *,
        image_path: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
        force_fresh: bool = False,
    ) -> ExecutionResult:
        if image_path or image_paths:
            raise RuntimeError("tmux backend does not support image requests in v1")
        paths = self.paths(session)
        if force_fresh:
            await self.close(session)
        created_session = await self._ensure_started(session, paths, force_fresh=force_fresh)
        if created_session and self._uses_ready_wait(session):
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
        self._write_last_request(paths, {"request_id": request_id, "started_at": started_at, "offset": offset})

        buffer_loaded = False
        buffer_pasted = False
        try:
            await driver.load_buffer(prompt_path, buffer_name=buffer_name)
            buffer_loaded = True
            await driver.paste_buffer(paths["pane_target"], buffer_name=buffer_name, delete=True)
            buffer_pasted = True
            if self._uses_ready_wait(session):
                await self._wait_for_pasted_prompt(session, paths, request_id)
            await driver.send_enter(paths["pane_target"])
        except asyncio.CancelledError:
            sent = False
            try:
                sent = bool(await driver.send_ctrl_c(paths["pane_target"]))
            except Exception:
                sent = False
            cancelled_at = time.time()
            state = self._read_state(paths)
            state.update(
                {
                    "state": "idle" if sent else "failed",
                    "active_request_id": None,
                    "last_activity_at": cancelled_at,
                }
            )
            self._write_state(paths, state)
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
                    pass
            try:
                os.remove(prompt_path)
            except OSError:
                pass

        idle_timeout = self.idle_fallback_sec
        if idle_timeout is None:
            idle_timeout = max(1.0, float(getattr(session, "idle_timeout_sec", 100) or 100))
        last_size = offset
        last_change = time.time()
        latest_text = ""
        complete = False
        try:
            while True:
                await asyncio.sleep(self.poll_interval_sec)
                size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
                if size != last_size:
                    last_size = size
                    last_change = time.time()
                delta = ""
                if os.path.exists(log_path):
                    with open(log_path, "rb") as handle:
                        handle.seek(offset)
                        delta = handle.read().decode("utf-8", errors="replace")
                if delta:
                    _capture_resume_token_from_output(session, delta)
                parsed = parse_tmux_delta(delta, request_id)
                latest_text = parsed.text or latest_text
                if parsed.text:
                    setattr(session, "last_output_ts", time.time())
                    setattr(session, "last_assistant_text_ts", time.time())
                    setattr(session, "last_assistant_text_value", parsed.text[-1000:])
                if parsed.complete:
                    complete = True
                    break
                if time.time() - last_change >= idle_timeout:
                    try:
                        await driver.send_ctrl_c(paths["pane_target"])
                    except Exception:
                        pass
                    break
        except asyncio.CancelledError:
            sent = False
            try:
                sent = bool(await driver.send_ctrl_c(paths["pane_target"]))
            except Exception:
                sent = False
            cancelled_at = time.time()
            state = self._read_state(paths)
            state.update(
                {
                    "state": "idle" if sent else "failed",
                    "active_request_id": None,
                    "last_activity_at": cancelled_at,
                }
            )
            self._write_state(paths, state)
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
            request_id=request_id,
            started_at=started_at,
            finished_at=finished_at,
            abnormal_stop=not complete,
            diagnostics={"session_name": paths["session_name"], "pane_target": paths["pane_target"]},
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

    async def close(self, session: Any) -> None:
        paths = self.paths(session)
        await self._driver(session).kill_session(paths["session_name"])
        state = self._read_state(paths) or {
            "backend": self.name,
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
        }
        state.update({"state": "stopped", "active_request_id": None, "last_activity_at": time.time()})
        self._write_state(paths, state)

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
