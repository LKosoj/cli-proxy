from __future__ import annotations

from typing import Any, Dict
from types import SimpleNamespace
import os
import asyncio
import logging
import random

from .agent_core import AgentRunner
from .tooling.registry import ToolRegistry
from .contracts import ExecutorRequest, ExecutorResponse, validate_request, validate_response
from .profiles import ExecutorProfile


class Executor:
    def __init__(self, config, tool_registry: ToolRegistry):
        self._config = config
        self._tool_registry = tool_registry
        self._runner = AgentRunner(config, tool_registry)
        self._log = logging.getLogger(__name__)

    def _sandbox_root(self) -> str:
        return os.path.join(self._config.defaults.workdir, "_sandbox")

    def _session_workspace(self, session_id: str) -> str:
        return os.path.join(self._sandbox_root(), "sessions", session_id)

    def _is_transient_exc(self, e: BaseException) -> bool:
        if isinstance(e, (asyncio.TimeoutError, ConnectionError, OSError)):
            return True
        msg = str(e).lower()
        return any(k in msg for k in ["timeout", "timed out", "connection", "temporarily", "reset by peer"])

    async def _sleep_backoff(self, attempt: int) -> None:
        # jittered exponential backoff
        base = 0.6 * (2**attempt)
        await asyncio.sleep(base + random.random() * 0.2)

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
        start_ts = asyncio.get_running_loop().time()
        self._log.info("executor start corr_id=%s task_id=%s profile=%s goal=%r",
                       request.corr_id, request.task_id, profile.name, (request.goal or "")[:200])
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
            self._log.info("executor ask_user: question=%r options=%s", question[:100], options)
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
                "corr_id": request.corr_id,
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
            self._log.info(
                "executor end corr_id=%s status=%s elapsed_ms=%s",
                request.corr_id,
                resp.status,
                int((asyncio.get_running_loop().time() - start_ts) * 1000),
            )
            return resp
        # Пока используем текущий ReAct как исполнителя.
        self._log.info("executor launching ReAct agent: max_retries=%d timeout_ms=%d",
                       int(profile.max_retries), int(profile.timeout_ms))
        last_exc: Exception | None = None
        max_retries = max(0, int(profile.max_retries))
        timeout_ms = int(profile.timeout_ms)
        if request.deadline_ms:
            try:
                timeout_ms = min(timeout_ms, int(request.deadline_ms))
            except Exception:
                pass
        for attempt in range(max_retries + 1):
            try:
                run_result = await asyncio.wait_for(
                    self._runner.run(
                        proxy_session,
                        request.goal,
                        bot,
                        context,
                        dest,
                        task_id=request.task_id,
                        allowed_tools=profile.allowed_tools,
                        request_context=request.context,
                        constraints=request.constraints,
                        corr_id=request.corr_id,
                    ),
                    timeout=timeout_ms / 1000.0,
                )
                output = run_result.output
                resp = ExecutorResponse(
                    task_id=request.task_id,
                    status=run_result.status,
                    summary=(output[:400] + "...") if len(output) > 400 else output,
                    outputs=[{"type": "text", "content": output}],
                    tool_calls=run_result.tool_calls,
                    next_questions=[],
                )
                validate_response(resp)
                elapsed = int((asyncio.get_running_loop().time() - start_ts) * 1000)
                self._log.info(
                    "executor end corr_id=%s status=%s elapsed_ms=%d tool_calls=%d",
                    request.corr_id, resp.status, elapsed, len(run_result.tool_calls),
                )
                return resp
            except asyncio.TimeoutError as e:
                last_exc = e
                # Timeout is transient only if caller allows retries.
                if attempt < max_retries:
                    self._log.warning("executor timeout, retrying corr_id=%s attempt=%s", request.corr_id, attempt)
                    await self._sleep_backoff(attempt)
                    continue
                break
            except Exception as e:
                last_exc = e
                msg = str(e)
                self._log.warning("executor error corr_id=%s attempt=%d err=%s", request.corr_id, attempt, msg[:200])
                if "BLOCKED" in msg or "not allowed" in msg.lower():
                    self._log.warning("executor BLOCKED, stopping retries")
                    break
                if attempt < max_retries and self._is_transient_exc(e):
                    self._log.warning("transient error, retrying corr_id=%s attempt=%s err=%s", request.corr_id, attempt, e)
                    await self._sleep_backoff(attempt)
                    continue
                break

        resp = ExecutorResponse(
            task_id=request.task_id,
            status="error",
            summary=f"Ошибка выполнения: {last_exc}",
            outputs=[],
            tool_calls=[{"tool": "agent", "error": str(last_exc), "corr_id": request.corr_id}],
            next_questions=[],
        )
        validate_response(resp)
        elapsed = int((asyncio.get_running_loop().time() - start_ts) * 1000)
        self._log.error(
            "executor FAILED corr_id=%s elapsed_ms=%d error=%s",
            request.corr_id, elapsed, str(last_exc)[:300],
        )
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
