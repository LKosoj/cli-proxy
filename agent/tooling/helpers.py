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
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from utils import strip_ansi
from .constants import (
    GREP_TIMEOUT_MS,
    OUTPUT_HEAD_LEN,
    OUTPUT_TAIL_LEN,
    OUTPUT_TRIM_LEN,
    TOOL_TIMEOUT_MS,
    WEB_FETCH_TIMEOUT_MS,
)

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
BLOCKED_PATTERNS_PATH = os.path.join(REPO_ROOT, "approvals", "blocked-patterns.json")

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


def set_approval_callback(cb: Callable[[int, str, str, str], None]) -> None:
    global _APPROVAL_CALLBACK
    _APPROVAL_CALLBACK = cb


def _store_pending_command(session_id: str, chat_id: int, command: str, cwd: str, reason: str) -> str:
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
    return cmd_id


def pop_pending_command(cmd_id: str) -> Optional[PendingCommand]:
    return _PENDING_COMMANDS.pop(cmd_id, None)


def _load_blocked_patterns() -> List[Dict[str, Any]]:
    try:
        with open(BLOCKED_PATTERNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        patterns = data.get("patterns", []) if isinstance(data, dict) else []
        return patterns
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return []


def check_command(command: str, chat_type: Optional[str]) -> Tuple[bool, bool, Optional[str]]:
    patterns = _load_blocked_patterns()
    command_lower = command.strip().lower()
    for p in patterns:
        try:
            if p.get("category") == "group_only" and chat_type != "group":
                continue
            regex = p.get("pattern")
            if regex and re.search(regex, command_lower, re.I):
                if p.get("blocked"):
                    return False, True, p.get("reason")
                return True, False, p.get("reason")
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            continue
    return False, False, None


def _check_workspace_isolation(command: str, user_workspace: str) -> Tuple[bool, Optional[str]]:
    if not user_workspace:
        return False, None
    forbidden = ["/root", "/etc", "/proc", "/sys", "/dev", "/var", "/boot", "/run"]
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()
    for p in parts:
        if p.startswith("/"):
            real = os.path.realpath(p)
            for f in forbidden:
                if real == f or real.startswith(f + "/"):
                    return True, f"BLOCKED: Path outside workspace: {real}"
    return False, None


def _check_command_path_escape(command: str, cwd: str) -> Tuple[bool, Optional[str]]:
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()
    for p in parts:
        if p.startswith("/"):
            real = os.path.realpath(p)
            root = os.path.realpath(cwd)
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
        completed = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=TOOL_TIMEOUT_MS / 1000,
        )
        output = completed.stdout or completed.stderr or ""
        sanitized = sanitize_output(output)
        trimmed = _trim_output(sanitized)
        if completed.returncode == 0:
            return {"success": True, "output": trimmed or "(empty output)"}
        return {"success": False, "error": f"Exit {completed.returncode}: {trimmed}"}
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
        for name, _ in providers:
            try:
                if name == "proxy":
                    r = requests.get(f"{proxy_url}/zai/search", params={"q": query}, timeout=timeout_sec)
                    if not r.ok:
                        raise RuntimeError(f"Proxy error: {r.status_code}")
                    results = (r.json() or {}).get("search_result", [])
                elif name == "tavily":
                    r = requests.post("https://api.tavily.com/search", json={"api_key": tavily_key, "query": query, "max_results": 5}, timeout=timeout_sec)
                    if not r.ok:
                        raise RuntimeError(f"Tavily error: {r.status_code}")
                    results = (r.json() or {}).get("results", [])
                elif name == "jina":
                    r = requests.get(
                        "https://s.jina.ai/",
                        params={"q": query},
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {jina_key}",
                            "X-Respond-With": "no-content",
                        },
                        timeout=timeout_sec,
                    )
                    if not r.ok:
                        raise RuntimeError(f"Jina search error: {r.status_code}")
                    data = r.json() or {}
                    results = data.get("data") or []
                elif name == "zai":
                    r = requests.post(
                        "https://api.z.ai/api/paas/v4/web_search",
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {zai_key}"},
                        json={"search_engine": "search-prime", "search_query": query, "count": 10},
                        timeout=timeout_sec,
                    )
                    if not r.ok:
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
            return {"success": False, "error": "🚫 BLOCKED: Cannot access metadata endpoints"}
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
            return {"success": False, "error": "No search API configured (PROXY_URL or TAVILY_API_KEY or JINA_API_KEY or ZAI_API_KEY)"}

        last_error: Optional[str] = None
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
                    return {"success": True, "output": output}
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
                    return {"success": True, "output": output}
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
                    return {"success": True, "output": content}
                if name == "zai":
                    r = requests.post(
                        "https://api.z.ai/api/paas/v4/reader",
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {zai_key}"},
                        json={"url": url, "return_format": "markdown", "retain_images": False, "timeout": int(WEB_FETCH_TIMEOUT_MS / 1000)},
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
                    return {"success": True, "output": output}
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                last_error = str(e)
                continue
        return {"success": False, "error": last_error or "Fetch failed"}
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {"success": False, "error": str(e)}


MEMORY_FILE = "MEMORY.md"
