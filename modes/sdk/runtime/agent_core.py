import asyncio
import json
import logging
import os
import re
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from modes.sdk.json_store import read_json_locked, write_json_locked
from app.services.redaction import redact_text
from modes.sdk.runtime.json_normalizer import loads_safe
from modes.sdk.runtime.lifecycle_hooks import AgentLifecycleEvent, AgentLifecycleHook
from modes.sdk.runtime.manage_tasks_progress import ManageTasksProgressBridge
from modes.sdk.runtime.openai_client import (
    create_async_openai_client,
    chat_completion as runtime_chat_completion,
    resolve_openai_config,
)
from modes.sdk.runtime.tooling.registry import ToolRegistry as PluginToolRegistry

from config import AppConfig
from sessions.scoped_key import session_scoped_key
from sessions.session_state_access import get_active_mode
from utils.paths import sandbox_root
from utils.text import strip_ansi

_log = logging.getLogger(__name__)

# ==== config constants ====
AGENT_MAX_ITERATIONS = 15
AGENT_MAX_ITERATION_EXTENSION = 5
AGENT_MAX_ITERATION_EXTENSIONS = 1
AGENT_NO_TOOL_GUARD_RETRIES = 1
AGENT_RUNTIME_CHECKPOINT_START = 5
AGENT_RUNTIME_CHECKPOINT_INTERVAL = 5
AGENT_MAX_HISTORY = 20
AGENT_MAX_BLOCKED = 3
MAX_CHAT_MESSAGES = 2500
MAX_MEMORY_CHARS = 2000
CHAT_MESSAGE_LEN = 200
LOG_DETAILS_LEN = 100
REQUEST_CONTEXT_TRIM_LEN = 24_000
REQUEST_CONTEXT_HEAD_LEN = 18_000
REQUEST_CONTEXT_TAIL_LEN = 4_000
CONSTRAINTS_TRIM_LEN = 8_000
CONSTRAINTS_HEAD_LEN = 6_000
CONSTRAINTS_TAIL_LEN = 1_500
WORKING_CONTENT_TRIM_LEN = 12_000
WORKING_CONTENT_HEAD_LEN = 8_000
WORKING_CONTENT_TAIL_LEN = 3_000
WORKING_TOOL_ARGS_TRIM_LEN = 6_000
WORKING_TOOL_ARGS_HEAD_LEN = 4_000
WORKING_TOOL_ARGS_TAIL_LEN = 1_500
RUNTIME_DIGEST_TOOL_PREVIEW_LEN = 500

RUNTIME_ROOT = os.path.dirname(__file__)
SYSTEM_PROMPT_PATH = os.path.join(RUNTIME_ROOT, "system.txt")
_PROGRESSIVE_CORE_TOOL_SCHEMAS = frozenset(
    {
        "ask_user",
        "list_directory",
        "read_file",
        "search_files",
        "search_text",
        "use_cli",
    }
)
# ==== Memory & chat history ====
MEMORY_FILE = "MEMORY.md"


def _shared_dir() -> str:
    sandbox_root_env = os.getenv("AGENT_SANDBOX_ROOT")
    if sandbox_root_env:
        return os.path.join(sandbox_root_env, "_shared")
    return os.path.join(os.getcwd(), "_sandbox", "_shared")


def _chats_dir() -> str:
    return os.path.join(_shared_dir(), "chats")


def _global_log_file() -> str:
    return os.path.join(_shared_dir(), "GLOBAL_LOG.md")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _ensure_shared() -> None:
    _ensure_dir(_shared_dir())


def _ensure_chats() -> None:
    _ensure_shared()
    _ensure_dir(_chats_dir())


def _chat_history_file(chat_id: Optional[int]) -> str:
    chats_dir = _chats_dir()
    if chat_id is None:
        return os.path.join(chats_dir, "chat_global.md")
    return os.path.join(chats_dir, f"chat_{chat_id}.md")


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
        lines = [line for line in content.split("\n") if line.strip()]
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
        log_path = _global_log_file()
        if not os.path.exists(log_path):
            header = "# Global Activity Log\n\n| Time | User | Action | Details |\n|------|------|--------|--------|\n"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(header)
        with open(log_path, "a", encoding="utf-8") as f:
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


def _normalize_web_research_query(args: Any) -> str:
    """Return normalized query string for web_research dedup within one run."""
    if not isinstance(args, dict):
        return ""
    raw = args.get("query")
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    if not text:
        return ""
    return " ".join(text.split())


def _trim_for_context(
    text: Any,
    *,
    limit: int,
    head: int,
    tail: int,
    label: str,
) -> str:
    raw = str(text or "")
    if limit <= 0 or len(raw) <= limit:
        return raw
    marker = f"\n\n...[context-trim: {label}; original={len(raw)} chars]...\n\n"
    available = limit - len(marker)
    if available <= 32:
        return raw[:limit]
    head_len = min(max(0, head), available)
    remaining = available - head_len
    tail_len = min(max(0, tail), remaining)
    if remaining > tail_len:
        head_len += remaining - tail_len
    if tail_len <= 0:
        return raw[:head_len] + marker
    return raw[:head_len] + marker + raw[-tail_len:]


def _compact_tool_calls_for_working(tool_calls: Any) -> List[Dict[str, Any]]:
    compacted: List[Dict[str, Any]] = []
    if not isinstance(tool_calls, list):
        return compacted
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        call = dict(item)
        function = dict(call.get("function") or {})
        if function:
            function["arguments"] = _trim_for_context(
                function.get("arguments") or "",
                limit=WORKING_TOOL_ARGS_TRIM_LEN,
                head=WORKING_TOOL_ARGS_HEAD_LEN,
                tail=WORKING_TOOL_ARGS_TAIL_LEN,
                label="tool_arguments",
            )
            call["function"] = function
        compacted.append(call)
    return compacted


def _compact_working_message(message: Dict[str, Any]) -> Dict[str, Any]:
    compact = dict(message or {})
    content = compact.get("content")
    if isinstance(content, str):
        compact["content"] = _trim_for_context(
            content,
            limit=WORKING_CONTENT_TRIM_LEN,
            head=WORKING_CONTENT_HEAD_LEN,
            tail=WORKING_CONTENT_TAIL_LEN,
            label="working_message",
        )
    tool_calls = compact.get("tool_calls")
    if isinstance(tool_calls, list):
        compact["tool_calls"] = _compact_tool_calls_for_working(tool_calls)
    return compact


def _looks_like_nonfinal_no_tool_text(text: Any) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return True
    if len(normalized) > 700:
        return False
    markers = (
        "сейчас провер",
        "сейчас изуч",
        "сейчас запущ",
        "сначала провер",
        "сначала изуч",
        "проверю",
        "изучу",
        "запущу",
        "проанализирую",
        "let me check",
        "let me inspect",
        "let me run",
        "i'll check",
        "i will check",
        "i'll inspect",
        "i will inspect",
        "i'll run",
        "i will run",
    )
    return any(marker in normalized for marker in markers)


def _redact_runtime_digest_text(text: Any) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    return redact_text(raw)


class ReActAgent:
    def __init__(self, config: AppConfig, tool_registry: PluginToolRegistry):
        self.config = config
        self._openai_cfg = resolve_openai_config(config, model_key="openai_model", env_priority=True)
        self._openai_client = None
        if self._openai_cfg:
            api_key, _, base_url = self._openai_cfg
            self._openai_client = create_async_openai_client(api_key=api_key, base_url=base_url)
        self._sessions: Dict[str, Dict[str, Any]] = {}
        # Обратный индекс: raw_session_id → {scoped_keys} для корректного clear_session_cache
        self._session_id_index: Dict[str, set] = {}
        # Лок удерживается только при load/save кэша сессии по scoped-ключу;
        # тело run выполняется БЕЗ удержания лока. Локи не удаляем: удаление
        # удерживаемого lock может разнести ожидающие корутины по разным lock.
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # ToolRegistry must be a singleton shared across executor/orchestrator/agent.
        self._tool_registry = tool_registry

    def _get_session_lock(self, key: str) -> asyncio.Lock:
        """Лениво создаёт и кэширует Lock для scoped-ключа сессии."""
        if key not in self._session_locks:
            self._session_locks[key] = asyncio.Lock()
        return self._session_locks[key]

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._tool_registry.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._tool_registry.resolve_question(question_id, answer)

    def _allowed_tool_names(self, allowed_tools: Optional[List[str]]) -> List[str]:
        available = self._tool_registry.list_tool_names()
        if allowed_tools is None or allowed_tools == [] or allowed_tools == ["All"]:
            return available
        allowed_set = {str(name).strip() for name in allowed_tools if str(name).strip()}
        return [name for name in available if name in allowed_set]

    def _build_tools_block(self, allowed_tools: Optional[List[str]]) -> str:
        names = self._allowed_tool_names(allowed_tools)
        specs = getattr(self._tool_registry, "specs", {}) or {}
        lines = ["<TOOLS>"]
        if names:
            lines.append("Available tools in this run:")
            for name in names:
                spec = specs.get(name)
                description = ""
                if spec is not None:
                    description = str(getattr(spec, "one_liner", "") or getattr(spec, "description", "") or "").strip()
                lines.append(f"- {name}: {description}" if description else f"- {name}")
        else:
            lines.append("No tools are available in this run.")

        disclosure = getattr(self.config.defaults, "tool_disclosure", "full")
        if disclosure == "progressive" and "get_tool_details" in names:
            lines.append(
                'Before first use of any tool, call get_tool_details(tool_names="tool_name") '
                "to fetch the full parameter schema."
            )
        lines.append("</TOOLS>")
        return "\n".join(lines)

    async def _build_request_tool_definitions(self, allowed_tools: Optional[List[str]]) -> List[Dict[str, Any]]:
        normalized_allowed_tools = allowed_tools or ["All"]
        disclosure = getattr(self.config.defaults, "tool_disclosure", "full")
        if disclosure != "progressive":
            return await self._tool_registry.get_definitions_async(normalized_allowed_tools)

        await self._tool_registry.ensure_mcp_loaded()
        allowed_names = self._allowed_tool_names(allowed_tools)

        # Progressive disclosure is only safe when the meta-tool is actually available.
        # Otherwise the model sees empty parameter schemas and starts guessing required fields.
        if "get_tool_details" not in allowed_names:
            return await self._tool_registry.get_definitions_async(normalized_allowed_tools)

        definitions = self._tool_registry.get_summary_definitions(normalized_allowed_tools)
        full_schema_names = {"get_tool_details"} | (_PROGRESSIVE_CORE_TOOL_SCHEMAS & set(allowed_names))
        full_definitions: Dict[str, Dict[str, Any]] = {}
        for tool_name in full_schema_names:
            detail = self._tool_registry.get_tool_detail(tool_name)
            if detail:
                full_definitions[tool_name] = detail

        if not full_definitions:
            return definitions

        merged: List[Dict[str, Any]] = []
        for item in definitions:
            function = item.get("function") if isinstance(item, dict) else {}
            name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
            merged.append(full_definitions.get(name, item))
        return merged

    @staticmethod
    def _claim_status_for_result(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized == "ok":
            return "confirmed"
        if normalized in {"partial", "needs_input"}:
            return "needs_check"
        if normalized in {"error", "blocked", "cancelled"}:
            return "unconfirmed"
        return "needs_check"

    @staticmethod
    def _extract_claim_texts(text: str) -> List[str]:
        raw = str(text or "").strip()
        if not raw:
            return []
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        bullet_lines = [
            line for line in lines
            if line.startswith(("- ", "* ", "• ")) or re.match(r"^\d+[.)]\s+", line)
        ]
        candidates: List[str] = []
        if len(bullet_lines) >= 2:
            for line in bullet_lines[:8]:
                item = re.sub(r"^(\d+[.)]\s+|[-*•]\s+)", "", line).strip()
                if item:
                    candidates.append(item)
        else:
            for segment in re.split(r"(?<=[.!?;])\s+|\n+", raw):
                item = " ".join(str(segment or "").split()).strip(" -\t\r\n")
                if item and len(item) >= 12:
                    candidates.append(item)
        deduped: List[str] = []
        seen: set[str] = set()
        for item in candidates:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:8]

    def _build_native_claims(self, *, text: str, status: str) -> List[Dict[str, Any]]:
        claim_status = self._claim_status_for_result(status)
        return [
            {
                "claim_id": f"claim_{idx}",
                "status": claim_status,
                "text": item,
                "evidence": [{"type": "text", "path": "", "preview": item}],
            }
            for idx, item in enumerate(self._extract_claim_texts(text), start=1)
        ]

    async def _extract_structured_claims(self, *, text: str, status: str, model_name: str) -> tuple[List[Dict[str, Any]], str]:
        raw_text = str(text or "").strip()
        if not raw_text:
            return [], "none"
        try:
            system = (
                "Извлеки из финального текста шага структурированные claims. "
                "Верни JSON object с массивом claims. "
                "Каждый claim: claim_id, status, text, component_scope, allowed_final_usage, evidence. "
                "status: confirmed|needs_check|unconfirmed. "
                "allowed_final_usage: fact|open_question|blocked_item."
            )
            user = f"step_status={str(status or '').strip()}\n\n{text}"
            raw = await runtime_chat_completion(
                self.config,
                system,
                user,
                response_format={"type": "json_object"},
                model=model_name,
                temperature=0.0,
                max_tokens=8000,
            )
            payload = loads_safe(str(raw or ""), strict_first=False)
            items = payload.get("claims") if isinstance(payload, dict) else None
            normalized: List[Dict[str, Any]] = []
            if isinstance(items, list):
                default_status = self._claim_status_for_result(status)
                for idx, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        continue
                    text_value = str(item.get("text") or "").strip()
                    if not text_value:
                        continue
                    status_value = str(item.get("status") or default_status).strip().lower()
                    if status_value not in {"confirmed", "needs_check", "unconfirmed"}:
                        status_value = default_status
                    usage_value = str(item.get("allowed_final_usage") or "").strip().lower()
                    if usage_value not in {"fact", "open_question", "blocked_item"}:
                        if status_value == "confirmed":
                            usage_value = "fact"
                        elif status_value == "needs_check":
                            usage_value = "open_question"
                        else:
                            usage_value = "blocked_item"
                    evidence_items: List[Dict[str, Any]] = []
                    for ev in item.get("evidence") if isinstance(item.get("evidence"), list) else []:
                        if not isinstance(ev, dict):
                            continue
                        evidence_items.append(
                            {
                                "type": str(ev.get("type") or "text").strip() or "text",
                                "path": str(ev.get("path") or "").strip(),
                                "preview": str(ev.get("preview") or "").strip(),
                            }
                        )
                    normalized.append(
                        {
                            "claim_id": str(item.get("claim_id") or f"claim_{idx}").strip() or f"claim_{idx}",
                            "status": status_value,
                            "text": text_value,
                            "component_scope": str(item.get("component_scope") or "general").strip() or "general",
                            "allowed_final_usage": usage_value,
                            "evidence": evidence_items,
                        }
                    )
            if normalized:
                return normalized, "llm_json"
        except Exception:
            _log.debug("structured claim extraction failed; fallback to text claims", exc_info=True)
        return self._build_native_claims(text=raw_text, status=status), "text_fallback"

    def _load_system_prompt(
        self,
        cwd: str,
        chat_id: Optional[int],
        allowed_tools: Optional[List[str]] = None,
    ) -> str:
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
        tool_names = ", ".join(self._allowed_tool_names(allowed_tools))
        prompt = (
            prompt.replace("{{cwd}}", cwd)
            .replace("{{date}}", time.strftime("%Y-%m-%d"))
            .replace("{{tools}}", tool_names)
            .replace("{{userPorts}}", user_ports)
            .replace("{{tools_disclosure_hint}}", "")
        )
        prompt = re.sub(r"<TOOLS>.*?</TOOLS>", self._build_tools_block(allowed_tools), prompt, flags=re.S)
        memory_content = get_memory_for_prompt(cwd)
        if memory_content:
            prompt += f"\n\n<MEMORY>\nNotes from previous sessions (use \"memory\" tool to update):\n{memory_content}\n</MEMORY>"
        chat_history = get_chat_history(chat_id)
        if chat_history:
            line_count = len([line for line in chat_history.split("\n") if line.strip()])
            prompt += (
                f"\n\n<RECENT_CHAT>\nИстория чата ({line_count} сообщений). "
                f"ЭТО ВСЁ что у тебя есть - от самых старых к новым:\n{chat_history}\n</RECENT_CHAT>"
            )
        return prompt

    def _load_session(self, state_root: str) -> Dict[str, Any]:
        path = os.path.join(state_root, "SESSION.json")
        data = read_json_locked(path, default={"history_by_task": {}})
        if isinstance(data, dict):
            data.setdefault("history_by_task", {})
            return data
        return {"history_by_task": {}}

    def _save_session(self, state_root: str, session: Dict[str, Any]) -> None:
        path = os.path.join(state_root, "SESSION.json")
        write_json_locked(path, session)

    def _build_messages(
        self,
        session: Dict[str, Any],
        user_message: str,
        cwd: str,
        chat_id: Optional[int],
        working: List[Dict[str, Any]],
        task_id: Optional[str],
        request_context: Optional[str],
        constraints: Optional[str],
        corr_id: Optional[str],
        allowed_tools: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        messages.append({"role": "system", "content": self._load_system_prompt(cwd, chat_id, allowed_tools)})
        extra_parts: List[str] = []
        if corr_id:
            extra_parts.append(f"corr_id: {corr_id}")
        if request_context:
            compact_request_context = _trim_for_context(
                request_context,
                limit=REQUEST_CONTEXT_TRIM_LEN,
                head=REQUEST_CONTEXT_HEAD_LEN,
                tail=REQUEST_CONTEXT_TAIL_LEN,
                label="request_context",
            )
            extra_parts.append(f"<REQUEST_CONTEXT>\n{compact_request_context}\n</REQUEST_CONTEXT>")
        if constraints:
            compact_constraints = _trim_for_context(
                constraints,
                limit=CONSTRAINTS_TRIM_LEN,
                head=CONSTRAINTS_HEAD_LEN,
                tail=CONSTRAINTS_TAIL_LEN,
                label="constraints",
            )
            extra_parts.append(f"<CONSTRAINTS>\n{compact_constraints}\n</CONSTRAINTS>")
        if extra_parts:
            messages.append({"role": "system", "content": "\n\n".join(extra_parts)})
        task_history = session.get("history_by_task", {}).get(task_id or "unknown", [])
        for conv in task_history:
            messages.append({"role": "user", "content": conv.get("user", "")})
            messages.append({"role": "assistant", "content": conv.get("assistant", "")})
        date_str = time.strftime("%Y-%m-%d")
        messages.append({"role": "user", "content": f"[{date_str}] {user_message}"})
        messages.extend(working)
        return messages

    async def _call_openai(
        self, messages: List[Dict[str, Any]], allowed_tools: Optional[List[str]]
    ) -> Dict[str, Any]:
        cfg = self._openai_cfg
        if not cfg or not self._openai_client:
            raise RuntimeError("OpenAI config missing")
        _, model, _ = cfg
        definitions = await self._build_request_tool_definitions(allowed_tools)
        resp = await self._openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=definitions,
            tool_choice="auto",
        )
        message = resp.choices[0].message
        return message.model_dump()

    async def run(
        self,
        session_id: str,
        user_message: str,
        session_obj: Any,
        bot: Any,
        context: Any,
        chat_id: Optional[int],
        chat_type: Optional[str],
        task_id: Optional[str],
        allowed_tools: Optional[List[str]] = None,
        request_context: Optional[str] = None,
        constraints: Optional[str] = None,
        corr_id: Optional[str] = None,
        failure_event_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        progress_event_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        lifecycle_hook: Optional[AgentLifecycleHook] = None,
        cancel_event: Optional["asyncio.Event"] = None,
        observability: Optional[Any] = None,
        run_handle: Optional[Any] = None,
    ) -> "AgentRunResult":
        runtime_session_key = session_scoped_key(session_obj) or str(session_id or "").strip()
        raw_session_id = str(getattr(session_obj, "id", "") or "").strip() or runtime_session_key
        active_mode_id = str(get_active_mode(session_obj, "") or "").strip()
        run_id = str(getattr(run_handle, "run_id", "") or "").strip()
        manage_tasks_run_token = run_id or str(task_id or corr_id or f"local-{int(time.time() * 1000)}")
        manage_tasks_scope_key = f"{runtime_session_key}:manage_tasks:{manage_tasks_run_token}"
        manage_tasks_progress = ManageTasksProgressBridge()
        cwd = session_obj.workdir
        # Single source of truth for runtime state is passed by executor (chat-scoped).
        state_root = str(getattr(session_obj, "state_root", "") or "").strip()
        if not state_root:
            state_root = sandbox_root(self.config.defaults.workdir)
        os.makedirs(state_root, exist_ok=True)
        _log.info("ReAct start session=%s task=%s corr_id=%s msg=%r",
                  runtime_session_key, task_id, corr_id, user_message[:200])

        lifecycle_tasks: set[asyncio.Task[None]] = set()

        async def _emit_lifecycle_event(event: AgentLifecycleEvent) -> None:
            if lifecycle_hook is None:
                return
            try:
                await lifecycle_hook(event.redacted_copy())
            except Exception:
                _log.exception(
                    "failed to emit agent lifecycle event session=%s task=%s event=%s",
                    runtime_session_key,
                    task_id,
                    event.event_type,
                )

        def _schedule_lifecycle_event(event: AgentLifecycleEvent) -> None:
            if lifecycle_hook is None:
                return
            try:
                task = asyncio.create_task(_emit_lifecycle_event(event))
                lifecycle_tasks.add(task)
                task.add_done_callback(lifecycle_tasks.discard)
            except Exception:
                _log.debug("lifecycle event scheduling failed", exc_info=True)

        async def _drain_lifecycle_events() -> None:
            if not lifecycle_tasks:
                return
            pending = list(lifecycle_tasks)
            lifecycle_tasks.clear()
            await asyncio.gather(*pending, return_exceptions=True)

        async def _emit_progress(phase: str, status: str, message: str, *, iteration: int = 0, step_id: str = "") -> None:
            event = AgentLifecycleEvent(
                event_type="runtime_progress",
                mode_id=active_mode_id,
                session_id=raw_session_id,
                session_uid=runtime_session_key,
                run_id=run_id,
                task_id=str(task_id or ""),
                corr_id=str(corr_id or ""),
                phase=str(phase or "event"),
                status=str(status or "running"),
                iteration=int(iteration or 0),
                step_id=str(step_id or ""),
                message=str(message or ""),
            )
            await _emit_lifecycle_event(event)
            if progress_event_callback is None:
                return
            try:
                await progress_event_callback(event.to_runtime_progress_payload())
            except Exception:
                _log.exception(
                    "failed to emit agent progress event session=%s task=%s phase=%s",
                    runtime_session_key,
                    task_id,
                    phase,
                )

        _llm_trace = bool(getattr(self.config.defaults, "llm_trace_enabled", False))

        def _resolve_run_artifact_handle() -> Optional[Any]:
            candidates = [run_handle, getattr(session_obj, "agent_run_artifact_handle", None)]
            candidates.append(getattr(session_obj, "analyst_run_artifact_handle", None))
            tool_session = getattr(session_obj, "tool_session", None)
            if tool_session is not None:
                candidates.append(getattr(tool_session, "agent_run_artifact_handle", None))
                candidates.append(getattr(tool_session, "analyst_run_artifact_handle", None))
            for candidate in candidates:
                artifacts_dir = str(getattr(candidate, "artifacts_dir", "") or "").strip()
                checkpoints_path = str(getattr(candidate, "checkpoints_path", "") or "").strip()
                run_token = str(getattr(candidate, "run_id", "") or "").strip()
                if artifacts_dir and checkpoints_path and run_token:
                    return candidate
            return None

        def _resolve_context_summary_artifact_dir() -> str:
            candidate_handles = [run_handle, getattr(session_obj, "agent_run_artifact_handle", None)]
            candidate_handles.append(getattr(session_obj, "analyst_run_artifact_handle", None))
            tool_session = getattr(session_obj, "tool_session", None)
            if tool_session is not None:
                candidate_handles.append(getattr(tool_session, "agent_run_artifact_handle", None))
                candidate_handles.append(getattr(tool_session, "analyst_run_artifact_handle", None))
            for candidate in candidate_handles:
                artifacts_dir = str(getattr(candidate, "artifacts_dir", "") or "").strip()
                if artifacts_dir:
                    os.makedirs(artifacts_dir, exist_ok=True)
                    return artifacts_dir
            artifact_dir = os.path.join(cwd, "_orchestrator")
            os.makedirs(artifact_dir, exist_ok=True)
            return artifact_dir

        def _persist_context_summary_artifact(kind: str, iteration_no: int, content: str) -> str:
            text = str(content or "").strip()
            if not text:
                return ""
            try:
                artifact_dir = _resolve_context_summary_artifact_dir()
                slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_session_id or "session").strip("._-") or "session"
                filename = f"{slug}_{kind}_iter_{max(1, int(iteration_no or 0))}.md"
                path = os.path.join(artifact_dir, filename)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                return path
            except Exception:
                _log.exception(
                    "failed to persist context summary artifact session=%s task=%s kind=%s",
                    runtime_session_key,
                    task_id,
                    kind,
                )
                return ""

        def _trace_llm_request(**kwargs: Any) -> None:
            try:
                event = AgentLifecycleEvent(
                    event_type="llm_request",
                    mode_id=active_mode_id,
                    session_id=raw_session_id,
                    session_uid=runtime_session_key,
                    run_id=run_id,
                    task_id=str(task_id or ""),
                    corr_id=str(corr_id or ""),
                    phase="llm_request",
                    status="running",
                    metadata=dict(kwargs or {}),
                )
                _schedule_lifecycle_event(event)
            except Exception:
                _log.debug("lifecycle llm request dispatch failed", exc_info=True)
            if _llm_trace and observability and run_handle:
                try:
                    observability.record_llm_request(run_handle, corr_id=corr_id, **kwargs)
                except Exception:
                    _log.debug("llm trace request failed", exc_info=True)

        def _trace_llm_response(**kwargs: Any) -> None:
            try:
                event = AgentLifecycleEvent(
                    event_type="llm_response",
                    mode_id=active_mode_id,
                    session_id=raw_session_id,
                    session_uid=runtime_session_key,
                    run_id=run_id,
                    task_id=str(task_id or ""),
                    corr_id=str(corr_id or ""),
                    phase="llm_response",
                    status="ok",
                    metadata=dict(kwargs or {}),
                )
                _schedule_lifecycle_event(event)
            except Exception:
                _log.debug("lifecycle llm response dispatch failed", exc_info=True)
            if _llm_trace and observability and run_handle:
                try:
                    observability.record_llm_response(run_handle, corr_id=corr_id, **kwargs)
                except Exception:
                    _log.debug("llm trace response failed", exc_info=True)

        def _trace_tool_execution(**kwargs: Any) -> None:
            try:
                event = AgentLifecycleEvent(
                    event_type="tool_execution",
                    mode_id=active_mode_id,
                    session_id=raw_session_id,
                    session_uid=runtime_session_key,
                    run_id=run_id,
                    task_id=str(task_id or ""),
                    corr_id=str(corr_id or ""),
                    phase="tool_execution",
                    status="ok" if bool(kwargs.get("success", True)) else "error",
                    tool_name=str(kwargs.get("tool_name") or ""),
                    error=str(kwargs.get("error") or ""),
                    metadata=dict(kwargs or {}),
                )
                _schedule_lifecycle_event(event)
            except Exception:
                _log.debug("lifecycle tool execution dispatch failed", exc_info=True)
            if _llm_trace and observability and run_handle:
                try:
                    observability.record_tool_execution(run_handle, corr_id=corr_id, **kwargs)
                except Exception:
                    _log.debug("llm trace tool exec failed", exc_info=True)

        await _emit_progress("start", "running", "ReAct запущен")
        async with self._get_session_lock(runtime_session_key):
            if runtime_session_key not in self._sessions:
                self._sessions[runtime_session_key] = self._load_session(state_root)
            # Регистрируем обратный индекс raw_session_id → scoped_key
            if raw_session_id and raw_session_id != runtime_session_key:
                self._session_id_index.setdefault(raw_session_id, set()).add(runtime_session_key)
            session = self._sessions[runtime_session_key]
        working: List[Dict[str, Any]] = []
        final_response = ""
        final_status = "ok"
        blocked_count = 0
        tool_facts: List[Dict[str, Any]] = []
        iterations_done = 0
        consecutive_all_failed = 0
        last_batch_sig: Optional[str] = None
        repeated_batches = 0
        max_iterations = AGENT_MAX_ITERATIONS
        iteration_extensions_used = 0
        seen_web_research_queries: set[str] = set()
        no_tool_guard_retries = 0
        runtime_digest_keys: set[tuple[int, str]] = set()

        def _text_preview(v: Any, max_chars: int = 2000) -> str:
            try:
                s = strip_ansi(str(v or ""))
            except Exception:
                s = ""
            if len(s) > max_chars:
                return s[:max_chars] + "...(truncated)"
            return s

        def _runtime_digest_payload(iteration_no: int, status: str) -> Dict[str, Any]:
            recent_tools = []
            for item in tool_facts[-6:]:
                recent_tools.append(
                    {
                        "tool": item.get("tool"),
                        "success": bool(item.get("success")),
                        "error": _redact_runtime_digest_text(
                            _text_preview(item.get("error"), max_chars=RUNTIME_DIGEST_TOOL_PREVIEW_LEN)
                        ),
                        "output_preview": _redact_runtime_digest_text(
                            _text_preview(
                                item.get("output_preview"),
                                max_chars=RUNTIME_DIGEST_TOOL_PREVIEW_LEN,
                            )
                        ),
                    }
                )
            return {
                "phase": "execute",
                "unit_id": str(task_id or "agent:runtime"),
                "status": str(status or "running"),
                "source": "agent_core",
                "kind": "runtime_digest",
                "iteration": int(iteration_no or 0),
                "max_iterations": int(max_iterations),
                "tool_calls_count": len(tool_facts),
                "working_messages_count": len(working),
                "recent_tools": recent_tools,
            }

        def _persist_runtime_digest_artifact(handle: Any, payload: Dict[str, Any]) -> str:
            try:
                artifact_dir = str(getattr(handle, "artifacts_dir", "") or "").strip()
                if not artifact_dir:
                    return ""
                os.makedirs(artifact_dir, exist_ok=True)
                slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_session_id or "session").strip("._-") or "session"
                iteration_no = max(1, int(payload.get("iteration") or 0))
                status_slug = re.sub(
                    r"[^A-Za-z0-9._-]+",
                    "_",
                    str(payload.get("status") or "running"),
                ).strip("._-") or "running"
                path = os.path.join(artifact_dir, f"{slug}_runtime_digest_iter_{iteration_no}_{status_slug}.md")
                lines = [
                    "# Runtime digest",
                    "",
                    f"- status: {payload.get('status')}",
                    f"- iteration: {payload.get('iteration')} / {payload.get('max_iterations')}",
                    f"- tool_calls_count: {payload.get('tool_calls_count')}",
                    f"- working_messages_count: {payload.get('working_messages_count')}",
                    "",
                    "## Recent tools",
                ]
                for item in payload.get("recent_tools") or []:
                    tool = item.get("tool") or "?"
                    lines.append(f"- {tool}: success={bool(item.get('success'))}")
                    if item.get("error"):
                        lines.append(f"  error: {item.get('error')}")
                    elif item.get("output_preview"):
                        lines.append(f"  output: {item.get('output_preview')}")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines).rstrip() + "\n")
                return path
            except Exception:
                _log.exception("failed to persist runtime digest artifact session=%s task=%s", runtime_session_key, task_id)
                return ""

        def _maybe_persist_runtime_checkpoint(iteration_no: int, status: str = "running", *, force: bool = False) -> None:
            resolved_iteration = int(iteration_no or 0)
            if resolved_iteration <= 0:
                return
            if not force:
                if resolved_iteration < AGENT_RUNTIME_CHECKPOINT_START:
                    return
                if resolved_iteration % AGENT_RUNTIME_CHECKPOINT_INTERVAL != 0:
                    return
            digest_key = (resolved_iteration, str(status or "running"))
            if digest_key in runtime_digest_keys:
                return
            handle = _resolve_run_artifact_handle()
            if handle is None:
                return
            payload = _runtime_digest_payload(resolved_iteration, status)
            artifact_path = _persist_runtime_digest_artifact(handle, payload)
            if artifact_path:
                payload["artifact_path"] = artifact_path
            try:
                from app.services.run_artifact_store import RunArtifactStore

                RunArtifactStore(self.config).append_checkpoint(handle, payload)
                runtime_digest_keys.add(digest_key)
            except Exception:
                _log.exception(
                    "failed to append runtime digest checkpoint session=%s task=%s iteration=%d",
                    runtime_session_key,
                    task_id,
                    resolved_iteration,
                )

        async def _maybe_extend_iteration_budget(*, iteration_no: int) -> bool:
            nonlocal max_iterations, iteration_extensions_used
            if iteration_extensions_used >= AGENT_MAX_ITERATION_EXTENSIONS:
                return False
            if iteration_no < max_iterations or final_response:
                return False
            if blocked_count > 0 or consecutive_all_failed > 0:
                return False
            recent = tool_facts[-4:]
            if not any(bool(item.get("success")) for item in recent):
                return False
            max_iterations += AGENT_MAX_ITERATION_EXTENSION
            iteration_extensions_used += 1
            _log.info(
                "ReAct extending iteration budget by %d -> %d after visible progress (extensions=%d)",
                AGENT_MAX_ITERATION_EXTENSION,
                max_iterations,
                iteration_extensions_used,
            )
            await _emit_progress(
                "extend_iterations",
                "running",
                f"Продлеваю лимит итераций на {AGENT_MAX_ITERATION_EXTENSION}: обнаружен прогресс, завершаю работу",
                iteration=iteration_no,
            )
            return True

        # Token counting config.
        _ctx_window = int(getattr(self.config.defaults, "context_window_tokens", 128_000) or 128_000)
        _ctx_reserve = int(getattr(self.config.defaults, "context_reserve_tokens", 4096) or 4096)
        _ctx_threshold = float(getattr(self.config.defaults, "summarization_threshold", 0.75) or 0.75)
        _model_name = (self._openai_cfg[1] if self._openai_cfg else "gpt-4o")

        iteration = 0
        while iteration < max_iterations:
            base_messages = self._build_messages(
                session,
                user_message,
                cwd,
                chat_id,
                [],
                task_id,
                request_context=request_context,
                constraints=constraints,
                corr_id=corr_id,
                allowed_tools=allowed_tools,
            )
            messages = list(base_messages) + list(working)

            # Auto-summarize the growing ReAct working tail first so compression persists across iterations.
            try:
                from modes.sdk.runtime.context_summarizer import summarize_context, summarize_working_context

                working, working_summarized = await summarize_working_context(
                    working,
                    base_messages=base_messages,
                    config=self.config,
                    model=_model_name,
                    max_tokens=_ctx_window,
                    reserve_tokens=_ctx_reserve,
                    threshold=_ctx_threshold,
                )
                messages = list(base_messages) + list(working)
                if working_summarized:
                    working_summary_artifact = _persist_context_summary_artifact(
                        "working_context_summary",
                        iteration + 1,
                        str((working[0] or {}).get("content") or "") if working else "",
                    )
                    progress_message = "Рабочий контекст суммаризирован для экономии токенов"
                    if working_summary_artifact:
                        progress_message += f" (artifact: {working_summary_artifact})"
                    await _emit_progress(
                        "context_summarized", "running",
                        progress_message,
                        iteration=iteration + 1,
                    )

                messages, was_summarized = await summarize_context(
                    messages,
                    config=self.config,
                    model=_model_name,
                    max_tokens=_ctx_window,
                    reserve_tokens=_ctx_reserve,
                    threshold=_ctx_threshold,
                )
                if was_summarized:
                    history_summary_artifact = _persist_context_summary_artifact(
                        "historical_context_summary",
                        iteration + 1,
                        next(
                            (
                                str(item.get("content") or "")
                                for item in messages
                                if str(item.get("content") or "").startswith("[Контекст суммаризирован]")
                            ),
                            "",
                        ),
                    )
                    # summarize_context only changes the current request payload; working compression above
                    # is what persists across iterations.
                    progress_message = "Исторический контекст суммаризирован для экономии токенов"
                    if history_summary_artifact:
                        progress_message += f" (artifact: {history_summary_artifact})"
                    await _emit_progress(
                        "context_summarized", "running",
                        progress_message,
                        iteration=iteration + 1,
                    )
            except Exception:
                _log.exception("context summarization failed, continuing with full context")

            iterations_done = iteration + 1
            _log.info("ReAct iter=%d/%d calling LLM (messages=%d)", iterations_done, max_iterations, len(messages))
            # Count tokens for progress reporting.
            try:
                from modes.sdk.runtime.token_counter import count_messages_tokens
                _current_tokens = count_messages_tokens(messages, _model_name)
            except Exception:
                _current_tokens = 0
            await _emit_progress(
                "iteration",
                "running",
                f"Итерация {iterations_done}: запрос к модели ({_current_tokens} tokens)",
                iteration=iterations_done,
            )
            # Cooperative cancellation check.
            if cancel_event is not None and cancel_event.is_set():
                _log.info("ReAct iter=%d cancelled via event", iterations_done)
                final_response = "Операция отменена пользователем."
                final_status = "cancelled"
                await _emit_progress("cancelled", "cancelled", "Отменено пользователем", iteration=iterations_done)
                break
            _trace_llm_request(
                model=_model_name,
                messages_count=len(messages),
                tools_count=len(self._tool_registry.list_tool_names()),
                estimated_tokens=_current_tokens,
            )
            _llm_start = time.time()
            raw_message = await self._call_openai(messages, allowed_tools)
            _llm_elapsed_ms = int((time.time() - _llm_start) * 1000)
            tool_calls = raw_message.get("tool_calls") or []
            content = raw_message.get("content")
            # Trace LLM response.
            _tc_summary = ", ".join(
                tc.get("function", {}).get("name", "?") for tc in (tool_calls or [])
            )[:500] if tool_calls else None
            _trace_llm_response(
                model=_model_name,
                content_preview=(content or "")[:500] if content else None,
                tool_calls_summary=_tc_summary,
                duration_ms=_llm_elapsed_ms,
            )
            if not tool_calls:
                if (
                    self._allowed_tool_names(allowed_tools)
                    and _looks_like_nonfinal_no_tool_text(content)
                ):
                    no_tool_guard_retries += 1
                    if no_tool_guard_retries <= AGENT_NO_TOOL_GUARD_RETRIES:
                        working.append(_compact_working_message({"role": "assistant", "content": content or ""}))
                        working.append(
                            {
                                "role": "user",
                                "content": (
                                    "Ответ выглядит как обещание действия без вызова инструмента. "
                                    "Если нужно проверить, прочитать, запустить или изменить что-то, вызови подходящий tool. "
                                    "Если инструменты не нужны, дай настоящий финальный ответ без обещаний будущих действий."
                                ),
                            }
                        )
                        _log.info("ReAct iter=%d no-tool guard retry", iterations_done)
                        await _emit_progress(
                            "no_tool_guard_retry",
                            "running",
                            f"Итерация {iterations_done}: ответ без tool_calls отправлен на уточнение",
                            iteration=iterations_done,
                        )
                        iteration += 1
                        continue
                    final_response = (
                        "Прогресс остановился: модель ответила обещанием действия без вызова инструментов. "
                        "Останавливаюсь, чтобы не выдавать невыполненную работу за результат."
                    )
                    final_status = "partial"
                    await _emit_progress(
                        "stop_no_tool_guard",
                        "partial",
                        "Остановка: ответ без tool_calls не содержит проверенного результата",
                        iteration=iterations_done,
                    )
                    break
                final_response = (content or "").strip() or "(empty response)"
                _log.info("ReAct iter=%d no tool_calls, final text (%d chars)", iterations_done, len(final_response))
                await _emit_progress(
                    "final_text",
                    "running",
                    f"Итерация {iterations_done}: получен финальный ответ",
                    iteration=iterations_done,
                )
                break
            tool_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
            _log.info("ReAct iter=%d tool_calls=%d: %s", iteration + 1, len(tool_calls), ", ".join(tool_names))
            await _emit_progress(
                "tool_batch",
                "running",
                f"Итерация {iterations_done}: вызовы инструментов ({', '.join(tool_names[:6])})",
                iteration=iterations_done,
            )
            if content:
                _log.info("ReAct iter=%d LLM also said: %r", iteration + 1, content[:200])
            working.append(
                _compact_working_message(
                    {"role": raw_message.get("role"), "content": content, "tool_calls": tool_calls}
                )
            )
            has_blocked = False
            unknown_tool = False
            all_failed = True
            tool_session = getattr(session_obj, "tool_session", None) or session_obj
            ctx = {
                "cwd": cwd,
                "state_root": state_root,
                "session_id": raw_session_id,
                "session_scoped_key": runtime_session_key,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "bot": bot,
                "context": context,
                "session": tool_session,
                "allowed_tools": allowed_tools or ["All"],
                "corr_id": corr_id,
                "run_id": run_id,
                "manage_tasks_scope_key": manage_tasks_scope_key,
            }
            calls = []
            call_meta: List[Dict[str, Any]] = []
            for call in tool_calls:
                name = call.get("function", {}).get("name")
                raw_args = call.get("function", {}).get("arguments") or "{}"
                try:
                    args = loads_safe(str(raw_args or ""), strict_first=False)
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                calls.append({"name": name, "args": args})
                call_meta.append({"name": name, "args": args})

            # Detect repeated identical tool-call batches (common "stuck" pattern).
            # Use a stable hash of tool names + args (sorted keys) to avoid large in-memory payloads.
            try:
                sig_parts: List[str] = []
                for c in calls:
                    nm = str(c.get("name") or "")
                    a = c.get("args") or {}
                    try:
                        a_s = json.dumps(a, ensure_ascii=False, sort_keys=True)
                    except Exception:
                        a_s = repr(a)
                    sig_parts.append(f"{nm}:{a_s}")
                sig_raw = "|".join(sig_parts)
                batch_sig = hashlib.sha256(sig_raw.encode("utf-8", errors="ignore")).hexdigest()
            except Exception:
                batch_sig = None

            prefilled_results: List[Optional[Dict[str, Any]]] = [None] * len(calls)
            calls_to_execute: List[Dict[str, Any]] = []
            for i, call in enumerate(calls):
                name = str(call.get("name") or "").strip()
                if name != "web_research":
                    calls_to_execute.append(call)
                    continue
                norm_query = _normalize_web_research_query(call.get("args") or {})
                if not norm_query:
                    calls_to_execute.append(call)
                    continue
                if norm_query in seen_web_research_queries:
                    prefilled_results[i] = {
                        "success": False,
                        "error": (
                            "Повтор web_research с тем же query в рамках одного шага заблокирован. "
                            "Используйте другой query или другой инструмент."
                        ),
                    }
                    continue
                seen_web_research_queries.add(norm_query)
                calls_to_execute.append(call)

            executed_results: List[Dict[str, Any]] = []
            if calls_to_execute:
                executed_results = await self._tool_registry.execute_many(calls_to_execute, ctx)
            results: List[Dict[str, Any]] = []
            exec_pos = 0
            for i in range(len(calls)):
                if prefilled_results[i] is not None:
                    results.append(prefilled_results[i] or {"success": False, "error": "unknown dedup result"})
                    continue
                if exec_pos < len(executed_results):
                    results.append(executed_results[exec_pos])
                    exec_pos += 1
                else:
                    results.append({"success": False, "error": "tool execution mapping mismatch"})
            for idx_r, (call, result) in enumerate(zip(tool_calls, results)):
                name = calls[idx_r]["name"]
                success = bool(result.get("success"))
                out_or_err = str(result.get("output") or result.get("error") or "")
                # Trace tool execution.
                _trace_tool_execution(
                    tool_name=name,
                    args_preview=str(calls[idx_r].get("args") or {})[:500],
                    result_preview=out_or_err[:1000],
                    success=success,
                    error=str(result.get("error") or "")[:500] if not success else None,
                )
                suffix = ""
                if not success:
                    err = str(result.get("error") or "")
                    # Log the tool arguments so failures like run_command show the exact command.
                    try:
                        args_repr = json.dumps(calls[idx_r].get("args") or {}, ensure_ascii=False)
                    except Exception:
                        args_repr = repr(calls[idx_r].get("args") or {})
                    suffix = f" err={err[:200]} args={args_repr}"
                _log.info(
                    "ReAct tool result [%d] %s: success=%s output_len=%d%s",
                    idx_r,
                    name,
                    success,
                    len(out_or_err),
                    suffix,
                )
                await manage_tasks_progress.sync(tool_name=str(name or ""), result=result, ctx=ctx)
            for call, result in zip(tool_calls, results):
                success = bool(result.get("success"))
                err_text = str(result.get("error") or "")
                out_text = str(result.get("output") or "")
                output = out_text if success else f"Error: {err_text}"
                if success:
                    all_failed = False
                else:
                    await _emit_progress(
                        "tool_error",
                        "error",
                        f"Итерация {iterations_done}: ошибка инструмента {call.get('function', {}).get('name', '?')}: {err_text[:160]}",
                        iteration=iterations_done,
                    )
                    if failure_event_callback is not None:
                        try:
                            await failure_event_callback(
                                {
                                    "source": "agent_core.tool_call",
                                    "session_id": raw_session_id,
                                    "session_scoped_key": runtime_session_key,
                                    "task_id": str(task_id or ""),
                                    "tool_name": str(call.get("function", {}).get("name") or ""),
                                    "error": err_text,
                                    "iteration": int(iteration + 1),
                                }
                            )
                        except Exception:
                            _log.exception(
                                "failed to emit agent tool failure event session=%s task=%s tool=%s",
                                runtime_session_key,
                                task_id,
                                call.get("function", {}).get("name", "?"),
                            )
                    if err_text.startswith("Unknown tool:"):
                        unknown_tool = True
                blocked_by_policy = bool(result.get("blocked"))
                if blocked_by_policy:
                    block_reason = str(result.get("block_reason") or err_text or "blocked by policy")
                    has_blocked = True
                    blocked_count += 1
                    _log.warning(
                        "ReAct policy block tool=%s reason=%s",
                        call.get("function", {}).get("name", "?"),
                        block_reason[:300],
                    )
                    output += (
                        f"\n\n⛔ THIS COMMAND IS PERMANENTLY BLOCKED ({block_reason}). Do NOT retry it. "
                        "Find an alternative approach or inform the user this action is not allowed."
                    )
                working.append(
                    _compact_working_message(
                        {"role": "tool", "tool_call_id": call.get("id"), "content": output or "Success"}
                    )
                )
            for meta, result in zip(call_meta, results):
                out = result.get("output") if result.get("success") else None
                tool_facts.append(
                    {
                        "tool": meta.get("name"),
                        "args": meta.get("args"),
                        "success": bool(result.get("success")),
                        "error": result.get("error"),
                        # Keep a small preview of tool output for partial results / debugging.
                        "output_len": len(str(out or "")) if out is not None else 0,
                        "output_preview": _text_preview(out, max_chars=2000) if out is not None else "",
                    }
                )
            _maybe_persist_runtime_checkpoint(iterations_done, status="running")
            if unknown_tool:
                _log.warning("ReAct iter=%d unknown tool, stopping", iteration + 1)
                final_response = "Не могу выполнить без инструментов, уточните."
                final_status = "error"
                await _emit_progress(
                    "stop_unknown_tool",
                    "error",
                    f"Итерация {iterations_done}: неизвестный инструмент",
                    iteration=iterations_done,
                )
                break
            if batch_sig:
                if batch_sig == last_batch_sig:
                    repeated_batches += 1
                else:
                    repeated_batches = 0
                    last_batch_sig = batch_sig
                # If we keep retrying the exact same tool calls and they keep failing,
                # we are almost certainly stuck in a loop.
                if all_failed and repeated_batches >= 2:
                    last_err = ""
                    try:
                        last = next((t for t in reversed(tool_facts) if not bool(t.get("success"))), None)
                        if last:
                            last_err = str(last.get("error") or "")
                    except Exception:
                        last_err = ""
                    final_response = (
                        "Прогресс остановился: агент повторяет один и тот же вызов инструментов без успеха. "
                        "Останавливаюсь, чтобы не зацикливаться.\n\n"
                        "Последняя ошибка инструмента: "
                        + (_redact_runtime_digest_text(last_err)[:600] if last_err else "(нет деталей)")
                    )
                    final_status = "error"
                    await _emit_progress(
                        "stop_repeated_failures",
                        "error",
                        "Остановка: повтор одинаковых неуспешных вызовов инструментов",
                        iteration=iterations_done,
                    )
                    _maybe_persist_runtime_checkpoint(iterations_done, status=final_status, force=True)
                    break
            if blocked_count >= AGENT_MAX_BLOCKED:
                _log.warning("ReAct iter=%d blocked_count=%d, stopping", iteration + 1, blocked_count)
                final_response = (
                    "🚫 Stopped: Multiple blocked commands detected. "
                    "The requested actions are not allowed for security reasons."
                )
                final_status = "blocked"
                await _emit_progress(
                    "stop_blocked",
                    "blocked",
                    f"Итерация {iterations_done}: остановка из-за policy block",
                    iteration=iterations_done,
                )
                _maybe_persist_runtime_checkpoint(iterations_done, status=final_status, force=True)
                break
            if all_failed and not (content or "").strip():
                # Tool errors are generally recoverable: the LLM should see the failure and
                # choose an alternative (different tool, different command, missing dependency, etc).
                consecutive_all_failed += 1
                _log.warning(
                    "ReAct iter=%d all tools failed (consecutive=%d), continuing",
                    iteration + 1,
                    consecutive_all_failed,
                )
                if consecutive_all_failed >= 3:
                    last_err = ""
                    try:
                        last = next((t for t in reversed(tool_facts) if not bool(t.get("success"))), None)
                        if last:
                            last_err = str(last.get("error") or "")
                    except Exception:
                        last_err = ""
                    final_response = (
                        "Инструменты возвращают ошибки и прогресс остановился. "
                        "Последняя ошибка инструмента: "
                        + (_redact_runtime_digest_text(last_err)[:600] if last_err else "(нет деталей)")
                    )
                    final_status = "error"
                    await _emit_progress(
                        "stop_consecutive_failures",
                        "error",
                        "Остановка: несколько итераций подряд безуспешны",
                        iteration=iterations_done,
                    )
                    _maybe_persist_runtime_checkpoint(iterations_done, status=final_status, force=True)
                    break
                # Let the next iteration attempt recovery.
                await _maybe_extend_iteration_budget(iteration_no=iterations_done)
                iteration += 1
                continue
            consecutive_all_failed = 0
            if not has_blocked:
                blocked_count = 0
            await _maybe_extend_iteration_budget(iteration_no=iterations_done)
            iteration += 1
        if not final_response:
            _log.warning("ReAct max iterations reached (%d)", max_iterations)
            # This is not a hard error: return whatever we managed to collect so the orchestrator
            # can decide whether to continue/replan.
            recent = tool_facts[-6:]
            lines: List[str] = []
            lines.append(f"⚠️ Достигнут лимит итераций ({max_iterations}). Возвращаю промежуточный результат.")
            if recent:
                lines.append("")
                lines.append("Последние вызовы инструментов:")
                for t in recent:
                    tool = t.get("tool") or "?"
                    ok = bool(t.get("success"))
                    args = t.get("args") or {}
                    try:
                        args_s = json.dumps(args, ensure_ascii=False)
                    except Exception:
                        args_s = repr(args)
                    args_s = _redact_runtime_digest_text(args_s)
                    lines.append(f"- {tool}: success={ok} args={args_s}")
                    if ok:
                        prev = _redact_runtime_digest_text(t.get("output_preview")).strip()
                        if prev:
                            lines.append(prev)
                    else:
                        err = _redact_runtime_digest_text(t.get("error")).strip()
                        if err:
                            lines.append(f"error: {err[:400]}")
            final_response = "\n".join(lines).strip()
            final_status = "partial"
            await _emit_progress(
                "max_iterations",
                "partial",
                f"Достигнут лимит итераций ({max_iterations})",
                iteration=iterations_done,
            )
            _maybe_persist_runtime_checkpoint(iterations_done, status=final_status, force=True)
        if final_status == "ok":
            try:
                if any((not bool(t.get("success"))) for t in tool_facts):
                    final_status = "partial"
            except Exception:
                _log.exception(
                    "failed to normalize final status from tool facts session=%s task=%s",
                    runtime_session_key,
                    task_id,
                )
        date_str = time.strftime("%Y-%m-%d")
        history_key = task_id or "unknown"
        session.setdefault("history_by_task", {}).setdefault(history_key, []).append(
            {"user": f"[{date_str}] {user_message}", "assistant": final_response}
        )
        while len(session["history_by_task"][history_key]) > AGENT_MAX_HISTORY:
            session["history_by_task"][history_key].pop(0)
        async with self._get_session_lock(runtime_session_key):
            self._save_session(state_root, session)
            # Ensure next run reloads from disk instead of cached memory.
            self._sessions.pop(runtime_session_key, None)
            # Чистим обратный индекс от orphan-ключа
            if raw_session_id and raw_session_id != runtime_session_key:
                bucket = self._session_id_index.get(raw_session_id)
                if bucket is not None:
                    bucket.discard(runtime_session_key)
                    if not bucket:
                        del self._session_id_index[raw_session_id]
        _log.info("ReAct end session=%s task=%s status=%s iterations=%d tool_calls=%d response_len=%d",
                  runtime_session_key, task_id, final_status, iterations_done,
                  len(tool_facts), len(final_response))
        await _emit_progress(
            "final",
            final_status,
            f"ReAct завершен: status={final_status}, iterations={iterations_done}, tool_calls={len(tool_facts)}",
            iteration=iterations_done,
        )
        await _drain_lifecycle_events()
        claims, claims_source = await self._extract_structured_claims(
            text=final_response,
            status=final_status,
            model_name=_model_name,
        )
        return AgentRunResult(
            output=final_response,
            status=final_status,
            tool_calls=tool_facts,
            claims=claims,
            claims_source=claims_source,
        )

    def clear_session_cache(self, session_id: str) -> None:
        # Прямое совпадение (scoped-ключ или legacy bare id)
        self._sessions.pop(session_id, None)
        # Все scoped-ключи, связанные с raw session_id через обратный индекс
        for scoped in self._session_id_index.pop(session_id, set()):
            self._sessions.pop(scoped, None)


class AgentRunner:
    def __init__(self, config: AppConfig, tool_registry: PluginToolRegistry):
        self.config = config
        self._react = ReActAgent(config, tool_registry)

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._react.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._react.resolve_question(question_id, answer)

    def clear_session_cache(self, session_id: str) -> None:
        self._react.clear_session_cache(session_id)

    async def run(
        self,
        session: Any,
        user_text: str,
        bot: Any,
        context: Any,
        dest: Dict[str, Any],
        task_id: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        request_context: Optional[str] = None,
        constraints: Optional[str] = None,
        corr_id: Optional[str] = None,
        failure_event_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        progress_event_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        lifecycle_hook: Optional[AgentLifecycleHook] = None,
        cancel_event: Optional[Any] = None,
        observability: Optional[Any] = None,
        run_handle: Optional[Any] = None,
    ) -> "AgentRunResult":
        if not resolve_openai_config(self.config, model_key="openai_model", env_priority=True):
            _log.error("AgentRunner: OpenAI not configured")
            return AgentRunResult(
                output="Агент не настроен: отсутствуют OPENAI_API_KEY/OPENAI_MODEL.",
                status="error",
                tool_calls=[],
                claims=[],
            )
        chat_id = dest.get("chat_id")
        chat_type = dest.get("chat_type")
        return await self._react.run(
            session.id,
            user_text,
            session,
            bot,
            context,
            chat_id,
            chat_type,
            task_id,
            allowed_tools=allowed_tools,
            request_context=request_context,
            constraints=constraints,
            corr_id=corr_id,
            failure_event_callback=failure_event_callback,
            progress_event_callback=progress_event_callback,
            lifecycle_hook=lifecycle_hook,
            cancel_event=cancel_event,
            observability=observability,
            run_handle=run_handle,
        )


@dataclass
class AgentRunResult:
    output: str
    status: str = "ok"
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    claims: List[Dict[str, Any]] = field(default_factory=list)
    claims_source: str = ""
