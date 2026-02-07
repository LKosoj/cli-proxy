import asyncio
import os
import errno
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import logging
import pexpect
import subprocess
import shlex

from config import AppConfig, ToolConfig, save_config
from state import get_state, load_active_state, load_sessions, save_sessions, set_active_state, clear_active_state
from utils import build_command, detect_prompt_regex, detect_resume_regex, extract_tick_tokens, resolve_env_value, strip_ansi


@dataclass
class Session:
    id: str
    tool: ToolConfig
    workdir: str
    idle_timeout_sec: int
    config: AppConfig
    name: Optional[str] = None
    busy: bool = False
    queue: Deque[Dict[str, Any]] = field(default_factory=deque)
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    child: Optional[pexpect.spawn] = None
    current_proc: Optional[asyncio.subprocess.Process] = None
    _headless_interrupt_flag: bool = False
    resume_token: Optional[str] = None
    auto_commands_ran: bool = False
    started_at: Optional[float] = None
    last_output_ts: Optional[float] = None
    last_tick_ts: Optional[float] = None
    last_tick_value: Optional[str] = None
    tick_seen: int = 0
    git_busy: bool = False
    git_conflict: bool = False
    git_conflict_files: list[str] = field(default_factory=list)
    git_conflict_kind: Optional[str] = None
    agent_enabled: bool = False
    manager_enabled: bool = False
    agent_memory: Dict[str, Any] = field(default_factory=dict)
    project_root: Optional[str] = None
    # Per-session "state" (previously stored by tool+workdir). This avoids collisions when
    # multiple sessions share the same tool/workdir.
    state_summary: Optional[str] = None
    state_updated_at: Optional[float] = None
    headless_forced_stop: Optional[str] = None

    async def run_prompt(self, prompt: str, image_path: Optional[str] = None) -> str:
        if image_path:
            if not self.tool.image_cmd:
                raise RuntimeError(f"{self.tool.name} не поддерживает изображения")
            cmd_template = self.tool.resume_cmd or self.tool.headless_cmd or self.tool.cmd
            if self.tool.image_cmd:
                cmd_template = cmd_template + self.tool.image_cmd
            return await self._run_headless(prompt, cmd_template=cmd_template, image_path=image_path)
        if self.tool.mode == "headless":
            try:
                return await self._run_headless(prompt)
            except Exception:
                # fallback to interactive
                return await self._run_interactive(prompt)
        return await self._run_interactive(prompt)

    async def _run_headless(
        self,
        prompt: str,
        cmd_template: Optional[List[str]] = None,
        image_path: Optional[str] = None,
    ) -> str:
        _log = logging.getLogger("session.headless")
        self.headless_forced_stop = None
        if cmd_template is None:
            cmd_template = self.tool.headless_cmd or self.tool.cmd
            if self.resume_token and self.tool.resume_cmd:
                cmd_template = self.tool.resume_cmd
        cmd, use_stdin = build_command(cmd_template, prompt, self.resume_token, image=image_path)
        _log.info("[headless] START cmd=%s use_stdin=%s cwd=%s", cmd, use_stdin, self.workdir)
        env = os.environ.copy()
        if self.tool.env:
            for k, v in self.tool.env.items():
                if v is None:
                    continue
                env[k] = resolve_env_value(str(v))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.workdir,
            env=env,
            stdin=asyncio.subprocess.PIPE if use_stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.current_proc = proc
        self._headless_interrupt_flag = False
        _log.info("[headless] PID=%s started", proc.pid)
        if use_stdin and proc.stdin:
            proc.stdin.write((prompt + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            _log.info("[headless] stdin written and closed")

        out_buf = bytearray()
        drain_eof = False

        async def _drain_stdout() -> None:
            nonlocal drain_eof
            if not proc.stdout:
                _log.warning("[headless] no stdout pipe")
                return
            try:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        drain_eof = True
                        _log.info("[headless] drain: EOF received, total %d bytes", len(out_buf))
                        break
                    out_buf.extend(chunk)
                    try:
                        self._update_activity(chunk.decode(errors="ignore"))
                    except Exception:
                        pass
            except asyncio.CancelledError:
                _log.warning("[headless] drain: cancelled, had %d bytes", len(out_buf))
                raise
            except Exception:
                logging.exception("[headless] drain: exception")

        def _pid_exists(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except OSError as e:
                return e.errno != errno.ESRCH

        wait_task = asyncio.create_task(proc.wait())
        drain_task = asyncio.create_task(_drain_stdout())

        forced_reason: Optional[str] = None
        poll_iter = 0
        while True:
            poll_iter += 1
            done, _ = await asyncio.wait({wait_task}, timeout=2)
            if done:
                _log.info("[headless] wait_task done after %d polls, returncode=%s", poll_iter, proc.returncode)
                break
            pid_alive = _pid_exists(proc.pid) if proc.pid else False
            _log.info(
                "[headless] poll #%d: wait_task.done=%s pid_exists=%s returncode=%s drain_eof=%s buf=%d",
                poll_iter, wait_task.done(), pid_alive, proc.returncode, drain_eof, len(out_buf),
            )
            # Fallback: если asyncio не получил событие завершения, но PID уже отсутствует,
            # считаем процесс завершенным, чтобы не держать сессию busy бесконечно.
            if proc.pid and not pid_alive:
                forced_reason = "PID отсутствует, но ожидание завершения не сработало"
                _log.warning("[headless] %s (returncode=%s)", forced_reason, proc.returncode)
                break
            # Дополнительный fallback: returncode уже установлен (asyncio получил SIGCHLD),
            # но wait() не разрешился, потому что stdout pipe ещё открыт (asyncio ждёт
            # disconnected на всех pipe прежде чем resolve-ить wait future).
            if proc.returncode is not None:
                forced_reason = f"returncode={proc.returncode} есть, но wait() не завершился (stdout pipe удерживается)"
                _log.warning("[headless] %s", forced_reason)
                break

        # Даем короткий grace на дочитывание хвоста вывода. Если stdout не закрывается — прекращаем чтение.
        grace_sec = 2.0
        if not drain_task.done():
            _log.info("[headless] waiting for drain (grace %.1fs)...", grace_sec)
            try:
                await asyncio.wait_for(asyncio.shield(drain_task), timeout=grace_sec)
                _log.info("[headless] drain finished within grace")
            except asyncio.TimeoutError:
                forced_reason = forced_reason or "stdout не закрылся после завершения процесса"
                _log.warning("[headless] drain timed out: %s", forced_reason)
                try:
                    drain_task.cancel()
                except Exception:
                    pass
                # Закрываем наш read-end пайпа, чтобы не держать ресурсы.
                try:
                    transport = getattr(proc.stdout, "_transport", None) if proc.stdout else None
                    if transport is not None:
                        transport.close()
                except Exception:
                    pass
            except Exception:
                forced_reason = forced_reason or "ошибка при ожидании stdout после завершения процесса"
                _log.exception("[headless] drain grace exception")
                try:
                    drain_task.cancel()
                except Exception:
                    pass
        else:
            _log.info("[headless] drain already finished")

        if not wait_task.done():
            _log.warning("[headless] wait_task still not done, cancelling")
            try:
                wait_task.cancel()
            except Exception:
                pass

        self.current_proc = None
        self._headless_interrupt_flag = False
        text = bytes(out_buf).decode(errors="ignore")
        _log.info("[headless] END PID=%s forced=%s output_len=%d", proc.pid, forced_reason, len(text))
        if forced_reason:
            self.headless_forced_stop = forced_reason
            if not text:
                text = "⚠️ CLI завершился, но бот не смог корректно дочитать вывод (stdout не закрыт)."
        self._update_activity(text)
        self._maybe_update_resume(text)
        return text

    async def _run_interactive(self, prompt: str) -> str:
        return await asyncio.to_thread(self._run_interactive_sync, prompt)

    def _ensure_child(self) -> None:
        if self.child and self.child.isalive():
            return
        cmd_template = self.tool.interactive_cmd or self.tool.cmd
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
                pass
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
        self._maybe_update_resume(output)
        self._maybe_autoset_resume_regex(output)
        lines = output.splitlines()
        regex = detect_prompt_regex(lines)
        if regex:
            self.tool.prompt_regex = regex
            save_config(self.config)
        return output

    def interrupt(self) -> None:
        if self.tool.mode == "headless":
            if self.current_proc and self.current_proc.returncode is None:
                self._headless_interrupt_flag = True
                try:
                    os.killpg(self.current_proc.pid, signal.SIGINT)
                    os.killpg(self.current_proc.pid, signal.SIGTERM)
                    os.killpg(self.current_proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        self.current_proc.kill()
                    except Exception:
                        pass
            return
        if self.child and self.child.isalive():
            try:
                self.child.sendcontrol("c")
            except Exception:
                pass

    def close(self) -> None:
        if self.child and self.child.isalive():
            try:
                self.child.close(force=True)
            except Exception:
                pass

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
            self.tool.resume_regex = regex
            save_config(self.config)

    def _update_activity(self, text: str) -> None:
        now = time.time()
        self.last_output_ts = now
        tokens = extract_tick_tokens(text)
        if not tokens:
            return
        last = tokens[-1]
        if self.last_tick_value and last != self.last_tick_value:
            self.last_tick_ts = now
            self.tick_seen += 1
        self.last_tick_value = last

    def is_active_by_tick(self, now: Optional[float] = None, window_sec: int = 3) -> bool:
        if not self.last_tick_ts:
            return False
        now = time.time() if now is None else now
        return (now - self.last_tick_ts) <= window_sec

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
        self.sessions: Dict[str, Session] = {}
        self.active_session_id: Optional[str] = None
        self._counter = 0
        # Optional callback invoked whenever the active session changes
        # (create, switch, close).  Signature: callback() -> None
        self.on_session_change: Optional[Callable[[], None]] = None
        self._restore_sessions()

    def create(self, tool_name: str, workdir: str) -> Session:
        tool = self.config.tools[tool_name]
        self._counter += 1
        sid = f"s{self._counter}"
        session = Session(
            id=sid,
            tool=tool,
            workdir=workdir,
            idle_timeout_sec=self.config.defaults.idle_timeout_sec,
            config=self.config,
        )
        session.name = f"{tool.name}@{workdir}"
        # Do not load state by (tool, workdir): it is ambiguous when multiple sessions share them.
        self.sessions[sid] = session
        self.active_session_id = sid
        try:
            set_active_state(self.config.defaults.state_path, tool.name, workdir, session_id=sid)
        except Exception:
            pass
        self._persist_sessions()
        self._fire_session_change()
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def active(self) -> Optional[Session]:
        if not self.active_session_id:
            return None
        return self.sessions.get(self.active_session_id)

    def set_active(self, session_id: str) -> bool:
        if session_id in self.sessions:
            self.active_session_id = session_id
            session = self.sessions[session_id]
            try:
                set_active_state(self.config.defaults.state_path, session.tool.name, session.workdir, session_id=session_id)
            except Exception:
                pass
            self._persist_sessions()
            self._fire_session_change()
            return True
        return False

    def close(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if not session:
            return False
        session.close()
        if self.active_session_id == session_id:
            self.active_session_id = None
            try:
                clear_active_state(self.config.defaults.state_path)
            except Exception:
                pass
        self._persist_sessions()
        try:
            sessions = load_sessions(self.config.defaults.state_path)
            if session_id in sessions:
                del sessions[session_id]
                save_sessions(self.config.defaults.state_path, sessions)
        except Exception:
            pass
        self._fire_session_change()
        return True

    def _fire_session_change(self) -> None:
        """Invoke the on_session_change callback if registered."""
        cb = self.on_session_change
        if cb:
            try:
                cb()
            except Exception:
                logging.exception("on_session_change callback failed")

    def _persist_sessions(self) -> None:
        try:
            data: Dict[str, Any] = {}
            for sid, s in self.sessions.items():
                queue_items: list[Dict[str, Any]] = []
                for item in s.queue:
                    if isinstance(item, str):
                        queue_items.append({"text": item, "dest": {"kind": "telegram"}})
                    elif isinstance(item, dict):
                        text = item.get("text")
                        if not text:
                            continue
                        queue_items.append(item)
                data[sid] = {
                    "tool": s.tool.name,
                    "workdir": s.workdir,
                    "name": s.name,
                    "resume_token": s.resume_token,
                    "summary": getattr(s, "state_summary", None),
                    "updated_at": getattr(s, "state_updated_at", None),
                    "queue": queue_items,
                    "agent_enabled": bool(getattr(s, "agent_enabled", False)),
                    "manager_enabled": bool(getattr(s, "manager_enabled", False)),
                    "agent_memory": getattr(s, "agent_memory", {}),
                    "project_root": getattr(s, "project_root", None),
                }
            save_sessions(self.config.defaults.state_path, data)
        except Exception:
            pass

    def _restore_sessions(self) -> None:
        try:
            saved = load_sessions(self.config.defaults.state_path)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return
        max_id = 0
        for sid, val in saved.items():
            tool = val.get("tool")
            workdir = val.get("workdir")
            if not tool or tool not in self.config.tools or not workdir:
                continue
            session = Session(
                id=sid,
                tool=self.config.tools[tool],
                workdir=workdir,
                idle_timeout_sec=self.config.defaults.idle_timeout_sec,
                config=self.config,
            )
            session.name = val.get("name") or f"{tool}@{workdir}"
            session.resume_token = val.get("resume_token")
            session.state_summary = val.get("summary")
            try:
                session.state_updated_at = float(val.get("updated_at")) if val.get("updated_at") is not None else None
            except Exception:
                session.state_updated_at = None
            session.agent_enabled = bool(val.get("agent_enabled", False))
            session.manager_enabled = bool(val.get("manager_enabled", False))
            session.agent_memory = val.get("agent_memory", {}) or {}
            session.project_root = val.get("project_root")
            raw_queue = val.get("queue", [])
            queue_items: list[Dict[str, Any]] = []
            for item in raw_queue:
                if isinstance(item, str):
                    queue_items.append({"text": item, "dest": {"kind": "telegram"}})
                elif isinstance(item, dict):
                    text = item.get("text")
                    if not text:
                        continue
                    queue_items.append(item)
            session.queue = deque(queue_items)
            self.sessions[sid] = session
            if sid.startswith("s"):
                try:
                    max_id = max(max_id, int(sid[1:]))
                except Exception:
                    pass
        self._counter = max_id
        active = load_active_state(self.config.defaults.state_path)
        if active and active.session_id and active.session_id in self.sessions:
            self.active_session_id = active.session_id
        elif self.sessions:
            # fallback to most recent session id
            self.active_session_id = sorted(self.sessions.keys())[-1]


def run_tool_help(tool: ToolConfig, workdir: str, idle_timeout_sec: int) -> str:
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
            pass
        output = child.before
    try:
        child.close(force=True)
    except Exception:
        pass
    return output or "help не вернул данных."
