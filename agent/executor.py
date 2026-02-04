from __future__ import annotations

from typing import Any, Dict

from . import agent_core as agent
from .contracts import ExecutorRequest, ExecutorResponse, validate_request, validate_response
from .profiles import ExecutorProfile


class Executor:
    def __init__(self, config):
        self._runner = agent.AgentRunner(config)
        self._tool_registry = agent.ToolRegistry(config)

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
        question = (request.inputs or {}).get("question")
        options = (request.inputs or {}).get("options")
        if options is not None and len(options) < 2:
            options = ["Да", "Нет"]
        if question and options:
            ctx = {
                "cwd": session.workdir,
                "session_id": session.id,
                "chat_id": dest.get("chat_id"),
                "chat_type": dest.get("chat_type"),
                "bot": bot,
                "context": context,
                "session": session,
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
        output = await self._runner.run(session, request.goal, bot, context, dest, task_id=request.task_id)
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
