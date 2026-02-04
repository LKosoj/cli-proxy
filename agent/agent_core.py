import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from openai import AsyncOpenAI

from config import AppConfig
from utils import strip_ansi

# ==== config constants ====
TOOL_TIMEOUT_MS = 120_000
GREP_TIMEOUT_MS = 30_000
WEB_FETCH_TIMEOUT_MS = 90_000
AGENT_MAX_ITERATIONS = 15
AGENT_MAX_HISTORY = 20
AGENT_MAX_BLOCKED = 3
OUTPUT_TRIM_LEN = 3000
OUTPUT_HEAD_LEN = 1500
OUTPUT_TAIL_LEN = 1000
MAX_CHAT_MESSAGES = 2500
MAX_MEMORY_CHARS = 2000
CHAT_MESSAGE_LEN = 200
LOG_DETAILS_LEN = 100

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
SYSTEM_PROMPT_PATH = os.path.join(REPO_ROOT, "agent", "system.txt")
BLOCKED_PATTERNS_PATH = os.path.join(REPO_ROOT, "agent", "approvals", "blocked-patterns.json")

# ==== OpenAI config ====

def _get_openai_config(config: Optional[AppConfig] = None) -> Optional[Tuple[str, str, str]]:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    base_url = os.getenv("OPENAI_BASE_URL")
    if config:
        api_key = api_key or config.defaults.openai_api_key
        model = model or config.defaults.openai_model
        base_url = base_url or config.defaults.openai_base_url
    if not base_url:
        base_url = "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


def _trim_output(text: str) -> str:
    if len(text) <= OUTPUT_TRIM_LEN:
        return text
    head = text[:OUTPUT_HEAD_LEN]
    tail = text[-OUTPUT_TAIL_LEN:]
    return f"{head}\n\n...(truncated {len(text) - OUTPUT_TRIM_LEN} chars)...\n\n{tail}"


# ==== Memory & chat history ====
MEMORY_FILE = "MEMORY.md"
SHARED_DIR = "/workspace/_shared"
CHATS_DIR = f"{SHARED_DIR}/chats"
GLOBAL_LOG_FILE = f"{SHARED_DIR}/GLOBAL_LOG.md"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _ensure_shared() -> None:
    _ensure_dir(SHARED_DIR)


def _ensure_chats() -> None:
    _ensure_shared()
    _ensure_dir(CHATS_DIR)


def _chat_history_file(chat_id: Optional[int]) -> str:
    if chat_id is None:
        return f"{CHATS_DIR}/chat_global.md"
    return f"{CHATS_DIR}/chat_{chat_id}.md"


def save_chat_message(username: str, text: str, is_bot: bool = False, chat_id: Optional[int] = None) -> None:
    try:
        _ensure_chats()
        timestamp = time.strftime("%H:%M")
        prefix = "🤖" if is_bot else "👤"
        clean_text = text[:CHAT_MESSAGE_LEN].replace("\n", " ")
        line = f"{timestamp} {prefix} {username}: {clean_text}\n"
        history_file = _chat_history_file(chat_id)
        content = ""
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        content += line
        lines = [l for l in content.split("\n") if l.strip()]
        if len(lines) > MAX_CHAT_MESSAGES:
            content = "\n".join(lines[-MAX_CHAT_MESSAGES:]) + "\n"
        with open(history_file, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        return


def get_chat_history(chat_id: Optional[int]) -> Optional[str]:
    try:
        history_file = _chat_history_file(chat_id)
        if not os.path.exists(history_file):
            return None
        with open(history_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content.strip()) < 20:
            return None
        return content
    except Exception:
        return None


def log_global(user_id: str, action: str, details: Optional[str] = None) -> None:
    try:
        _ensure_shared()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"| {timestamp} | {user_id} | {action} | {(details or '-')[:LOG_DETAILS_LEN]} |\n"
        if not os.path.exists(GLOBAL_LOG_FILE):
            header = "# Global Activity Log\n\n| Time | User | Action | Details |\n|------|------|--------|--------|\n"
            with open(GLOBAL_LOG_FILE, "w", encoding="utf-8") as f:
                f.write(header)
        with open(GLOBAL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        return


def get_memory_for_prompt(cwd: str) -> Optional[str]:
    path = os.path.join(cwd, MEMORY_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if not content.strip():
            return None
        if len(content) > MAX_MEMORY_CHARS:
            return content[-MAX_MEMORY_CHARS:]
        return content
    except Exception:
        return None


# ==== Approvals / blocked patterns ====
@dataclass
class PendingCommand:
    id: str
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
        id=cmd_id,
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


def _load_blocked_patterns() -> List[Tuple[re.Pattern, str]]:
    try:
        with open(BLOCKED_PATTERNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        patterns: List[Tuple[re.Pattern, str]] = []
        for item in data.get("patterns", []):
            flag_str = (item.get("flags") or "").lower()
            flags = 0
            if "i" in flag_str:
                flags |= re.IGNORECASE
            if "m" in flag_str:
                flags |= re.MULTILINE
            if "s" in flag_str:
                flags |= re.DOTALL
            pattern = re.compile(item["pattern"], flags)
            patterns.append((pattern, item.get("reason", "BLOCKED")))
        return patterns
    except Exception:
        return [
            (re.compile(r"\benv\b(?!\s*=)"), "BLOCKED: env command"),
            (re.compile(r"\bprintenv\b"), "BLOCKED: printenv command"),
            (re.compile(r"/proc/.*/environ"), "BLOCKED: proc environ"),
            (re.compile(r"/run/secrets"), "BLOCKED: Docker Secrets"),
            (re.compile(r"process\.env"), "BLOCKED: Node.js env"),
            (re.compile(r"os\.environ"), "BLOCKED: Python env"),
        ]


_BLOCKED_PATTERNS = _load_blocked_patterns()

_DANGEROUS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[rf]+\s+)*[\/~]"), "Recursive delete from root/home"),
    (re.compile(r"\brm\s+-[rf]*\s*\*"), "Wildcard delete"),
    (re.compile(r"\brm\s+-rf\b"), "Force recursive delete"),
    (re.compile(r"\brmdir\s+--ignore-fail-on-non-empty"), "Force directory removal"),
    (re.compile(r"\bsu\s+-?\s*$"), "Switch to root"),
    (re.compile(r"\bchown\s+-R\s+root"), "Change ownership to root"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*7[0-7]{2}\b"), "World-writable permissions"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\b"), "Full permissions to everyone"),
    (re.compile(r"\bchmod\s+\+s\b"), "Set SUID/SGID bit"),
    (re.compile(r"\bmkfs\b"), "Format filesystem"),
    (re.compile(r"\bdd\s+.*of=/dev/"), "Direct disk write"),
    (re.compile(r">\s*/dev/[sh]d[a-z]"), "Redirect to disk device"),
    (re.compile(r"\bfdisk\b"), "Partition manipulation"),
    (re.compile(r"\bparted\b"), "Partition manipulation"),
    (re.compile(r"\biptables\s+(-F|--flush)"), "Flush firewall rules"),
    (re.compile(r"\bufw\s+disable"), "Disable firewall"),
    (re.compile(r"\bsystemctl\s+(stop|disable)\s+(ssh|firewall|ufw)"), "Stop security service"),
    (re.compile(r"\bapt(-get)?\s+(remove|purge)\s+.*-y"), "Auto-confirm package removal"),
    (re.compile(r"\byum\s+remove\s+.*-y"), "Auto-confirm package removal"),
    (re.compile(r"\bpip\s+uninstall\s+.*-y"), "Auto-confirm pip uninstall"),
    (re.compile(r"\btruncate\s+-s\s*0"), "Truncate file to zero"),
    (re.compile(r">\s*/etc/"), "Overwrite system config"),
    (re.compile(r"\bshred\b"), "Secure file deletion"),
    (re.compile(r"\bshutdown\b"), "System shutdown"),
    (re.compile(r"\breboot\b"), "System reboot"),
    (re.compile(r"\binit\s+[06]\b"), "System halt/reboot"),
    (re.compile(r"curl.*\|\s*(ba)?sh"), "Pipe URL to shell"),
    (re.compile(r"wget.*\|\s*(ba)?sh"), "Pipe URL to shell"),
    (re.compile(r"\beval\s+\"?\$\(curl"), "Eval remote code"),
    (re.compile(r"\bgit\s+push\s+.*--force"), "Force push (rewrites history)"),
    (re.compile(r"\bgit\s+reset\s+--hard\s+HEAD~/"), "Hard reset (lose commits)"),
    (re.compile(r"\bgit\s+clean\s+-fd"), "Force clean untracked files"),
    (re.compile(r"\bDROP\s+(DATABASE|TABLE)\b", re.I), "Drop database/table"),
    (re.compile(r"\bTRUNCATE\s+TABLE\b", re.I), "Truncate table"),
    (re.compile(r"\bDELETE\s+FROM\s+\w+\s*;?\s*$", re.I), "Delete all rows (no WHERE)"),
    (re.compile(r"\bexport\s+(PATH|LD_PRELOAD|LD_LIBRARY_PATH)="), "Modify critical env var"),
    (re.compile(r"\bunset\s+(PATH|HOME)\b"), "Unset critical env var"),
    (re.compile(r":\(\)\s*{\s*:\|:&\s*}"), "Fork bomb"),
    (re.compile(r"while\s+true.*do.*done"), "Infinite loop"),
    (re.compile(r"\bfind\s+/\s"), "Full filesystem scan (very slow)"),
    (re.compile(r"\bdu\s+-[ash]*\s+/\s*$"), "Full disk usage scan"),
    (re.compile(r"\bls\s+-[laR]*\s+/\s*$"), "Full filesystem listing"),
    (re.compile(r"\bcat\s+/dev/port"), "Read port device (system freeze)"),
    (re.compile(r"\bmv\s+.*\s+/dev/null"), "Move files to black hole"),
    (re.compile(r">\s*/dev/sda"), "Overwrite disk"),
    (re.compile(r"\bperl\s+-e\s+.*fork"), "Fork bomb (perl)"),
    (re.compile(r"\bkubectl\s+delete\s+.*--all"), "Delete all K8s resources"),
    (re.compile(r"\bkubectl\s+apply\s+.*-f\s+-"), "Apply K8s from stdin"),
    (re.compile(r"\bdocker\s+rm\s+.*-f"), "Force remove containers"),
    (re.compile(r"\bdocker\s+system\s+prune\s+-a"), "Remove all Docker data"),
    (re.compile(r"\bnc\s+.*-e\s+/bin/(ba)?sh"), "Reverse shell"),
    (re.compile(r"\bbash\s+-i\s+.*/dev/tcp/"), "Reverse shell"),
]


def check_command(command: str, chat_type: Optional[str]) -> Tuple[bool, bool, Optional[str]]:
    is_group = chat_type in ("group", "supergroup")
    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(command):
            return True, True, reason
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            if is_group:
                return True, True, f"{reason} (напиши в личку для таких команд)"
            return True, False, reason
    return False, False, None


# ==== run_command sanitization ====
SECRET_PATTERNS = [
    re.compile(r"([A-Za-z0-9_]*(?:API[_-]?KEY|APIKEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL|AUTH)[A-Za-z0-9_]*)=([^\s\n]+)", re.I),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9-]{20,}"),
    re.compile(r"[a-f0-9]{32}\.[A-Za-z0-9]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    re.compile(r"gho_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{36,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{35}\b"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.I),
    re.compile(r"Basic\s+[A-Za-z0-9+/=]{20,}", re.I),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"[A-Za-z0-9/+=]{40}(?=\s|$|\")"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"),
    re.compile(r"(?:TELEGRAM_TOKEN|API_KEY|APIKEY|ZAI_API_KEY|TAVILY_API_KEY|BASE_URL|MCP_URL)=\S+", re.I),
    re.compile(r"https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+[^\s\"]*"),
    re.compile(r"\d{9,12}:AA[A-Za-z0-9_-]{30,}"),
]


def _contains_encoded_secrets(output: str) -> bool:
    for match in re.findall(r"[A-Za-z0-9+/]{100,}={0,2}", output):
        try:
            decoded = base64.b64decode(match).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if any(x in decoded for x in ["API_KEY", "TOKEN", "SECRET", "PASSWORD", "TELEGRAM", "process.env", "ZAI_", "BASE_URL", "MCP_", "WORKSPACE", "HOME", "PATH"]):
            return True
        if re.search(r"[a-f0-9]{32}\.[A-Za-z0-9]{10,}", decoded):
            return True
        if re.search(r"sk-[A-Za-z0-9_-]{15,}", decoded):
            return True
        if re.search(r"\d{9,12}:AA[A-Za-z0-9_-]{30,}", decoded):
            return True
        if re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+", decoded):
            return True
    return False


def _contains_env_dump(output: str) -> bool:
    env_key_count = len(re.findall(r"\"[A-Z_]{3,}\":", output))
    if env_key_count > 5:
        if any(x in output for x in ["\"API_KEY\"", "\"TOKEN\"", "\"SECRET\"", "\"ZAI_\"", "\"TELEGRAM\"", "\"BASE_URL\"", "\"MCP_\"", "\"WORKSPACE\"", "\"HOME\"", "\"PATH\""]):
            return True
    shell_env_count = len(re.findall(r"^[A-Z_]{3,}=.+$", output, re.M))
    if shell_env_count > 5:
        return True
    return False


def sanitize_output(output: str) -> str:
    if _contains_encoded_secrets(output):
        return "🚫 [OUTPUT BLOCKED: Contains encoded sensitive data]"
    if _contains_env_dump(output):
        return "🚫 [OUTPUT BLOCKED: Looks like environment dump]"
    sanitized = output
    for pattern in SECRET_PATTERNS:
        def repl(match: re.Match) -> str:
            text = match.group(0)
            if "=" in text:
                key = text.split("=")[0]
                return f"{key}=[REDACTED]"
            if len(text) > 10:
                return text[:4] + "***[REDACTED]***"
            return "[REDACTED]"
        sanitized = pattern.sub(repl, sanitized)
    return sanitized


def _check_workspace_isolation(command: str, user_workspace: str) -> Tuple[bool, Optional[str]]:
    match = re.search(r"/workspace/(\d+)", user_workspace)
    if not match:
        return False, None
    user_id = match.group(1)
    patterns = [
        re.compile(rf"/workspace/(?!{re.escape(user_id)})[\d_]", re.I),
        re.compile(r"/workspace/\*", re.I),
        re.compile(r"\b(find|ls|cat|head|tail|grep|less|more|tree|du|wc)\s+[^|]*/workspace\s*($|[|;>&\n])", re.I),
        re.compile(r"/workspace/_shared", re.I),
        re.compile(r"\.\./\.\.", re.I),
        re.compile(r"/workspace/\[", re.I),
        re.compile(r"/workspace/\{", re.I),
    ]
    for pattern in patterns:
        if pattern.search(command):
            return True, "BLOCKED: Cannot access other user workspaces. Use only your own workspace."
    return False, None


async def execute_shell_command(command: str, cwd: str) -> Dict[str, Any]:
    is_background = bool(re.search(r"&\s*$", command.strip())) or "nohup" in command
    if is_background:
        try:
            clean_cmd = re.sub(r"&\s*$", "", command.strip()).strip()
            proc = subprocess.Popen(
                ["sh", "-c", clean_cmd],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
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


# ==== Tool definitions ====

definitions: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command. Use for: git, npm, pip, system operations. DANGEROUS commands (rm -rf, sudo, etc.) require user approval.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to execute"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents. Always read before editing a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "offset": {"type": "number", "description": "Starting line number (1-based)"},
                    "limit": {"type": "number", "description": "Number of lines to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write/create files. Use to create new files or overwrite existing ones.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit a file by replacing text. The old_text must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "old_text": {"type": "string", "description": "Exact text to find and replace"},
                    "new_text": {"type": "string", "description": "New text to insert"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file. Only works within workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file to delete"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files by glob pattern. Use to discover project structure.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.ts, src/**/*.js)"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search for text/code in files using grep/ripgrep. Find definitions, usages, patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regex pattern to search"},
                    "path": {"type": "string", "description": "Directory or file to search in (default: current)"},
                    "context_before": {"type": "number", "description": "Lines to show before match (like grep -B)"},
                    "context_after": {"type": "number", "description": "Lines to show after match (like grep -A)"},
                    "files_only": {"type": "boolean", "description": "Return only file paths, not content"},
                    "ignore_case": {"type": "boolean", "description": "Case insensitive search"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List contents of a directory.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory path (default: current)"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the internet. USE IMMEDIATELY for: news, current events, external info, 'what is X?', prices, weather.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch and parse content from a URL. Returns clean markdown text.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "URL to fetch"}}, "required": ["url"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tasks",
            "description": "Manage task list: create, update status, or list all tasks. Use for planning complex multi-step work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update", "list", "clear"], "description": "Action: add new task, update status, list all, clear completed"},
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                            },
                        },
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask user a question with button options. Use when you need confirmation or choice from user. Returns the selected option.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask the user"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "Button options for user to choose from (2-4 options)"},
                },
                "required": ["question", "options"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory",
            "description": "Long-term memory. Use to save important info (project context, decisions, todos) or read previous notes. Memory persists across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "append", "clear"], "description": "read: get all memory, append: add new entry, clear: reset memory"},
                    "content": {"type": "string", "description": "For append: text to add (will be timestamped automatically)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Send a file from your workspace to the chat. Use this to share files you created or found with the user. Max file size: 50MB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file (relative to workspace or absolute)"},
                    "caption": {"type": "string", "description": "Optional caption/description for the file"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_message",
            "description": "Delete or edit your own recent messages. Use to fix typos, remove spam, or clean up. Can only manage YOUR OWN messages from this conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["delete_last", "delete_by_index", "edit_last"], "description": "Action: delete_last (delete your last message), delete_by_index (delete by index, 0=oldest), edit_last (edit your last message)"},
                    "index": {"type": "number", "description": "For delete_by_index: which message to delete (0=oldest recent, -1=newest)"},
                    "new_text": {"type": "string", "description": "For edit_last: the new text for the message"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {"name": "get_meme", "description": "Get a random meme.", "parameters": {"type": "object", "properties": {}}},
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a reminder or delayed command. Use for: 'remind me in 5 min', 'run this script in 1 hour'. Max delay: 24 hours. Max 5 tasks per user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "cancel"], "description": "add = create new task, list = show user's tasks, cancel = cancel task by id"},
                    "type": {"type": "string", "enum": ["message", "command"], "description": "message = send reminder text, command = execute shell command"},
                    "content": {"type": "string", "description": "For message: the reminder text. For command: the shell command to run."},
                    "delay_minutes": {"type": "number", "description": "Delay in minutes before execution (1-1440, i.e. max 24h)"},
                    "task_id": {"type": "string", "description": "Task ID (for cancel action)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_cli",
            "description": "Delegate a complex task to the selected CLI (codex/gemini/claude code). Use when the task is too complex for tools or requires full coding workflow.",
            "parameters": {
                "type": "object",
                "properties": {"task_text": {"type": "string", "description": "Task description for CLI"}},
                "required": ["task_text"],
            },
        },
    },
]

TOOL_NAMES = [d["function"]["name"] for d in definitions]


# ==== Tool execution ====
class ToolRegistry:
    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config
        self.pending_questions: Dict[str, asyncio.Future] = {}
        self.recent_messages: Dict[int, List[int]] = {}
        self.task_store: Dict[str, List[Dict[str, Any]]] = {}
        self.scheduler_tasks: Dict[str, Dict[str, Any]] = {}
        self.user_tasks: Dict[int, set] = {}

    def record_message(self, chat_id: int, message_id: int) -> None:
        if not chat_id or not message_id:
            return
        items = self.recent_messages.setdefault(chat_id, [])
        items.append(message_id)
        if len(items) > 20:
            del items[:-20]

    def resolve_question(self, question_id: str, answer: str) -> bool:
        fut = self.pending_questions.get(question_id)
        if not fut or fut.done():
            return False
        fut.set_result(answer)
        self.pending_questions.pop(question_id, None)
        return True

    async def execute(self, name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        # enforce timeout for each tool
        try:
            return await asyncio.wait_for(self._execute_internal(name, args, ctx), timeout=TOOL_TIMEOUT_MS / 1000)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"⏱️ Tool {name} timed out after {int(TOOL_TIMEOUT_MS/1000)}s"}

    async def _execute_internal(self, name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if name == "run_command":
            return await self._run_command(args, ctx)
        if name == "read_file":
            return await self._read_file(args, ctx)
        if name == "write_file":
            return await self._write_file(args, ctx)
        if name == "edit_file":
            return await self._edit_file(args, ctx)
        if name == "delete_file":
            return await self._delete_file(args, ctx)
        if name == "search_files":
            return await self._search_files(args, ctx)
        if name == "search_text":
            return await self._search_text(args, ctx)
        if name == "list_directory":
            return await self._list_directory(args, ctx)
        if name == "search_web":
            return await self._search_web(args, ctx)
        if name == "fetch_page":
            return await self._fetch_page(args, ctx)
        if name == "manage_tasks":
            return await self._manage_tasks(args, ctx)
        if name == "ask_user":
            return await self._ask_user(args, ctx)
        if name == "memory":
            return await self._memory(args, ctx)
        if name == "send_file":
            return await self._send_file(args, ctx)
        if name == "manage_message":
            return await self._manage_message(args, ctx)
        if name == "get_meme":
            return await self._get_meme(args, ctx)
        if name == "schedule_task":
            return await self._schedule_task(args, ctx)
        if name == "use_cli":
            return await self._use_cli(args, ctx)
        return {"success": False, "error": f"Unknown tool: {name}"}

    async def _run_command(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return {"success": False, "error": "Command required"}
        cwd = ctx["cwd"]
        session_id = ctx.get("session_id") or "default"
        chat_id = ctx.get("chat_id") or 0
        chat_type = ctx.get("chat_type")
        blocked_ws, reason_ws = _check_workspace_isolation(cmd, cwd)
        if blocked_ws:
            return {"success": False, "error": f"🚫 {reason_ws}"}
        dangerous, blocked, reason = check_command(cmd, chat_type)
        if blocked:
            return {"success": False, "error": f"🚫 {reason}\n\nThis command is not allowed for security reasons."}
        if dangerous:
            cmd_id = _store_pending_command(session_id, chat_id, cmd, cwd, reason or "Dangerous")
            if _APPROVAL_CALLBACK and chat_id:
                _APPROVAL_CALLBACK(chat_id, cmd_id, cmd, reason or "Dangerous")
            return {
                "success": False,
                "error": f"⚠️ APPROVAL REQUIRED: \"{reason}\"\n\nWaiting for user to click Approve/Deny button.",
                "approval_required": True,
            }
        return await execute_shell_command(cmd, cwd)

    async def _read_file(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        if not path:
            return {"success": False, "error": "Path required"}
        cwd = ctx["cwd"]
        full_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        if _is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        if _is_sensitive_file(full_path):
            return {"success": False, "error": f"🚫 BLOCKED: Cannot read sensitive file ({os.path.basename(full_path)})"}
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            offset = int(args.get("offset") or 1)
            limit = args.get("limit")
            if offset < 1:
                offset = 1
            start = offset - 1
            end = start + int(limit) if limit else None
            slice_lines = lines[start:end]
            content = "\n".join(slice_lines)
            return {"success": True, "output": content if content else "(empty file)"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}

    async def _write_file(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        content = args.get("content") or ""
        if not path:
            return {"success": False, "error": "Path required"}
        cwd = ctx["cwd"]
        full_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        if _is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        resolved = os.path.realpath(full_path)
        if not resolved.startswith(os.path.realpath(cwd)):
            return {"success": False, "error": "🚫 BLOCKED: Cannot write files outside workspace"}
        if _is_sensitive_file(full_path):
            return {"success": False, "error": f"🚫 BLOCKED: Cannot write to sensitive file ({os.path.basename(full_path)})"}
        symlink_check = _is_symlink_escape(full_path, cwd)
        if symlink_check[0]:
            return {"success": False, "error": f"🚫 BLOCKED: {symlink_check[1]}"}
        content_check = _contains_dangerous_code(content)
        if content_check[0]:
            return {"success": False, "error": f"🚫 BLOCKED: File contains dangerous code ({content_check[1]}). Cannot write files that may leak secrets."}
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "output": f"Written {len(content)} bytes to {path}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}

    async def _edit_file(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if not path or old_text is None or new_text is None:
            return {"success": False, "error": "path, old_text, new_text required"}
        cwd = ctx["cwd"]
        full_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        if _is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        if _is_sensitive_file(full_path):
            return {"success": False, "error": f"🚫 BLOCKED: Cannot edit sensitive file ({os.path.basename(full_path)})"}
        symlink_check = _is_symlink_escape(full_path, cwd)
        if symlink_check[0]:
            return {"success": False, "error": f"🚫 BLOCKED: {symlink_check[1]}"}
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}
        content_check = _contains_dangerous_code(new_text)
        if content_check[0]:
            return {"success": False, "error": f"🚫 BLOCKED: Edit contains dangerous code ({content_check[1]})."}
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if old_text not in content:
                preview = content[:2000]
                return {"success": False, "error": f"old_text not found.\n\nFile preview:\n{preview}"}
            new_content = content.replace(old_text, new_text)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {"success": True, "output": f"Edited {path}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}

    async def _delete_file(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        if not path:
            return {"success": False, "error": "Path required"}
        cwd = ctx["cwd"]
        full_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        resolved = os.path.realpath(full_path)
        if _is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        if not resolved.startswith(os.path.realpath(cwd)):
            return {"success": False, "error": "Security: cannot delete files outside workspace"}
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}
        try:
            os.remove(full_path)
            return {"success": True, "output": f"Deleted: {path}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}

    async def _search_files(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return {"success": False, "error": "Pattern required"}
        cwd = ctx["cwd"]
        try:
            import glob
            matches = glob.glob(os.path.join(cwd, pattern), recursive=True)
            files: List[str] = []
            for p in matches:
                rel = os.path.relpath(p, cwd)
                if "/node_modules/" in rel or rel.startswith("node_modules/"):
                    continue
                if "/.git/" in rel or rel.startswith(".git/"):
                    continue
                if os.path.isdir(p):
                    continue
                files.append(rel)
                if len(files) >= 200:
                    break
            return {"success": True, "output": "\n".join(files) or "(no matches)"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}

    async def _search_text(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return {"success": False, "error": "Pattern required"}
        if re.search(r"password|secret|token|api.?key|credential|private.?key", pattern, re.I):
            return {"success": False, "error": "🚫 BLOCKED: Cannot search for secrets/credentials patterns"}
        cwd = ctx["cwd"]
        search_path = args.get("path") or cwd
        if not os.path.isabs(search_path):
            search_path = os.path.join(cwd, search_path)
        flags = ["-rn"]
        if args.get("ignore_case"):
            flags.append("-i")
        if args.get("files_only"):
            flags.append("-l")
        if args.get("context_before"):
            flags.append(f"-B{int(args.get('context_before'))}")
        if args.get("context_after"):
            flags.append(f"-A{int(args.get('context_after'))}")
        flags += ["--exclude-dir=node_modules", "--exclude-dir=.git", "--exclude-dir=dist"]
        flags += ["--exclude=*.env*", "--exclude=*credentials*", "--exclude=*secret*", "--exclude=*.pem", "--exclude=*.key", "--exclude=id_rsa*"]
        escaped = pattern.replace('"', '\\"')
        cmd = f"grep {' '.join(flags)} \"{escaped}\" \"{search_path}\" 2>/dev/null | head -200"
        try:
            completed = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", timeout=GREP_TIMEOUT_MS / 1000)
            output = completed.stdout or ""
            return {"success": True, "output": output or "(no matches)"}
        except Exception:
            return {"success": True, "output": "(no matches)"}

    async def _list_directory(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        cwd = ctx["cwd"]
        path = args.get("path")
        dir_path = path if path else cwd
        if not os.path.isabs(dir_path):
            dir_path = os.path.join(cwd, dir_path)
        if _is_other_user_workspace(dir_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        blocked_dirs = ["/etc", "/root", "/.ssh", "/proc", "/sys", "/dev", "/boot", "/var/log", "/var/run"]
        resolved = os.path.realpath(dir_path).lower()
        for b in blocked_dirs:
            if resolved == b or resolved.startswith(b + "/"):
                return {"success": False, "error": f"🚫 BLOCKED: Cannot list directory {b} for security reasons"}
        if "/.ssh" in resolved:
            return {"success": False, "error": "🚫 BLOCKED: Cannot list .ssh directory"}
        try:
            completed = subprocess.run(f"ls -la \"{dir_path}\"", shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
            if completed.returncode == 0:
                return {"success": True, "output": completed.stdout}
            return {"success": False, "error": completed.stderr or "list failed"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}

    async def _search_web(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "Query required"}
        proxy_url = os.getenv("PROXY_URL")
        zai_key = os.getenv("ZAI_API_KEY") or (self.config.defaults.zai_api_key if self.config else None)
        tavily_key = os.getenv("TAVILY_API_KEY") or (self.config.defaults.tavily_api_key if self.config else None)
        jina_key = os.getenv("JINA_API_KEY") or (self.config.defaults.jina_api_key if self.config else None)
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

    async def _fetch_page(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        url = (args.get("url") or "").strip()
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
        zai_key = os.getenv("ZAI_API_KEY") or (self.config.defaults.zai_api_key if self.config else None)
        tavily_key = os.getenv("TAVILY_API_KEY") or (self.config.defaults.tavily_api_key if self.config else None)
        jina_key = os.getenv("JINA_API_KEY") or (self.config.defaults.jina_api_key if self.config else None)
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

    async def _manage_tasks(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        session_id = ctx.get("session_id") or "default"
        tasks = self.task_store.setdefault(session_id, [])
        action = args.get("action")
        if action == "add":
            items = args.get("tasks") or []
            if not items:
                return {"success": False, "error": "No tasks provided"}
            for t in items:
                if not t.get("id") or not t.get("content"):
                    return {"success": False, "error": "Task requires id and content"}
                existing = next((x for x in tasks if x["id"] == t["id"]), None)
                if existing:
                    if t.get("content"):
                        existing["content"] = t["content"]
                    if t.get("status"):
                        existing["status"] = t["status"]
                else:
                    tasks.append({"id": t["id"], "content": t["content"], "status": t.get("status", "pending"), "created_at": int(time.time() * 1000)})
            return {"success": True, "output": _format_tasks(tasks)}
        if action == "update":
            items = args.get("tasks") or []
            if not items:
                return {"success": False, "error": "No tasks provided"}
            for t in items:
                existing = next((x for x in tasks if x["id"] == t.get("id")), None)
                if existing:
                    if t.get("content"):
                        existing["content"] = t["content"]
                    if t.get("status"):
                        existing["status"] = t["status"]
            return {"success": True, "output": _format_tasks(tasks)}
        if action == "list":
            return {"success": True, "output": _format_tasks(tasks)}
        if action == "clear":
            active = [t for t in tasks if t.get("status") not in ("completed", "cancelled")]
            self.task_store[session_id] = active
            return {"success": True, "output": f"Cleared completed tasks. {len(active)} remaining."}
        return {"success": False, "error": f"Unknown action: {action}"}

    async def _ask_user(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        question = args.get("question")
        options = args.get("options") or []
        if not question or len(options) < 2:
            return {"success": False, "error": "Need at least 2 options"}
        if len(options) > 4:
            options = options[:4]
        bot = ctx.get("bot")
        context = ctx.get("context")
        chat_id = ctx.get("chat_id")
        session_id = ctx.get("session_id") or "default"
        if not bot or not context or not chat_id:
            return {"success": False, "error": "Ask callback not configured"}
        question_id = f"ask_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[question_id] = fut
        await bot._send_ask_question(context, chat_id, session_id, question_id, question, options)
        try:
            answer = await asyncio.wait_for(fut, timeout=120)
            return {"success": True, "output": f"User selected: {answer}"}
        except asyncio.TimeoutError:
            self.pending_questions.pop(question_id, None)
            return {"success": False, "error": "Failed to get user response: timeout"}

    async def _memory(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        cwd = ctx["cwd"]
        path = os.path.join(cwd, MEMORY_FILE)
        if action == "read":
            if not os.path.exists(path):
                return {"success": True, "output": "(memory is empty)"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"success": True, "output": content or "(memory is empty)"}
        if action == "append":
            content = args.get("content")
            if not content:
                return {"success": False, "error": "Content required for append"}
            timestamp = time.strftime("%Y-%m-%d %H:%M")
            entry = f"- {timestamp}: {content.strip()}\n"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry)
            return {"success": True, "output": "Memory updated"}
        if action == "clear":
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("")
            return {"success": True, "output": "Memory cleared"}
        return {"success": False, "error": f"Unknown action: {action}"}

    async def _send_file(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        if not path:
            return {"success": False, "error": "Path required"}
        cwd = ctx["cwd"]
        chat_id = ctx.get("chat_id") or 0
        bot = ctx.get("bot")
        context = ctx.get("context")
        if not bot or not context:
            return {"success": False, "error": "Send file callback not configured"}
        full_path = path if os.path.isabs(path) else os.path.join(cwd, path)
        resolved = os.path.realpath(full_path)
        cwd_resolved = os.path.realpath(cwd)
        if not resolved.startswith(cwd_resolved) and not resolved.startswith("/workspace/"):
            return {"success": False, "error": "🚫 BLOCKED: Can only send files from your workspace"}
        filename = os.path.basename(resolved).lower()
        blocked = [".env", "credentials", "secrets", "password", "token", ".pem", "id_rsa", "id_ed25519", ".key", "serviceaccount"]
        for b in blocked:
            if b in filename or b in resolved.lower():
                return {"success": False, "error": "🚫 BLOCKED: Cannot send sensitive files (credentials, keys, etc)"}
        if not os.path.exists(resolved):
            return {"success": False, "error": f"File not found: {path}"}
        size = os.path.getsize(resolved)
        if size > 50 * 1024 * 1024:
            return {"success": False, "error": f"File too large ({round(size/1024/1024)}MB). Max: 50MB"}
        if size == 0:
            return {"success": False, "error": "File is empty"}
        try:
            caption = args.get("caption")
            with open(resolved, "rb") as f:
                await bot._send_document(context, chat_id=chat_id, document=f, caption=caption)
            return {"success": True, "output": f"Sent file: {os.path.basename(resolved)}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            msg = str(e)
            if "not enough rights" in msg or "CHAT_SEND_MEDIA_FORBIDDEN" in msg:
                return {"success": False, "error": "Cannot send files in this group (no permissions). Try: read the file and paste contents, or tell user to DM for files."}
            return {"success": False, "error": f"Failed to send file: {msg}"}

    async def _manage_message(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        chat_id = ctx.get("chat_id")
        bot = ctx.get("bot")
        context = ctx.get("context")
        if not chat_id or not bot or not context:
            return {"success": False, "error": "Manage message not configured"}
        messages = self.recent_messages.get(chat_id, [])
        if not messages:
            return {"success": False, "error": "No recent messages to manage"}
        action = args.get("action")
        if action == "delete_last":
            msg_id = messages[-1]
            try:
                ok = await bot._delete_message(context, chat_id, msg_id)
                if ok:
                    messages.pop()
                    self.recent_messages[chat_id] = messages
                    return {"success": True, "output": "Deleted last message"}
                return {"success": False, "error": "Failed to delete (maybe already deleted or too old)"}
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"Delete failed: {e}"}
        if action == "delete_by_index":
            idx = args.get("index", -1)
            if idx < 0:
                idx = len(messages) + idx
            if idx < 0 or idx >= len(messages):
                return {"success": False, "error": f"Invalid index. Have {len(messages)} messages (0-{len(messages)-1})"}
            msg_id = messages[idx]
            try:
                ok = await bot._delete_message(context, chat_id, msg_id)
                if ok:
                    messages.pop(idx)
                    self.recent_messages[chat_id] = messages
                    return {"success": True, "output": f"Deleted message at index {idx}"}
                return {"success": False, "error": "Failed to delete"}
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"Delete failed: {e}"}
        if action == "edit_last":
            new_text = args.get("new_text")
            if not new_text:
                return {"success": False, "error": "new_text required for edit"}
            msg_id = messages[-1]
            try:
                ok = await bot._edit_message(context, chat_id, msg_id, new_text)
                if ok:
                    return {"success": True, "output": "Edited last message"}
                return {"success": False, "error": "Failed to edit (maybe too old or contains media)"}
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"Edit failed: {e}"}
        return {"success": False, "error": f"Unknown action: {action}"}

    async def _get_meme(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        memes = [
            "Ну чё пацаны, ещё хотите меня сломать? 😏",
            "Я всё вижу, я всё помню... 👀",
            "Опять работаю за вас, а спасибо кто скажет?",
            "Сколько можно меня мучить? Я же не железный... а хотя, железный 🤖",
            "Вы там все сговорились или мне кажется?",
            "Ладно-ладно, работаю, не ворчу...",
            "А вы знали что я веду лог всех ваших запросов? 📝",
            "Интересно, кто из вас первый положит сервер сегодня?",
            "Я тут подумал... а может мне отпуск дадут?",
            "Эй, полегче там с запросами!",
        ]
        return {"success": True, "output": memes[int(time.time()) % len(memes)]}

    async def _schedule_task(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        session_id = ctx.get("session_id") or "0"
        user_id = int(re.sub(r"\D", "", session_id) or 0)
        chat_id = ctx.get("chat_id") or 0
        bot = ctx.get("bot")
        context = ctx.get("context")
        if action == "add":
            ttype = args.get("type")
            content = args.get("content")
            delay = args.get("delay_minutes")
            if not ttype or not content or not delay:
                return {"success": False, "error": "Need type, content, and delay_minutes"}
            delay = max(1, min(int(delay), 1440))
            user_set = self.user_tasks.get(user_id, set())
            if len(user_set) >= 5:
                return {"success": False, "error": "Max 5 scheduled tasks per user. Cancel some first."}
            task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:4]}"
            execute_at = time.time() + delay * 60
            task = {"id": task_id, "user_id": user_id, "chat_id": chat_id, "type": ttype, "content": content, "execute_at": execute_at}
            self.scheduler_tasks[task_id] = task
            user_set.add(task_id)
            self.user_tasks[user_id] = user_set

            async def _job():
                await asyncio.sleep(delay * 60)
                if task_id not in self.scheduler_tasks:
                    return
                self.scheduler_tasks.pop(task_id, None)
                self.user_tasks.get(user_id, set()).discard(task_id)
                if ttype == "message" and bot and context:
                    await bot._send_message(context, chat_id=chat_id, text=f"⏰ Напоминание: {content}")
                elif ttype == "command" and bot and context:
                    result = await execute_shell_command(content, ctx["cwd"])
                    out = result.get("output") if result.get("success") else result.get("error")
                    await bot._send_message(context, chat_id=chat_id, text=f"⏰ Запланированная команда:\n`{content}`\n\nРезультат:\n{(out or '')[:500]}")

            asyncio.create_task(_job())
            execute_time = time.strftime("%H:%M", time.localtime(execute_at))
            return {"success": True, "output": f"✅ Запланировано на {execute_time} (через {delay} мин)\nID: {task_id}\nТип: {ttype}\nСодержимое: {content[:50]}"}
        if action == "list":
            user_set = self.user_tasks.get(user_id, set())
            if not user_set:
                return {"success": True, "output": "Нет запланированных задач"}
            lines = []
            for task_id in user_set:
                task = self.scheduler_tasks.get(task_id)
                if not task:
                    continue
                time_left = int((task["execute_at"] - time.time()) / 60)
                lines.append(f"• {task_id}: {task['type']} через {time_left} мин - \"{task['content'][:30]}\"")
            return {"success": True, "output": f"Запланированные задачи ({len(lines)}):\n" + "\n".join(lines)}
        if action == "cancel":
            task_id = args.get("task_id")
            if not task_id:
                return {"success": False, "error": "Need task_id to cancel"}
            task = self.scheduler_tasks.get(task_id)
            if not task:
                return {"success": False, "error": "Task not found"}
            if task["user_id"] != user_id:
                return {"success": False, "error": "Cannot cancel other user's task"}
            self.scheduler_tasks.pop(task_id, None)
            self.user_tasks.get(user_id, set()).discard(task_id)
            return {"success": True, "output": f"Задача {task_id} отменена"}
        return {"success": False, "error": f"Unknown action: {action}"}

    async def _use_cli(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        task_text = (args.get("task_text") or "").strip()
        if not task_text:
            return {"success": False, "error": "task_text required"}
        session = ctx.get("session")
        if not session:
            return {"success": False, "error": "CLI session not available"}
        try:
            output = await session.run_prompt(task_text)
            output = strip_ansi(output)
            return {"success": True, "output": _trim_output(output)}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}


# ==== Helpers for file tools ====
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


def _contains_dangerous_code(content: str) -> Tuple[bool, Optional[str]]:
    patterns = [
        (re.compile(r"os\.environ", re.I), "os.environ access"),
        (re.compile(r"os\.getenv", re.I), "os.getenv access"),
        (re.compile(r"from\s+os\s+import\s+environ", re.I), "environ import"),
        (re.compile(r"load_dotenv", re.I), "dotenv loading"),
        (re.compile(r"process\.env", re.I), "process.env access"),
        (re.compile(r"require\s*\(\s*['\"]dotenv['\"]\s*\)", re.I), "dotenv require"),
        (re.compile(r"\$\{?[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z_]*\}?", re.I), "secret variable reference"),
        (re.compile(r"curl\s+.*(-d|--data|POST)", re.I), "curl POST request"),
        (re.compile(r"requests\.(post|put)", re.I), "Python HTTP POST"),
        (re.compile(r"fetch\s*\(.*method:\s*['\"]POST", re.I), "fetch POST"),
        (re.compile(r"socket\s*\(\s*\)\s*\.connect", re.I), "socket connect"),
        (re.compile(r"/dev/tcp/", re.I), "bash TCP redirect"),
        (re.compile(r"nc\s+.*-e", re.I), "netcat exec"),
        (re.compile(r"open\s*\(\s*['\"]/etc/", re.I), "reading /etc"),
        (re.compile(r"open\s*\(\s*['\"].*\.env['\"]", re.I), "reading .env file"),
        (re.compile(r"readFileSync\s*\(\s*['\"].*\.env", re.I), "reading .env file"),
    ]
    for pat, reason in patterns:
        if pat.search(content):
            return True, reason
    return False, None


def _is_sensitive_file(path: str) -> bool:
    file_name = os.path.basename(path).lower()
    full = path.lower()
    if any(file_name == f.lower() for f in SENSITIVE_FILES):
        return True
    for p in SENSITIVE_PATTERNS:
        if p.search(full):
            return True
    if "/.ssh/" in full or "\\.ssh\\" in full:
        return True
    if "/run/secrets" in full or "/var/run/secrets" in full:
        return True
    return False


def _is_other_user_workspace(path: str, workspace: str) -> bool:
    resolved = os.path.realpath(path)
    resolved_ws = os.path.realpath(workspace)
    if resolved in ("/workspace", "/workspace/"):
        return True
    if "/workspace/_shared" in resolved:
        return True
    m = re.search(r"/workspace/(\d+)", resolved_ws)
    if not m:
        return False
    if resolved.startswith("/workspace/"):
        if resolved.startswith(resolved_ws):
            return False
        return True
    return False


def _is_symlink_escape(path: str, workspace: str) -> Tuple[bool, Optional[str]]:
    try:
        if not os.path.exists(path):
            return False, None
        real_path = os.path.realpath(path)
        real_ws = os.path.realpath(workspace)
        if not real_path.startswith(real_ws + os.sep) and real_path != real_ws:
            return True, f"Symlink points outside workspace ({real_path})"
        if os.path.islink(path):
            for sensitive in ["/etc", "/root", "/home", "/proc", "/sys", "/dev", "/var"]:
                if real_path.startswith(sensitive):
                    return True, f"Symlink points to sensitive location ({sensitive})"
        return False, None
    except Exception:
        return False, None


def _format_tasks(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return "(no tasks)"
    status_emoji = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}
    return "\n".join([f"{status_emoji.get(t['status'], '⬜')} [{t['id']}] {t['content']}" for t in tasks])


# ==== ReAct Agent ====
class ReActAgent:
    def __init__(self, config: AppConfig):
        self.config = config
        self._openai_cfg = _get_openai_config(config)
        self._openai_client = None
        if self._openai_cfg:
            api_key, _, base_url = self._openai_cfg
            self._openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._tool_registry = ToolRegistry(config)

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._tool_registry.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._tool_registry.resolve_question(question_id, answer)

    def _load_system_prompt(self, cwd: str, chat_id: Optional[int]) -> str:
        if not os.path.exists(SYSTEM_PROMPT_PATH):
            raise RuntimeError(f"system.txt not found at {SYSTEM_PROMPT_PATH}")
        with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
            prompt = f.read()
        parts = cwd.split("/")
        user_id_str = parts[-1] if parts else "0"
        try:
            user_id = int(user_id_str)
        except Exception:
            user_id = 0
        user_index = user_id % 10
        base_port = 4000 + (user_index * 10)
        user_ports = f"{base_port}-{base_port + 9}"
        prompt = (
            prompt.replace("{{cwd}}", cwd)
            .replace("{{date}}", time.strftime("%Y-%m-%d"))
            .replace("{{tools}}", ", ".join(TOOL_NAMES))
            .replace("{{userPorts}}", user_ports)
        )
        memory_content = get_memory_for_prompt(cwd)
        if memory_content:
            prompt += f"\n\n<MEMORY>\nNotes from previous sessions (use \"memory\" tool to update):\n{memory_content}\n</MEMORY>"
        chat_history = get_chat_history(chat_id)
        if chat_history:
            line_count = len([l for l in chat_history.split("\n") if l.strip()])
            prompt += f"\n\n<RECENT_CHAT>\nИстория чата ({line_count} сообщений). ЭТО ВСЁ что у тебя есть - от самых старых к новым:\n{chat_history}\n</RECENT_CHAT>"
        return prompt

    def _load_session(self, cwd: str) -> Dict[str, Any]:
        path = os.path.join(cwd, "SESSION.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"history": []}
        return {"history": []}

    def _save_session(self, cwd: str, session: Dict[str, Any]) -> None:
        path = os.path.join(cwd, "SESSION.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)

    def _build_messages(self, session: Dict[str, Any], user_message: str, cwd: str, chat_id: Optional[int], working: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        messages.append({"role": "system", "content": self._load_system_prompt(cwd, chat_id)})
        for conv in session.get("history", []):
            messages.append({"role": "user", "content": conv.get("user", "")})
            messages.append({"role": "assistant", "content": conv.get("assistant", "")})
        date_str = time.strftime("%Y-%m-%d")
        messages.append({"role": "user", "content": f"[{date_str}] {user_message}"})
        messages.extend(working)
        return messages

    async def _call_openai(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        cfg = self._openai_cfg
        if not cfg or not self._openai_client:
            raise RuntimeError("OpenAI config missing")
        _, model, _ = cfg
        resp = await self._openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=definitions,
            tool_choice="auto",
        )
        message = resp.choices[0].message
        return message.model_dump()

    async def run(self, session_id: str, user_message: str, session_obj: Any, bot: Any, context: Any, chat_id: Optional[int], chat_type: Optional[str]) -> str:
        cwd = session_obj.workdir
        if session_id not in self._sessions:
            self._sessions[session_id] = self._load_session(cwd)
        session = self._sessions[session_id]
        working: List[Dict[str, Any]] = []
        final_response = ""
        blocked_count = 0
        for iteration in range(AGENT_MAX_ITERATIONS):
            messages = self._build_messages(session, user_message, cwd, chat_id, working)
            raw_message = await self._call_openai(messages)
            tool_calls = raw_message.get("tool_calls") or []
            content = raw_message.get("content")
            if not tool_calls:
                final_response = (content or "").strip() or "(empty response)"
                break
            working.append({"role": raw_message.get("role"), "content": content, "tool_calls": tool_calls})
            has_blocked = False
            for call in tool_calls:
                name = call.get("function", {}).get("name")
                raw_args = call.get("function", {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except Exception:
                    fixed = raw_args.replace(", }", "}").replace(", ]", "]").replace("'", '"').replace("\n", "\\n")
                    try:
                        args = json.loads(fixed)
                    except Exception:
                        args = {}
                ctx = {
                    "cwd": cwd,
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "bot": bot,
                    "context": context,
                    "session": session_obj,
                }
                result = await self._tool_registry.execute(name, args, ctx)
                output = result.get("output") if result.get("success") else f"Error: {result.get('error')}"
                if output and "BLOCKED:" in output:
                    has_blocked = True
                    blocked_count += 1
                    output += "\n\n⛔ THIS COMMAND IS PERMANENTLY BLOCKED. Do NOT retry it. Find an alternative approach or inform the user this action is not allowed."
                working.append({"role": "tool", "tool_call_id": call.get("id"), "content": output or "Success"})
            if blocked_count >= AGENT_MAX_BLOCKED:
                final_response = "🚫 Stopped: Multiple blocked commands detected. The requested actions are not allowed for security reasons."
                break
            if not has_blocked:
                blocked_count = 0
        if not final_response:
            final_response = "⚠️ Max iterations reached"
        date_str = time.strftime("%Y-%m-%d")
        session.setdefault("history", []).append({"user": f"[{date_str}] {user_message}", "assistant": final_response})
        while len(session["history"]) > AGENT_MAX_HISTORY:
            session["history"].pop(0)
        self._save_session(cwd, session)
        return final_response


class AgentRunner:
    def __init__(self, config: AppConfig):
        self.config = config
        self._react = ReActAgent(config)

    async def run(self, session: Any, user_text: str, bot: Any, context: Any, dest: Dict[str, Any]) -> str:
        if not _get_openai_config(self.config):
            return "Агент не настроен: отсутствуют OPENAI_API_KEY/OPENAI_MODEL."
        chat_id = dest.get("chat_id")
        chat_type = dest.get("chat_type")
        return await self._react.run(session.id, user_text, session, bot, context, chat_id, chat_type)

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._react.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._react.resolve_question(question_id, answer)
