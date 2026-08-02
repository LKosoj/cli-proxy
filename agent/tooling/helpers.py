from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import httpx
import requests

from utils.text import strip_ansi
from agent.tooling.command_scan import scannable_command
from modes.sdk.runtime.tooling.constants import (
    OUTPUT_HEAD_LEN,
    OUTPUT_TAIL_LEN,
    OUTPUT_TRIM_LEN,
    TOOL_TIMEOUT_MS,
    WEB_FETCH_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
BLOCKED_PATTERNS_PATH = os.path.join(REPO_ROOT, "approvals", "blocked-patterns.json")

if TYPE_CHECKING:
    from app.services.state_repository import JsonStateRepository

# ==== Approvals ====


@dataclass
class PendingCommand:
    cmd_id: str
    session_id: str
    chat_id: int
    command: str
    cwd: str
    reason: str
    created_at: float


_PENDING_COMMANDS: Dict[str, PendingCommand] = {}
_APPROVAL_CALLBACK: Optional[Callable[[int, str, str, str], None]] = None
_PENDING_STORE_PATH: Optional[str] = None
_PENDING_STORE_REPO: Optional["JsonStateRepository"] = None
_PENDING_STORE_LOADED: bool = False
_PENDING_COMMAND_WAITERS: Dict[str, asyncio.Future] = {}
_PENDING_COMMAND_DECISIONS: Dict[str, bool] = {}


def _normalize_block_reason(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("🚫"):
        text = text[1:].strip()
    if "BLOCKED:" in text:
        text = text.split("BLOCKED:", 1)[1].strip()
    return text or "blocked by policy"


def blocked_error(reason: str, *, error: Optional[str] = None) -> Dict[str, Any]:
    reason_text = _normalize_block_reason(reason)
    err_text = str(error).strip() if isinstance(error, str) and str(error).strip() else f"🚫 BLOCKED: {reason_text}"
    return {"success": False, "error": err_text, "blocked": True, "block_reason": reason_text}


def blocked_from_error(error: Optional[str]) -> Optional[Dict[str, Any]]:
    text = str(error or "").strip()
    if not text:
        return None
    if "BLOCKED:" in text or "not allowed for security reasons" in text.lower():
        return blocked_error(text, error=text)
    return None


def set_approval_callback(cb: Callable[[int, str, str, str], None]) -> None:
    global _APPROVAL_CALLBACK
    _APPROVAL_CALLBACK = cb


def configure_pending_commands_store(path: Optional[str]) -> None:
    global _PENDING_STORE_PATH, _PENDING_STORE_REPO, _PENDING_STORE_LOADED
    from app.services.path_normalization import normalize_optional_state_path
    from app.services.state_repository import get_state_repository

    try:
        normalized_path = normalize_optional_state_path(path)
    except TypeError:
        normalized_path = None
    _PENDING_STORE_PATH = normalized_path
    _PENDING_STORE_REPO = get_state_repository(normalized_path) if normalized_path else None
    _PENDING_STORE_LOADED = False
    _PENDING_COMMAND_WAITERS.clear()
    _PENDING_COMMAND_DECISIONS.clear()
    _ensure_pending_commands_loaded()


def _ensure_pending_commands_loaded() -> None:
    global _PENDING_STORE_LOADED
    if _PENDING_STORE_LOADED:
        return
    _PENDING_STORE_LOADED = True
    repo = _PENDING_STORE_REPO
    if repo is None:
        return
    try:
        pending = repo.load_pending_commands()
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return
    for cmd_id, payload in pending.items():
        if not isinstance(payload, dict):
            continue
        try:
            chat_id = int(payload.get("chat_id") or 0)
            created_at = float(payload.get("created_at") or 0.0)
            session_id = str(payload.get("session_id") or "").strip()
            command = str(payload.get("command") or "").strip()
            cwd = str(payload.get("cwd") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            if not cmd_id or not session_id or not command:
                continue
            _PENDING_COMMANDS[str(cmd_id)] = PendingCommand(
                cmd_id=str(cmd_id),
                session_id=session_id,
                chat_id=chat_id,
                command=command,
                cwd=cwd,
                reason=reason,
                created_at=created_at,
            )
        except Exception:
            logging.exception("tool failed pending command parse")


def _persist_pending_commands() -> None:
    repo = _PENDING_STORE_REPO
    if repo is None:
        return
    payload: Dict[str, Dict[str, Any]] = {
        cmd_id: {
            "cmd_id": cmd.cmd_id,
            "session_id": cmd.session_id,
            "chat_id": int(cmd.chat_id),
            "command": cmd.command,
            "cwd": cmd.cwd,
            "reason": cmd.reason,
            "created_at": float(cmd.created_at),
        }
        for cmd_id, cmd in _PENDING_COMMANDS.items()
    }
    try:
        repo.replace_pending_commands(payload)
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")


def _store_pending_command(session_id: str, chat_id: int, command: str, cwd: str, reason: str) -> str:
    _ensure_pending_commands_loaded()
    cmd_id = f"cmd_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    _PENDING_COMMANDS[cmd_id] = PendingCommand(
        cmd_id=cmd_id,
        session_id=session_id,
        chat_id=chat_id,
        command=command,
        cwd=cwd,
        reason=reason,
        created_at=time.time(),
    )
    _persist_pending_commands()
    return cmd_id


def pop_pending_command(cmd_id: str) -> Optional[PendingCommand]:
    _ensure_pending_commands_loaded()
    out = _PENDING_COMMANDS.pop(cmd_id, None)
    _persist_pending_commands()
    return out


def get_pending_command(cmd_id: str) -> Optional[PendingCommand]:
    _ensure_pending_commands_loaded()
    qid = str(cmd_id or "").strip()
    if not qid:
        return None
    return _PENDING_COMMANDS.get(qid)


def _resolve_pending_command_waiter(cmd_id: str, approved: bool) -> None:
    fut = _PENDING_COMMAND_WAITERS.pop(str(cmd_id or "").strip(), None)
    if fut is not None:
        if not fut.done():
            fut.set_result(bool(approved))
        return
    _PENDING_COMMAND_DECISIONS[str(cmd_id or "").strip()] = bool(approved)


def has_pending_command_waiter(cmd_id: str) -> bool:
    qid = str(cmd_id or "").strip()
    if not qid:
        return False
    fut = _PENDING_COMMAND_WAITERS.get(qid)
    return bool(fut is not None and not fut.done())


def ensure_pending_command_waiter(cmd_id: str) -> Optional[asyncio.Future]:
    qid = str(cmd_id or "").strip()
    if not qid:
        return None
    fut = _PENDING_COMMAND_WAITERS.get(qid)
    if fut is not None and not fut.done():
        return fut
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    fut = loop.create_future()
    _PENDING_COMMAND_WAITERS[qid] = fut
    if qid in _PENDING_COMMAND_DECISIONS:
        decision = bool(_PENDING_COMMAND_DECISIONS.pop(qid))
        if not fut.done():
            fut.set_result(decision)
    return fut


async def wait_pending_command_decision(cmd_id: str, *, timeout_sec: int = 1200) -> Optional[bool]:
    qid = str(cmd_id or "").strip()
    if not qid:
        return None
    if qid in _PENDING_COMMAND_DECISIONS:
        return bool(_PENDING_COMMAND_DECISIONS.pop(qid))
    fut = ensure_pending_command_waiter(qid)
    if fut is None:
        return None
    try:
        out = await asyncio.wait_for(asyncio.shield(fut), timeout=max(1, int(timeout_sec)))
        return bool(out)
    except asyncio.TimeoutError:
        return None
    finally:
        _PENDING_COMMAND_WAITERS.pop(qid, None)


def approve_pending_command(cmd_id: str) -> Optional[PendingCommand]:
    qid = str(cmd_id or "").strip()
    if not qid:
        return None
    pending = pop_pending_command(qid)
    if pending is not None:
        _resolve_pending_command_waiter(qid, True)
    return pending


def deny_pending_command(cmd_id: str) -> Optional[PendingCommand]:
    qid = str(cmd_id or "").strip()
    if not qid:
        return None
    pending = pop_pending_command(qid)
    if pending is not None:
        _resolve_pending_command_waiter(qid, False)
    return pending


def _load_blocked_patterns() -> List[Dict[str, Any]]:
    try:
        with open(BLOCKED_PATTERNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        patterns = data.get("patterns", []) if isinstance(data, dict) else []
        return patterns
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return []


_MAX_PATTERN_LEN = 256


@dataclass
class _CompiledPattern:
    id: str
    category: str
    regex: "re.Pattern[str]"
    reason: Optional[str]
    blocked: bool


_COMPILED_PATTERNS_CACHE: Optional[List[_CompiledPattern]] = None


def _compile_blocked_patterns() -> List[_CompiledPattern]:
    """Скомпилировать blocked-patterns.json один раз и закэшировать (чистое ускорение:
    раньше `re.search(str, ...)` компилировал паттерн заново на каждый вызов check_command).

    Кэш живёт до конца процесса: правка blocked-patterns.json на диске подхватывается
    только рестартом. Редактора паттернов в рантайме в проекте нет, поэтому сброс не нужен.
    """
    global _COMPILED_PATTERNS_CACHE
    if _COMPILED_PATTERNS_CACHE is not None:
        return _COMPILED_PATTERNS_CACHE
    compiled: List[_CompiledPattern] = []
    for p in _load_blocked_patterns():
        pattern_id = str(p.get("id") or "")
        raw_pattern = p.get("pattern")
        if not raw_pattern:
            continue
        if len(raw_pattern) > _MAX_PATTERN_LEN:
            logger.warning("blocked pattern %s skipped: longer than %d chars", pattern_id, _MAX_PATTERN_LEN)
            continue
        try:
            regex = re.compile(raw_pattern, re.I)
        except re.error:
            logger.exception("blocked pattern %s is not a valid regex, skipping", pattern_id)
            continue
        compiled.append(
            _CompiledPattern(
                id=pattern_id,
                category=str(p.get("category") or ""),
                regex=regex,
                reason=p.get("reason"),
                blocked=bool(p.get("blocked")),
            )
        )
    _COMPILED_PATTERNS_CACHE = compiled
    return compiled


def check_command(command: str, chat_type: Optional[str]) -> Tuple[bool, bool, Optional[str]]:
    """Проверить команду на соответствие политике безопасности (blocked-patterns.json).

    Прогоняет паттерны и по сырой строке, и (если нормализация дала другой текст) по
    развёрнутой через `agent.tooling.command_scan.scannable_command` версии — это закрывает
    обфускацию через кавычки, ANSI-C escape-последовательности, `bash -c`, `$(...)`, heredoc
    и т.п. Известные ограничения:
    * питоновский `re` не умеет таймаутить долгий матчинг сам по себе — от этого частично
      защищают лимиты длины входа (`MAX_SCAN_INPUT_CHARS`) и длины паттерна;
    * запись команды в файл с последующим запуском (`echo '...' > f; bash f`) не ловится:
      скрытый кавычками текст раскрывается только когда bash исполняет уже записанный файл,
      а сами запись и запуск легко разнести по двум вызовам инструмента. Это предел любого
      сканера одной командной строки, а не свойство нормализации.
    """
    patterns = _compile_blocked_patterns()
    raw = command.strip().lower()
    try:
        normalized = scannable_command(command.strip()).lower()
    except Exception:
        logger.exception("scannable_command failed for command, falling back to raw string")
        normalized = raw
    candidates: Tuple[str, ...] = (raw,) if normalized == raw else (raw, normalized)
    for p in patterns:
        if p.category == "group_only" and chat_type != "group":
            continue
        for candidate in candidates:
            try:
                matched = p.regex.search(candidate)
            except Exception:
                # Единственный барьер безопасности на каждую команду: сбой одного паттерна
                # не должен снимать проверку остальными.
                logger.exception("blocked pattern %s failed while matching, skipping", p.id)
                break
            if matched:
                if p.blocked:
                    return False, True, p.reason
                return True, False, p.reason
    return False, False, None


def _check_workspace_isolation(command: str, user_workspace: str) -> Tuple[bool, Optional[str]]:
    if not user_workspace:
        return False, None
    forbidden = ["/root", "/etc", "/proc", "/sys", "/dev", "/var", "/boot", "/run"]
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()
    root = os.path.realpath(user_workspace)
    for p in parts:
        if p.startswith("/") or ".." in p:
            real = os.path.realpath(os.path.join(root, p) if not os.path.isabs(p) else p)
            for f in forbidden:
                if real == f or real.startswith(f + "/"):
                    return True, f"BLOCKED: Path outside workspace: {real}"
    return False, None


def _check_command_path_escape(command: str, cwd: str) -> Tuple[bool, Optional[str]]:
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()
    root = os.path.realpath(cwd)
    for p in parts:
        if p.startswith("/") or ".." in p:
            real = os.path.realpath(os.path.join(cwd, p) if not os.path.isabs(p) else p)
            if not (real == root or real.startswith(root + os.sep)):
                return True, "BLOCKED: Command path escapes workspace"
    return False, None


def sanitize_output(output: str) -> str:
    return strip_ansi(output or "")


def _trim_output(text: str) -> str:
    if len(text) <= OUTPUT_TRIM_LEN:
        return text
    head = text[:OUTPUT_HEAD_LEN]
    tail = text[-OUTPUT_TAIL_LEN:]
    return f"{head}\n\n...(truncated {len(text) - OUTPUT_TRIM_LEN} chars)...\n\n{tail}"


FETCH_MAX_CHARS = 80_000


def _trim_fetch_output(text: str, *, reason: str = "превышен лимит") -> str:
    """Hard cap fetch outputs to avoid blowing up the next LLM turn."""
    if not text:
        return text
    if len(text) <= FETCH_MAX_CHARS:
        return text
    suffix = f"\n\n...(обрезано до {FETCH_MAX_CHARS} символов: {reason}, было {len(text)} символов)...\n"
    if len(suffix) + 50 >= FETCH_MAX_CHARS:
        return text[:FETCH_MAX_CHARS]
    return text[: FETCH_MAX_CHARS - len(suffix)] + suffix


async def execute_shell_command(command: str, cwd: str) -> Dict[str, Any]:
    if not command:
        return {"success": False, "error": "Command required"}
    try:
        if command.endswith(" &"):
            parts = command[:-2]
            with open(os.devnull, "w") as f:
                proc = subprocess.Popen(
                    parts,
                    shell=True,
                    cwd=cwd,
                    stdout=f,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            await asyncio.sleep(0.5)
            if proc.poll() is None:
                return {"success": True, "output": f"Started in background (PID: {proc.pid}). Check logs with: tail <logfile>"}
            return {"success": False, "error": f"Process started but died immediately (PID: {proc.pid}). Check the log file for errors!"}
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {"success": False, "error": f"Failed to start background process: {e}"}

    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=TOOL_TIMEOUT_MS / 1000,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        combined = stdout
        if stderr:
            combined = (combined + ("\n" if combined else "") + stderr) if combined is not None else stderr
        sanitized = sanitize_output(combined or "")
        trimmed = _trim_output(sanitized)
        if completed.returncode == 0:
            return {"success": True, "output": trimmed or "(empty output)"}
        if not trimmed.strip():
            # Provide enough context in logs/UI to understand what failed.
            return {
                "success": False,
                "error": f"Exit {completed.returncode}: (no output) command={command!r} cwd={cwd!r}",
                "meta": {
                    "returncode": completed.returncode, "cwd": cwd, "command": command,
                    "stdout_len": len(stdout), "stderr_len": len(stderr),
                },
            }
        return {
            "success": False,
            "error": f"Exit {completed.returncode}: {trimmed}",
            "meta": {
                "returncode": completed.returncode, "cwd": cwd, "command": command,
                "stdout_len": len(stdout), "stderr_len": len(stderr),
            },
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"⏱️ Tool run_command timed out after {int(TOOL_TIMEOUT_MS/1000)}s"}
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {"success": False, "error": f"Exit 1: {sanitize_output(str(e))}"}


# ==== File helpers ====
SENSITIVE_FILES = [
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.staging",
    "credentials.json",
    "credentials.yaml",
    "secrets.json",
    "secrets.yaml",
    ".secrets",
    "service-account.json",
    "serviceAccountKey.json",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".pem",
    ".key",
]

SENSITIVE_PATTERNS = [
    re.compile(r"\.env(\.[a-z]+)?$", re.I),
    re.compile(r"credentials?\.(json|yaml|yml)$", re.I),
    re.compile(r"secrets?\.(json|yaml|yml)$", re.I),
    re.compile(r"service.?account.*\.json$", re.I),
    re.compile(r"private.?key", re.I),
    re.compile(r"id_(rsa|dsa|ecdsa|ed25519)$", re.I),
    re.compile(r"\.(pem|key|p12|pfx)$", re.I),
]


def _resolve_within_workspace(path: str, cwd: str) -> Tuple[Optional[str], Optional[str]]:
    if not path:
        return None, "Path required"
    full_path = path
    if not os.path.isabs(full_path):
        full_path = os.path.join(cwd, path)
    full_path = os.path.realpath(full_path)
    root = os.path.realpath(cwd)
    if not (full_path == root or full_path.startswith(root + os.sep)):
        return None, "🚫 BLOCKED: Path escapes workspace"
    return full_path, None


def _is_sensitive_file(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name in SENSITIVE_FILES:
        return True
    for p in SENSITIVE_PATTERNS:
        if p.search(name):
            return True
    return False


def _is_other_user_workspace(path: str, workspace: str) -> bool:
    try:
        full = os.path.realpath(path)
        root = os.path.realpath(workspace)
        return not (full == root or full.startswith(root + os.sep))
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return False


def _is_symlink_escape(path: str, workspace: str) -> Tuple[bool, Optional[str]]:
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(workspace)
        if not (real == root or real.startswith(root + os.sep)):
            return True, "Path resolves outside workspace"
        return False, None
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return True, "Path resolution failed"


def _contains_dangerous_code(content: str) -> Tuple[bool, Optional[str]]:
    if not content:
        return False, None
    patterns = [
        r"OPENAI_API_KEY",
        r"AWS_SECRET_ACCESS_KEY",
        r"-----BEGIN [A-Z ]+PRIVATE KEY-----",
        r"password\s*="
    ]
    for p in patterns:
        if re.search(p, content, re.I):
            return True, p
    return False, None


def _format_tasks(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return "(no tasks)"
    lines = []
    for t in tasks:
        lines.append(f"- {t.get('id')}: {t.get('content')} [{t.get('status')}]")
    return "\n".join(lines)


# ==== Web helpers ====
async def search_web_impl(query: str, config: Any) -> Dict[str, Any]:
    if not query:
        return {"success": False, "error": "Query required"}
    proxy_url = os.getenv("PROXY_URL")
    zai_key = os.getenv("ZAI_API_KEY") or (config.defaults.zai_api_key if config else None)
    tavily_key = os.getenv("TAVILY_API_KEY") or (config.defaults.tavily_api_key if config else None)
    jina_key = os.getenv("JINA_API_KEY") or (config.defaults.jina_api_key if config else None)
    timeout_sec = int(WEB_FETCH_TIMEOUT_MS / 1000)
    try:
        providers: List[tuple[str, Any]] = []
        if proxy_url:
            providers.append(("proxy", "proxy"))
        if tavily_key:
            providers.append(("tavily", "tavily"))
        if jina_key:
            providers.append(("jina", "jina"))
        if zai_key:
            providers.append(("zai", "zai"))
        if not providers:
            logging.exception("tool failed: No search API configured (PROXY_URL or TAVILY_API_KEY or JINA_API_KEY or ZAI_API_KEY)")
            return {"success": False, "error": "No search API configured (PROXY_URL or TAVILY_API_KEY or JINA_API_KEY or ZAI_API_KEY)"}

        last_error: Optional[str] = None
        results = None
        async with httpx.AsyncClient() as client:
            for name, _ in providers:
                try:
                    if name == "proxy":
                        r = await client.get(f"{proxy_url}/zai/search", params={"q": query}, timeout=timeout_sec)
                        if r.status_code >= 400:
                            raise RuntimeError(f"Proxy error: {r.status_code}")
                        results = (r.json() or {}).get("search_result", [])
                    elif name == "tavily":
                        r = await client.post(
                            "https://api.tavily.com/search",
                            json={"api_key": tavily_key, "query": query, "max_results": 5},
                            timeout=timeout_sec,
                        )
                        if r.status_code >= 400:
                            raise RuntimeError(f"Tavily error: {r.status_code}")
                        results = (r.json() or {}).get("results", [])
                    elif name == "jina":
                        r = await client.get(
                            "https://s.jina.ai/",
                            params={"q": query},
                            headers={
                                "Accept": "application/json",
                                "Authorization": f"Bearer {jina_key}",
                                "X-Respond-With": "no-content",
                            },
                            timeout=timeout_sec,
                        )
                        if r.status_code >= 400:
                            raise RuntimeError(f"Jina search error: {r.status_code}")
                        data = r.json() or {}
                        results = data.get("data") or []
                    elif name == "zai":
                        r = await client.post(
                            "https://api.z.ai/api/paas/v4/web_search",
                            headers={"Content-Type": "application/json", "Authorization": f"Bearer {zai_key}"},
                            json={"search_engine": "search-prime", "search_query": query, "count": 10},
                            timeout=timeout_sec,
                        )
                        if r.status_code >= 400:
                            raise RuntimeError(f"Z.AI error: {r.status_code}")
                        results = (r.json() or {}).get("search_result", [])
                    break
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                    last_error = str(e)
                    results = None
                    continue

        if results is None:
            return {"success": False, "error": last_error or "Search failed"}

        if not results:
            return {"success": True, "output": "(no results)"}
        out_parts = []
        for i, r in enumerate(results):
            title = r.get("title") or ""
            url = r.get("link") or r.get("url")
            content = r.get("content") or r.get("description") or ""
            date = r.get("publish_date") or r.get("date")
            date_part = f" ({date})" if date else ""
            out_parts.append(f"[{i+1}] {title}{date_part}\n{url}\n{content}")
        return {"success": True, "output": "\n\n".join(out_parts)}
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {"success": False, "error": str(e)}


def _extract_html_title(html_content: str) -> str:
    """Извлекает заголовок из HTML-страницы."""
    try:
        from bs4 import BeautifulSoup as _BS
        soup = _BS(html_content, "html.parser")
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title.get("content").strip()
        h1_tag = soup.find("h1")
        if h1_tag:
            return h1_tag.get_text().strip()
    except Exception:
        pass
    return ""


def _clean_html_with_bs4(html_content: str) -> str:
    """Очищает HTML через BeautifulSoup и возвращает текст."""
    try:
        from bs4 import BeautifulSoup as _BS
        soup = _BS(html_content, "html.parser")
        for el in soup(["script", "style", "iframe", "noscript", "nav",
                        "footer", "header", "aside", "form", "button"]):
            el.decompose()
        for el in soup(["br", "p", "h1", "h2", "h3", "h4", "h5", "h6",
                        "ul", "ol", "li", "div", "table", "tr", "td", "th"]):
            el.append("\n")
        text = soup.get_text(separator="\n", strip=True)
        # убираем лишние пустые строки
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)
    except Exception:
        return ""


extract_html_title = _extract_html_title
clean_html_with_bs4 = _clean_html_with_bs4


def clean_extra_spaces(text: str) -> str:
    """Удаляет лишние пробелы и переносы строк в тексте."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


async def fetch_page_impl(url: str, config: Any) -> Dict[str, Any]:
    if not url:
        return {"success": False, "error": "URL required"}
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    blocked = [
        re.compile(r"^https?://169\.254\.169\.254", re.I),
        re.compile(r"^https?://metadata\.google\.internal", re.I),
        re.compile(r"^https?://metadata\.azure\.internal", re.I),
        re.compile(r"^https?://100\.100\.100\.200", re.I),
    ]
    for p in blocked:
        if p.search(url):
            return blocked_error("Cannot access metadata endpoints")

    timeout_sec = int(WEB_FETCH_TIMEOUT_MS / 1000)

    # ── Stage 1: прямой запрос через httpx (без API-ключей) ──────────
    direct_error: Optional[str] = None
    try:
        enhanced_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=enhanced_headers, timeout=timeout_sec)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()

            # --- PDF ---
            if url.lower().endswith(".pdf") or "application/pdf" in content_type:
                try:
                    from io import BytesIO
                    from pdfminer.high_level import extract_text  # type: ignore
                    pdf_text = extract_text(BytesIO(response.content))
                    if pdf_text and pdf_text.strip():
                        title = url.split("/")[-1] or "PDF"
                        output = f"# {title}\n\n{pdf_text.strip()}"
                        return {"success": True, "output": _trim_fetch_output(output, reason="fetch_page direct+pdf")}
                except ImportError:
                    logging.warning("pdfminer not installed, skipping PDF extraction")
                except Exception as e:
                    logging.warning(f"PDF extraction failed for {url}: {e}")

            html_text = response.text
            if not html_text or not html_text.strip():
                raise RuntimeError("Empty response body")

            title = _extract_html_title(html_text)

            # --- trafilatura (основной метод) ---
            try:
                import trafilatura  # type: ignore
                extracted = trafilatura.extract(
                    html_text,
                    include_formatting=True,
                    include_links=True,
                    include_tables=True,
                    include_images=True,
                    include_comments=False,
                    output_format="markdown",
                )
                if extracted and extracted.strip():
                    output = f"# {title}\n\n{extracted.strip()}" if title else extracted.strip()
                    logging.info(f"fetch_page OK via direct+trafilatura: {url}")
                    return {"success": True, "output": _trim_fetch_output(output, reason="fetch_page direct+trafilatura")}
            except ImportError:
                pass
            except Exception as e:
                logging.warning(f"trafilatura extraction failed for {url}: {e}")

            # --- BeautifulSoup fallback ---
            bs_text = _clean_html_with_bs4(html_text)
            if bs_text and bs_text.strip():
                output = f"# {title}\n\n{bs_text.strip()}" if title else bs_text.strip()
                logging.info(f"fetch_page OK via direct+bs4: {url}")
                return {"success": True, "output": _trim_fetch_output(output, reason="fetch_page direct+bs4")}

            # --- raw HTML как последний вариант прямого запроса ---
            logging.info(f"fetch_page OK via direct (raw html): {url}")
            return {"success": True, "output": _trim_fetch_output(html_text[:20000], reason="fetch_page direct")}

    except Exception as e:
        direct_error = str(e)
        logging.warning(f"Direct fetch failed for {url}: {e}")

    # ── Stage 2: API-провайдеры (fallback) ───────────────────────────
    proxy_url = os.getenv("PROXY_URL")
    zai_key = os.getenv("ZAI_API_KEY") or (config.defaults.zai_api_key if config else None)
    tavily_key = os.getenv("TAVILY_API_KEY") or (config.defaults.tavily_api_key if config else None)
    jina_key = os.getenv("JINA_API_KEY") or (config.defaults.jina_api_key if config else None)

    try:
        providers: List[tuple[str, Any]] = []
        if proxy_url:
            providers.append(("proxy", "proxy"))
        if tavily_key:
            providers.append(("tavily", "tavily"))
        if jina_key:
            providers.append(("jina", "jina"))
        if zai_key:
            providers.append(("zai", "zai"))
        if not providers:
            return {"success": False, "error": direct_error or "Direct fetch failed and no API providers configured"}

        last_error: Optional[str] = direct_error
        for name, _ in providers:
            try:
                if name == "proxy":
                    r = requests.get(f"{proxy_url}/zai/read", params={"url": url}, timeout=timeout_sec)
                    if not r.ok:
                        raise RuntimeError(f"Proxy error: {r.status_code}")
                    data = (r.json() or {}).get("reader_result") or {}
                    content = data.get("content")
                    if not content:
                        raise RuntimeError("No content returned")
                    title = data.get("title")
                    desc = data.get("description")
                    output = ""
                    if title:
                        output += f"# {title}\n\n"
                    if desc:
                        output += f"> {desc}\n\n"
                    output += content
                    return {"success": True, "output": _trim_fetch_output(output, reason="fetch_page proxy")}
                if name == "tavily":
                    r = requests.post(
                        "https://api.tavily.com/extract",
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tavily_key}"},
                        json={"urls": [url]},
                        timeout=timeout_sec,
                    )
                    if not r.ok:
                        raise RuntimeError(f"Tavily extract error: {r.status_code}")
                    data = r.json() or {}
                    results = data.get("results") or data.get("data") or []
                    if isinstance(results, dict):
                        results = [results]
                    item = results[0] if results else {}
                    content = item.get("content") or item.get("raw_content") or ""
                    if not content:
                        raise RuntimeError("No content returned")
                    title = item.get("title") or ""
                    output = f"# {title}\n\n{content}" if title else content
                    return {"success": True, "output": _trim_fetch_output(output, reason="fetch_page tavily")}
                if name == "jina":
                    r = requests.get(
                        f"https://r.jina.ai/{url}",
                        headers={"Authorization": f"Bearer {jina_key}"},
                        timeout=timeout_sec,
                    )
                    if not r.ok:
                        raise RuntimeError(f"Jina extract error: {r.status_code}")
                    content = r.text or ""
                    if not content.strip():
                        raise RuntimeError("No content returned")
                    return {"success": True, "output": _trim_fetch_output(content, reason="fetch_page jina")}
                if name == "zai":
                    r = requests.post(
                        "https://api.z.ai/api/paas/v4/reader",
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {zai_key}"},
                        json={"url": url, "format": "markdown", "keep_images": False, "timeout": int(WEB_FETCH_TIMEOUT_MS / 1000)},
                        timeout=timeout_sec,
                    )
                    if not r.ok:
                        raise RuntimeError(f"Z.AI Reader error: {r.status_code}")
                    data = (r.json() or {}).get("reader_result") or {}
                    content = data.get("content")
                    if not content:
                        raise RuntimeError("No content returned")
                    title = data.get("title")
                    desc = data.get("description")
                    output = ""
                    if title:
                        output += f"# {title}\n\n"
                    if desc:
                        output += f"> {desc}\n\n"
                    output += content
                    return {"success": True, "output": _trim_fetch_output(output, reason="fetch_page zai")}
            except Exception as e:
                msg = str(e)
                if isinstance(e, RuntimeError) and msg == "No content returned":
                    logging.warning("tool failed %s", msg)
                elif isinstance(e, RuntimeError) and any(
                    msg.startswith(p) for p in ("Tavily", "Proxy", "Jina", "Z.AI")
                ):
                    logging.warning("tool failed %s", msg)
                else:
                    logging.exception(f"tool failed {msg}")
                last_error = msg
                continue

        return {"success": False, "error": last_error or "All fetch methods failed"}
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {"success": False, "error": str(e)}


MEMORY_FILE = "MEMORY.md"
