from __future__ import annotations

from typing import Any, Dict
from types import SimpleNamespace
import os

from .agent_core import AgentRunner
from .tooling.registry import ToolRegistry
from .contracts import ExecutorRequest, ExecutorResponse, validate_request, validate_response
from .profiles import ExecutorProfile


class Executor:
    def __init__(self, config):
        self._config = config
        self._runner = AgentRunner(config)
        self._tool_registry = ToolRegistry(config)

    def _sandbox_root(self) -> str:
        return os.path.join(self._config.defaults.workdir, "_sandbox")

    def _session_workspace(self, session_id: str) -> str:
        return os.path.join(self._sandbox_root(), "sessions", session_id)

    async def run(
        self,
        session: Any,
        request: ExecutorRequest,
        bot: Any,
        context: Any,
        dest: Dict[str, Any],
        profile: ExecutorProfile,
    ) -> ExecutorResponse:
        validate_request(request)
        # Явный needs_input через ask_user
        state_root = self._sandbox_root()
        session_workspace = self._session_workspace(session.id)
        os.makedirs(session_workspace, exist_ok=True)
        project_root = getattr(session, "project_root", None)
        agent_cwd = project_root or session_workspace
        proxy_session = SimpleNamespace(
            id=session.id,
            workdir=agent_cwd,
            state_root=state_root,
        )
        question = (request.inputs or {}).get("question")
        options = (request.inputs or {}).get("options")
        if options is not None and len(options) < 2:
            options = ["Да", "Нет"]
        if question and options:
            ctx = {
                "cwd": proxy_session.workdir,
                "state_root": proxy_session.state_root,
                "session_id": proxy_session.id,
                "chat_id": dest.get("chat_id"),
                "chat_type": dest.get("chat_type"),
                "bot": bot,
                "context": context,
                "session": proxy_session,
                "allowed_tools": profile.allowed_tools,
            }
            result = await self._tool_registry.execute("ask_user", {"question": question, "options": options}, ctx)
            if not result.get("success"):
                resp = ExecutorResponse(
                    task_id=request.task_id,
                    status="needs_input",
                    summary="Нужен ответ пользователя",
                    outputs=[],
                    tool_calls=[{"tool": "ask_user", "error": result.get("error")}],
                    next_questions=[question],
                )
                validate_response(resp)
                return resp
            resp = ExecutorResponse(
                task_id=request.task_id,
                status="ok",
                summary="Ответ пользователя получен",
                outputs=[{"type": "text", "content": result.get("output")}],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=[],
            )
            validate_response(resp)
            return resp
        # Пока используем текущий ReAct как исполнителя.
        output = await self._runner.run(proxy_session, request.goal, bot, context, dest, task_id=request.task_id)
        resp = ExecutorResponse(
            task_id=request.task_id,
            status="ok",
            summary=(output[:400] + "...") if len(output) > 400 else output,
            outputs=[{"type": "text", "content": output}],
            tool_calls=[],
            next_questions=[],
        )
        validate_response(resp)
        return resp

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._runner.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._runner.resolve_question(question_id, answer)

    def clear_session_cache(self, session_id: str) -> None:
        self._runner.clear_session_cache(session_id)

    def get_plugin_commands(self, profile: ExecutorProfile) -> Dict[str, Any]:
        return self._tool_registry.build_bot_commands(profile.allowed_tools)

    def get_plugin_ui(self, profile: ExecutorProfile) -> Dict[str, Any]:
        return self._tool_registry.build_bot_ui(profile.allowed_tools)
