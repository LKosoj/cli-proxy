import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from openai import AsyncOpenAI

from agent.tooling.registry import ToolRegistry as PluginToolRegistry

from config import AppConfig
from utils import strip_ansi

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
    def __init__(self, config: AppConfig):
        self.config = config
        self._openai_cfg = _get_openai_config(config)
        self._openai_client = None
        if self._openai_cfg:
            api_key, _, base_url = self._openai_cfg
            self._openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._tool_registry = PluginToolRegistry(config)

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
        memory_content = get_memory_for_prompt(state_root)
        if memory_content:
            prompt += f"\n\n<MEMORY>\nNotes from previous sessions (use \"memory\" tool to update):\n{memory_content}\n</MEMORY>"
        chat_history = get_chat_history(chat_id)
        if chat_history:
            line_count = len([l for l in chat_history.split("\n") if l.strip()])
            prompt += f"\n\n<RECENT_CHAT>\nИстория чата ({line_count} сообщений). ЭТО ВСЁ что у тебя есть - от самых старых к новым:\n{chat_history}\n</RECENT_CHAT>"
        return prompt

    def _load_session(self, state_root: str) -> Dict[str, Any]:
        path = os.path.join(state_root, "SESSION.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("history_by_task", {})
                    if "history" in data and "history_by_task" not in data:
                        data["history_by_task"] = {"legacy": data.get("history", [])}
                    return data
            except Exception:
                return {"history_by_task": {}}
        return {"history_by_task": {}}

    def _save_session(self, state_root: str, session: Dict[str, Any]) -> None:
        path = os.path.join(state_root, "SESSION.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)

    def _build_messages(
        self,
        session: Dict[str, Any],
        user_message: str,
        state_root: str,
        chat_id: Optional[int],
        working: List[Dict[str, Any]],
        task_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        messages.append({"role": "system", "content": self._load_system_prompt(state_root, chat_id)})
        task_history = session.get("history_by_task", {}).get(task_id or "unknown", [])
        for conv in task_history:
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
        definitions = self._tool_registry.get_definitions(["All"])
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
    ) -> str:
        cwd = session_obj.workdir
        state_root = getattr(session_obj, "state_root", cwd)
        if session_id not in self._sessions:
            self._sessions[session_id] = self._load_session(state_root)
        session = self._sessions[session_id]
        working: List[Dict[str, Any]] = []
        final_response = ""
        blocked_count = 0
        for iteration in range(AGENT_MAX_ITERATIONS):
            messages = self._build_messages(session, user_message, state_root, chat_id, working, task_id)
            raw_message = await self._call_openai(messages)
            tool_calls = raw_message.get("tool_calls") or []
            content = raw_message.get("content")
            if not tool_calls:
                final_response = (content or "").strip() or "(empty response)"
                break
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
            }
            calls = []
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
            results = await self._tool_registry.execute_many(calls, ctx)
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
                    output += "\n\n⛔ THIS COMMAND IS PERMANENTLY BLOCKED. Do NOT retry it. Find an alternative approach or inform the user this action is not allowed."
                working.append({"role": "tool", "tool_call_id": call.get("id"), "content": output or "Success"})
            if unknown_tool:
                final_response = "Не могу выполнить без инструментов, уточните."
                break
            if all_failed and not (content or "").strip():
                final_response = "Не могу выполнить без инструментов, уточните."
                break
            if blocked_count >= AGENT_MAX_BLOCKED:
                final_response = "🚫 Stopped: Multiple blocked commands detected. The requested actions are not allowed for security reasons."
                break
            if not has_blocked:
                blocked_count = 0
        if not final_response:
            final_response = "⚠️ Max iterations reached"
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
        return final_response

    def clear_session_cache(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class AgentRunner:
    def __init__(self, config: AppConfig):
        self.config = config
        self._react = ReActAgent(config)

    async def run(
        self,
        session: Any,
        user_text: str,
        bot: Any,
        context: Any,
        dest: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> str:
        if not _get_openai_config(self.config):
            return "Агент не настроен: отсутствуют OPENAI_API_KEY/OPENAI_MODEL."
        chat_id = dest.get("chat_id")
        chat_type = dest.get("chat_type")
        return await self._react.run(session.id, user_text, session, bot, context, chat_id, chat_type, task_id)

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._react.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._react.resolve_question(question_id, answer)

    def clear_session_cache(self, session_id: str) -> None:
        self._react.clear_session_cache(session_id)
