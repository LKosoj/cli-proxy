import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent.openai_client import create_async_openai_client
from agent.tooling.registry import ToolRegistry as PluginToolRegistry
from agent.session_store import read_json_locked, write_json_locked

from config import AppConfig
from utils import strip_ansi, sandbox_root

_log = logging.getLogger(__name__)

# ==== config constants ====
AGENT_MAX_ITERATIONS = 15
AGENT_MAX_HISTORY = 20
AGENT_MAX_BLOCKED = 3
MAX_CHAT_MESSAGES = 2500
MAX_MEMORY_CHARS = 2000
CHAT_MESSAGE_LEN = 200
LOG_DETAILS_LEN = 100

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
SYSTEM_PROMPT_PATH = os.path.join(REPO_ROOT, "agent", "system.txt")
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


class ReActAgent:
    def __init__(self, config: AppConfig, tool_registry: PluginToolRegistry):
        self.config = config
        self._openai_cfg = _get_openai_config(config)
        self._openai_client = None
        if self._openai_cfg:
            api_key, _, base_url = self._openai_cfg
            self._openai_client = create_async_openai_client(api_key=api_key, base_url=base_url)
        self._sessions: Dict[str, Dict[str, Any]] = {}
        # ToolRegistry must be a singleton shared across executor/orchestrator/agent.
        self._tool_registry = tool_registry

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
        tool_names = ", ".join(self._tool_registry.list_tool_names())
        prompt = (
            prompt.replace("{{cwd}}", cwd)
            .replace("{{date}}", time.strftime("%Y-%m-%d"))
            .replace("{{tools}}", tool_names)
            .replace("{{userPorts}}", user_ports)
        )
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
            if "history" in data and "history_by_task" not in data:
                data["history_by_task"] = {"legacy": data.get("history", [])}
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
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        messages.append({"role": "system", "content": self._load_system_prompt(cwd, chat_id)})
        extra_parts: List[str] = []
        if corr_id:
            extra_parts.append(f"corr_id: {corr_id}")
        if request_context:
            extra_parts.append(f"<REQUEST_CONTEXT>\n{request_context}\n</REQUEST_CONTEXT>")
        if constraints:
            extra_parts.append(f"<CONSTRAINTS>\n{constraints}\n</CONSTRAINTS>")
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
        # Важно: модель должна видеть только разрешённые инструменты, иначе будет часто звать запрещённые.
        definitions = await self._tool_registry.get_definitions_async(allowed_tools or ["All"])
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
    ) -> "AgentRunResult":
        cwd = session_obj.workdir
        # Single source of truth for session state file:
        # always write/read /workdir/_sandbox/SESSION.json
        state_root = sandbox_root(self.config.defaults.workdir)
        os.makedirs(state_root, exist_ok=True)
        _log.info("ReAct start session=%s task=%s corr_id=%s msg=%r",
                  session_id, task_id, corr_id, user_message[:200])
        if session_id not in self._sessions:
            self._sessions[session_id] = self._load_session(state_root)
        session = self._sessions[session_id]
        working: List[Dict[str, Any]] = []
        final_response = ""
        final_status = "ok"
        blocked_count = 0
        tool_facts: List[Dict[str, Any]] = []
        iterations_done = 0
        consecutive_all_failed = 0

        def _text_preview(v: Any, max_chars: int = 2000) -> str:
            try:
                s = strip_ansi(str(v or ""))
            except Exception:
                s = ""
            if len(s) > max_chars:
                return s[:max_chars] + "...(truncated)"
            return s

        for iteration in range(AGENT_MAX_ITERATIONS):
            messages = self._build_messages(
                session,
                user_message,
                cwd,
                chat_id,
                working,
                task_id,
                request_context=request_context,
                constraints=constraints,
                corr_id=corr_id,
            )
            iterations_done = iteration + 1
            _log.info("ReAct iter=%d/%d calling LLM (messages=%d)", iterations_done, AGENT_MAX_ITERATIONS, len(messages))
            raw_message = await self._call_openai(messages, allowed_tools)
            tool_calls = raw_message.get("tool_calls") or []
            content = raw_message.get("content")
            if not tool_calls:
                final_response = (content or "").strip() or "(empty response)"
                _log.info("ReAct iter=%d no tool_calls, final text (%d chars)", iterations_done, len(final_response))
                break
            tool_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
            _log.info("ReAct iter=%d tool_calls=%d: %s", iteration + 1, len(tool_calls), ", ".join(tool_names))
            if content:
                _log.info("ReAct iter=%d LLM also said: %r", iteration + 1, content[:200])
            working.append({"role": raw_message.get("role"), "content": content, "tool_calls": tool_calls})
            has_blocked = False
            unknown_tool = False
            all_failed = True
            ctx = {
                "cwd": cwd,
                "state_root": state_root,
                "session_id": session_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "bot": bot,
                "context": context,
                "session": session_obj,
                "allowed_tools": allowed_tools or ["All"],
                "corr_id": corr_id,
            }
            calls = []
            call_meta: List[Dict[str, Any]] = []
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
                calls.append({"name": name, "args": args})
                call_meta.append({"name": name, "args": args})
            results = await self._tool_registry.execute_many(calls, ctx)
            for idx_r, (call, result) in enumerate(zip(tool_calls, results)):
                name = calls[idx_r]["name"]
                success = bool(result.get("success"))
                out_or_err = str(result.get("output") or result.get("error") or "")
                suffix = ""
                if not success:
                    err = str(result.get("error") or "")
                    # Log the tool arguments so failures like run_command show the exact command.
                    try:
                        args_repr = json.dumps(calls[idx_r].get("args") or {}, ensure_ascii=False)
                    except Exception:
                        args_repr = repr(calls[idx_r].get("args") or {})
                    if len(args_repr) > 500:
                        args_repr = args_repr[:500] + "...(truncated)"
                    suffix = f" err={err[:200]} args={args_repr}"
                _log.info(
                    "ReAct tool result [%d] %s: success=%s output_len=%d%s",
                    idx_r,
                    name,
                    success,
                    len(out_or_err),
                    suffix,
                )
            for call, result in zip(tool_calls, results):
                output = result.get("output") if result.get("success") else f"Error: {result.get('error')}"
                if result.get("success"):
                    all_failed = False
                else:
                    err_text = str(result.get("error") or "")
                    if err_text.startswith("Unknown tool:"):
                        unknown_tool = True
                if output and "BLOCKED:" in output:
                    has_blocked = True
                    blocked_count += 1
                    output += (
                        "\n\n⛔ THIS COMMAND IS PERMANENTLY BLOCKED. Do NOT retry it. "
                        "Find an alternative approach or inform the user this action is not allowed."
                    )
                working.append({"role": "tool", "tool_call_id": call.get("id"), "content": output or "Success"})
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
            if unknown_tool:
                _log.warning("ReAct iter=%d unknown tool, stopping", iteration + 1)
                final_response = "Не могу выполнить без инструментов, уточните."
                final_status = "error"
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
                        + (last_err[:600] if last_err else "(нет деталей)")
                    )
                    final_status = "error"
                    break
                # Let the next iteration attempt recovery.
                continue
            consecutive_all_failed = 0
            if blocked_count >= AGENT_MAX_BLOCKED:
                _log.warning("ReAct iter=%d blocked_count=%d, stopping", iteration + 1, blocked_count)
                final_response = (
                    "🚫 Stopped: Multiple blocked commands detected. "
                    "The requested actions are not allowed for security reasons."
                )
                final_status = "blocked"
                break
            if not has_blocked:
                blocked_count = 0
        if not final_response:
            _log.warning("ReAct max iterations reached (%d)", AGENT_MAX_ITERATIONS)
            # This is not a hard error: return whatever we managed to collect so the orchestrator
            # can decide whether to continue/replan.
            recent = tool_facts[-6:]
            lines: List[str] = []
            lines.append(f"⚠️ Достигнут лимит итераций ({AGENT_MAX_ITERATIONS}). Возвращаю промежуточный результат.")
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
                    if len(args_s) > 300:
                        args_s = args_s[:300] + "...(truncated)"
                    lines.append(f"- {tool}: success={ok} args={args_s}")
                    if ok:
                        prev = (t.get("output_preview") or "").strip()
                        if prev:
                            lines.append(prev)
                    else:
                        err = str(t.get("error") or "").strip()
                        if err:
                            lines.append(f"error: {err[:400]}")
            final_response = "\n".join(lines).strip()
            final_status = "partial"
        if final_status == "ok":
            try:
                if any((not bool(t.get("success"))) for t in tool_facts):
                    final_status = "partial"
            except Exception:
                pass
        date_str = time.strftime("%Y-%m-%d")
        history_key = task_id or "unknown"
        session.setdefault("history_by_task", {}).setdefault(history_key, []).append(
            {"user": f"[{date_str}] {user_message}", "assistant": final_response}
        )
        while len(session["history_by_task"][history_key]) > AGENT_MAX_HISTORY:
            session["history_by_task"][history_key].pop(0)
        self._save_session(state_root, session)
        # Ensure next run reloads from disk instead of cached memory.
        self._sessions.pop(session_id, None)
        _log.info("ReAct end session=%s task=%s status=%s iterations=%d tool_calls=%d response_len=%d",
                  session_id, task_id, final_status, iterations_done,
                  len(tool_facts), len(final_response))
        return AgentRunResult(output=final_response, status=final_status, tool_calls=tool_facts)

    def clear_session_cache(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


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
    ) -> "AgentRunResult":
        if not _get_openai_config(self.config):
            _log.error("AgentRunner: OpenAI not configured")
            return AgentRunResult(
                output="Агент не настроен: отсутствуют OPENAI_API_KEY/OPENAI_MODEL.",
                status="error",
                tool_calls=[],
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
        )


@dataclass
class AgentRunResult:
    output: str
    status: str = "ok"
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
