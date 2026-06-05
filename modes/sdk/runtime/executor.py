from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Optional
from types import SimpleNamespace
import os
import asyncio
import logging
import random
import re

from app.services.runtime_progress_service import emit_runtime_progress
from i18n import t
from sessions.scoped_key import session_scoped_key
from sessions.session_state_access import get_active_mode
from utils.lang import resolve_user_lang
from .agent_core import AgentRunner
from .tooling.registry import ToolRegistry
from .contracts import ExecutorRequest, ExecutorResponse, validate_request, validate_response
from .events import EventSeverity, EventType, OrchestratorEvent
from .lifecycle_hooks import AgentLifecycleEvent
from .profiles import ExecutorProfile
from .reactions import ReactionAction, ReactionEngine, ReactionRule
from .memory_store import chat_workspace_root


class Executor:
    def __init__(self, config, tool_registry: ToolRegistry):
        self._config = config
        self._tool_registry = tool_registry
        self._runner = AgentRunner(config, tool_registry)
        self._log = logging.getLogger(__name__)
        self._reaction_engine = ReactionEngine(logger=self._log)

    def _build_failure_rules(self, *, max_retries: int) -> list[ReactionRule]:
        safe_retries = max(0, int(max_retries))
        return [
            ReactionRule(
                rule_id="executor.retry_on_failed_step",
                event_types=[EventType.STEP_FAILED],
                min_severity=EventSeverity.ERROR,
                actions=[
                    ReactionAction(action_type="retry_step", params={"max_retries": safe_retries}),
                    ReactionAction(action_type="notify_failure"),
                ],
            )
        ]

    async def _emit_failure_reactions(
        self,
        *,
        session_id: str,
        task_id: str,
        summary: str,
        retry_count: int,
        max_retries: int,
        payload: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        event = OrchestratorEvent(
            event_type=EventType.STEP_FAILED,
            severity=EventSeverity.ERROR,
            session_id=str(session_id or ""),
            step_id=str(task_id or ""),
            message=str(summary or ""),
            payload={
                "retry_count": int(retry_count),
                **dict(payload or {}),
            },
        )
        try:
            return await self._reaction_engine.execute(
                event,
                self._build_failure_rules(max_retries=max_retries),
                ctx={"session_id": str(session_id or ""), "task_id": str(task_id or "")},
            )
        except Exception:
            self._log.exception(
                "executor failure reactions execution failed corr_task=%s session=%s",
                task_id,
                session_id,
            )
            return []

    async def _emit_agent_tool_failure_event(self, event_payload: Dict[str, Any]) -> None:
        session_id = str((event_payload or {}).get("session_id") or "")
        task_id = str((event_payload or {}).get("task_id") or "")
        tool_name = str((event_payload or {}).get("tool_name") or "")
        tool_error = str((event_payload or {}).get("error") or "")
        await self._emit_failure_reactions(
            session_id=session_id,
            task_id=task_id,
            summary=f"Tool failed: {tool_name}",
            retry_count=0,
            max_retries=0,
            payload={
                "source": str((event_payload or {}).get("source") or "agent_core.tool_call"),
                "tool_name": tool_name,
                "error": tool_error,
                "iteration": int((event_payload or {}).get("iteration") or 0),
            },
        )

    async def _emit_agent_progress_event(self, session: Any, event_payload: Dict[str, Any]) -> None:
        mode_id = str(get_active_mode(session, "") or "").strip()
        payload = {
            "mode_id": mode_id,
            "source": str((event_payload or {}).get("source") or "agent_core"),
            "phase": str((event_payload or {}).get("phase") or "event"),
            "status": str((event_payload or {}).get("status") or "running"),
            "message": str((event_payload or {}).get("message") or ""),
            "corr_id": str((event_payload or {}).get("corr_id") or ""),
            "task_id": str((event_payload or {}).get("task_id") or ""),
            "step_id": str((event_payload or {}).get("step_id") or ""),
            "iteration": int((event_payload or {}).get("iteration") or 0),
        }
        emit_runtime_progress(session, payload)

    async def _emit_agent_lifecycle_event(
        self,
        session: Any,
        event: AgentLifecycleEvent,
        *,
        observability: Optional[Any] = None,
        run_handle: Optional[Any] = None,
    ) -> None:
        redacted = event.redacted_copy()
        mode_id = redacted.mode_id or str(get_active_mode(session, "") or "").strip()
        if mode_id != redacted.mode_id:
            redacted = replace(redacted, mode_id=mode_id)
        if redacted.event_type == "runtime_progress":
            emit_runtime_progress(session, redacted.to_runtime_progress_payload())
            if getattr(session, "_run_observability_bridge", None) is not None:
                return
            if observability is None or run_handle is None:
                return
            try:
                observability.record_runtime_progress(run_handle, event=redacted.to_runtime_progress_payload())
            except Exception:
                self._log.exception(
                    "executor lifecycle progress persistence failed corr_id=%s task_id=%s",
                    redacted.corr_id,
                    redacted.task_id,
                )
            return
        if observability is None or run_handle is None:
            return
        try:
            observability.artifact_store.append_event(run_handle, redacted.to_artifact_event())
        except Exception:
            self._log.exception(
                "executor lifecycle event persistence failed corr_id=%s task_id=%s event=%s",
                redacted.corr_id,
                redacted.task_id,
                redacted.event_type,
            )

    @staticmethod
    def _should_retry_from_reaction_results(results: list[Dict[str, Any]]) -> bool:
        for item in results:
            if str(item.get("action") or "") == "retry_step" and str(item.get("status") or "") == "queued":
                return True
        return False

    def _sandbox_root(self) -> str:
        return os.path.join(self._config.defaults.workdir, "_sandbox")

    def _session_workspace(self, session_id: str) -> str:
        return os.path.join(self._sandbox_root(), "sessions", session_id)

    def _chat_workspace(self, chat_id: Any) -> str:
        return chat_workspace_root(self._config.defaults.workdir, chat_id)

    def _is_transient_exc(self, e: BaseException) -> bool:
        if isinstance(e, (asyncio.TimeoutError, ConnectionError, OSError)):
            return True
        msg = str(e).lower()
        return any(k in msg for k in ["timeout", "timed out", "connection", "temporarily", "reset by peer"])

    async def _sleep_backoff(self, attempt: int) -> None:
        # jittered exponential backoff
        base = 0.6 * (2**attempt)
        await asyncio.sleep(base + random.random() * 0.2)

    @staticmethod
    def _claim_status_for_response(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized == "ok":
            return "confirmed"
        if normalized in {"partial", "needs_input"}:
            return "needs_check"
        if normalized in {"error", "blocked", "timeout"}:
            return "unconfirmed"
        return "needs_check"

    @staticmethod
    def _candidate_claim_texts(summary: str, outputs: list[Dict[str, Any]]) -> list[str]:
        candidates: list[str] = []

        def _normalize(text: str) -> str:
            value = " ".join(str(text or "").split()).strip(" -\t\r\n")
            return value

        def _append(value: str) -> None:
            text = _normalize(value)
            if not text:
                return
            if len(text) < 12:
                return
            candidates.append(text)

        _append(summary)
        for output in outputs or []:
            if not isinstance(output, dict):
                continue
            if str(output.get("type") or "").strip() != "text":
                continue
            content = str(output.get("content") or "").strip()
            if not content:
                continue
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            bullet_lines = [
                line for line in lines
                if line.startswith(("- ", "* ", "• ")) or re.match(r"^\d+[.)]\s+", line)
            ]
            if len(bullet_lines) >= 2:
                for line in bullet_lines[:8]:
                    text = re.sub(r"^(\d+[.)]\s+|[-*•]\s+)", "", line).strip()
                    _append(text)
                continue
            segments = re.split(r"(?<=[.!?;])\s+|\n+", content)
            for segment in segments[:8]:
                _append(segment)

        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:8]

    def _build_claims(self, *, status: str, summary: str, outputs: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        texts = self._candidate_claim_texts(summary, outputs)
        claim_status = self._claim_status_for_response(status)
        evidence: list[Dict[str, Any]] = []
        for output in outputs or []:
            if not isinstance(output, dict):
                continue
            preview = str(output.get("content") or "").strip()
            evidence.append(
                {
                    "type": str(output.get("type") or "text").strip() or "text",
                    "path": str(output.get("path") or output.get("file_path") or "").strip(),
                    "preview": preview,
                }
            )
        claims: list[Dict[str, Any]] = []
        for idx, text in enumerate(texts, start=1):
            claims.append(
                {
                    "claim_id": f"claim_{idx}",
                    "status": claim_status,
                    "text": text,
                    "evidence": evidence[:5],
                }
            )
        return claims

    async def run(
        self,
        session: Any,
        request: ExecutorRequest,
        bot: Any,
        context: Any,
        dest: Dict[str, Any],
        profile: ExecutorProfile,
        cancel_event: Optional[Any] = None,
        observability: Optional[Any] = None,
        run_handle: Optional[Any] = None,
    ) -> ExecutorResponse:
        validate_request(request)
        start_ts = asyncio.get_running_loop().time()
        mode_id = str(get_active_mode(session, "") or "").strip()
        self._log.info("executor start corr_id=%s task_id=%s profile=%s goal=%r",
                       request.corr_id, request.task_id, profile.name, (request.goal or "")[:200])
        chat_id = dest.get("chat_id")
        _lang = resolve_user_lang(self._config, chat_id=chat_id)
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "executor",
                "phase": "start",
                "status": "running",
                "corr_id": str(request.corr_id or ""),
                "task_id": str(request.task_id or ""),
                "message": t("executor.progress.start", _lang, profile=profile.name),
            },
        )
        # Явный needs_input через ask_user
        scoped_key = session_scoped_key(session) or str(getattr(session, "id", "") or "").strip()
        state_root = self._chat_workspace(chat_id)
        session_workspace = self._session_workspace(scoped_key or session.id)
        os.makedirs(session_workspace, exist_ok=True)
        project_root = getattr(session, "project_root", None)
        session_workdir = getattr(session, "workdir", None)
        agent_cwd = project_root or session_workdir or session_workspace
        proxy_session = SimpleNamespace(
            id=session.id,
            scoped_key=scoped_key,
            active_mode=mode_id,
            workdir=agent_cwd,
            state_root=state_root,
            tool_session=session,
        )
        question = (request.inputs or {}).get("question")
        options = (request.inputs or {}).get("options")
        if options is not None and len(options) < 2:
            options = [t("common.yes", _lang), t("common.no", _lang)]
        if question and options:
            self._log.info("executor ask_user: question=%r options=%s", question[:100], options)
            emit_runtime_progress(
                session,
                {
                    "mode_id": mode_id,
                    "source": "executor",
                    "phase": "ask_user",
                    "status": "needs_input",
                    "corr_id": str(request.corr_id or ""),
                    "task_id": str(request.task_id or ""),
                    "message": t("executor.progress.ask_user", _lang),
                },
            )
            ctx = {
                "cwd": proxy_session.workdir,
                "state_root": proxy_session.state_root,
                "session_id": proxy_session.id,
                "session_scoped_key": scoped_key,
                "chat_id": dest.get("chat_id"),
                "chat_type": dest.get("chat_type"),
                "bot": bot,
                "context": context,
                # Pass the real session object so tools that require full session API
                # (for example, use_cli -> session.run_prompt) remain compatible.
                "session": session,
                "allowed_tools": profile.allowed_tools,
                "corr_id": request.corr_id,
            }
            try:
                result = await self._tool_registry.execute("ask_user", {"question": question, "options": options}, ctx)
            except Exception:
                self._log.exception(
                    "executor ask_user failed with exception corr_id=%s task_id=%s",
                    request.corr_id,
                    request.task_id,
                )
                result = {"success": False, "error": "ask_user execution failed"}
            if not result.get("success"):
                emit_runtime_progress(
                    session,
                    {
                        "mode_id": mode_id,
                        "source": "executor",
                        "phase": "ask_user_failed",
                        "status": "error",
                        "corr_id": str(request.corr_id or ""),
                        "task_id": str(request.task_id or ""),
                        "message": str(result.get("error") or "ask_user execution failed"),
                    },
                )
                reaction_results = await self._emit_failure_reactions(
                    session_id=str(session.id or ""),
                    task_id=str(request.task_id or ""),
                    summary="ask_user tool failed",
                    retry_count=0,
                    max_retries=0,
                    payload={
                        "source": "executor.ask_user",
                        "status": "needs_input",
                        "error": str(result.get("error") or ""),
                    },
                )
                resp = ExecutorResponse(
                    task_id=request.task_id,
                    status="needs_input",
                    summary=t("executor.progress.needs_input", _lang),
                    outputs=[],
                    tool_calls=[{"tool": "ask_user", "error": result.get("error"), "reactions": reaction_results}],
                    next_questions=[question],
                )
                validate_response(resp)
                return resp
            resp = ExecutorResponse(
                task_id=request.task_id,
                status="ok",
                summary=t("executor.progress.user_answered", _lang),
                outputs=[{"type": "text", "content": result.get("output")}],
                claims=self._build_claims(
                    status="ok",
                    summary=t("executor.progress.user_answered", _lang),
                    outputs=[{"type": "text", "content": result.get("output")}],
                ),
                claims_source="native_tool",
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
            emit_runtime_progress(
                session,
                {
                    "mode_id": mode_id,
                    "source": "executor",
                    "phase": "final",
                    "status": str(resp.status or "ok"),
                    "corr_id": str(request.corr_id or ""),
                    "task_id": str(request.task_id or ""),
                    "message": t("executor.progress.user_answered", _lang),
                },
            )
            return resp
        # Пока используем текущий ReAct как исполнителя.
        self._log.info("executor launching ReAct agent: max_retries=%d timeout_ms=%d",
                       int(profile.max_retries), int(profile.timeout_ms))
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "executor",
                "phase": "launch_react",
                "status": "running",
                "corr_id": str(request.corr_id or ""),
                "task_id": str(request.task_id or ""),
                "message": t("executor.progress.launch_react", _lang, retries=int(profile.max_retries), timeout_ms=int(profile.timeout_ms)),
            },
        )
        last_exc: Exception | None = None
        max_retries = max(0, int(profile.max_retries))
        timeout_ms = int(profile.timeout_ms)
        if request.deadline_ms:
            try:
                timeout_ms = min(timeout_ms, int(request.deadline_ms))
            except Exception:
                self._log.exception(
                    "executor failed to apply deadline_ms corr_id=%s deadline=%r",
                    request.corr_id,
                    request.deadline_ms,
                )
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
                        failure_event_callback=self._emit_agent_tool_failure_event,
                        lifecycle_hook=lambda event: self._emit_agent_lifecycle_event(
                            session,
                            event,
                            observability=observability,
                            run_handle=run_handle,
                        ),
                        cancel_event=cancel_event,
                        observability=observability,
                        run_handle=run_handle,
                    ),
                    timeout=timeout_ms / 1000.0,
                )
                output = run_result.output
                reaction_results: list[Dict[str, Any]] = []
                if run_result.status in ("error", "blocked"):
                    reaction_results = await self._emit_failure_reactions(
                        session_id=str(session.id or ""),
                        task_id=str(request.task_id or ""),
                        summary=output or "executor step failed",
                        retry_count=attempt,
                        max_retries=max_retries,
                        payload={
                            "source": "executor.runner",
                            "status": str(run_result.status or ""),
                            "corr_id": str(request.corr_id or ""),
                        },
                    )
                    if (
                        attempt < max_retries
                        and self._should_retry_from_reaction_results(reaction_results)
                    ):
                        self._log.warning(
                            "executor retry by reactions corr_id=%s attempt=%d",
                            request.corr_id,
                            attempt,
                        )
                        await self._sleep_backoff(attempt)
                        continue
                claims = list(getattr(run_result, "claims", []) or [])
                claims_source = str(getattr(run_result, "claims_source", "") or "").strip()
                if not claims:
                    claims = self._build_claims(
                        status=run_result.status,
                        summary=output,
                        outputs=[{"type": "text", "content": output}],
                    )
                    claims_source = "text_fallback"
                resp = ExecutorResponse(
                    task_id=request.task_id,
                    status=run_result.status,
                    summary=output,
                    outputs=[{"type": "text", "content": output}],
                    claims=claims,
                    claims_source=claims_source,
                    tool_calls=run_result.tool_calls
                    + (
                        [{"tool": "reactions_v2", "results": reaction_results}]
                        if reaction_results
                        else []
                    ),
                    next_questions=[],
                )
                validate_response(resp)
                elapsed = int((asyncio.get_running_loop().time() - start_ts) * 1000)
                self._log.info(
                    "executor end corr_id=%s status=%s elapsed_ms=%d tool_calls=%d",
                    request.corr_id, resp.status, elapsed, len(run_result.tool_calls),
                )
                emit_runtime_progress(
                    session,
                    {
                        "mode_id": mode_id,
                        "source": "executor",
                        "phase": "final",
                        "status": str(resp.status or "ok"),
                        "corr_id": str(request.corr_id or ""),
                        "task_id": str(request.task_id or ""),
                        "message": t("executor.progress.done", _lang, status=resp.status),
                    },
                )
                return resp
            except asyncio.TimeoutError as e:
                last_exc = e
                self._log.exception(
                    "executor timeout corr_id=%s attempt=%d timeout_ms=%d",
                    request.corr_id,
                    attempt,
                    timeout_ms,
                )
                reaction_results = await self._emit_failure_reactions(
                    session_id=str(session.id or ""),
                    task_id=str(request.task_id or ""),
                    summary=f"Timeout: {e}",
                    retry_count=attempt,
                    max_retries=max_retries,
                    payload={
                        "source": "executor.timeout",
                        "status": "timeout",
                        "corr_id": str(request.corr_id or ""),
                    },
                )
                # Timeout is transient only if caller allows retries.
                emit_runtime_progress(
                    session,
                    {
                        "mode_id": mode_id,
                        "source": "executor",
                        "phase": "timeout",
                        "status": "error",
                        "corr_id": str(request.corr_id or ""),
                        "task_id": str(request.task_id or ""),
                        "message": f"Timeout attempt={attempt + 1} timeout_ms={timeout_ms}",
                    },
                )
                if (
                    attempt < max_retries
                    and self._should_retry_from_reaction_results(reaction_results)
                ):
                    self._log.warning("executor timeout, retrying corr_id=%s attempt=%s", request.corr_id, attempt)
                    await self._sleep_backoff(attempt)
                    continue
                break
            except Exception as e:
                last_exc = e
                msg = str(e)
                self._log.exception(
                    "executor error corr_id=%s attempt=%d err=%s",
                    request.corr_id,
                    attempt,
                    msg[:200],
                )
                reaction_results = await self._emit_failure_reactions(
                    session_id=str(session.id or ""),
                    task_id=str(request.task_id or ""),
                    summary=f"Exception: {msg}",
                    retry_count=attempt,
                    max_retries=max_retries,
                    payload={
                        "source": "executor.exception",
                        "status": "error",
                        "corr_id": str(request.corr_id or ""),
                    },
                )
                emit_runtime_progress(
                    session,
                    {
                        "mode_id": mode_id,
                        "source": "executor",
                        "phase": "exception",
                        "status": "error",
                        "corr_id": str(request.corr_id or ""),
                        "task_id": str(request.task_id or ""),
                        "message": f"Exception attempt={attempt + 1}: {msg[:180]}",
                    },
                )
                if "BLOCKED" in msg or "not allowed" in msg.lower():
                    self._log.warning("executor BLOCKED, stopping retries")
                    break
                if (
                    attempt < max_retries
                    and self._is_transient_exc(e)
                    and self._should_retry_from_reaction_results(reaction_results)
                ):
                    self._log.warning("transient error, retrying corr_id=%s attempt=%s err=%s", request.corr_id, attempt, e)
                    await self._sleep_backoff(attempt)
                    continue
                break

        resp = ExecutorResponse(
            task_id=request.task_id,
            status="error",
            summary=t("executor.execution_error", _lang, error=last_exc),
            outputs=[],
            claims=[],
            tool_calls=[{"tool": "agent", "error": str(last_exc), "corr_id": request.corr_id}],
            next_questions=[],
        )
        validate_response(resp)
        elapsed = int((asyncio.get_running_loop().time() - start_ts) * 1000)
        self._log.error(
            "executor FAILED corr_id=%s elapsed_ms=%d error=%s",
            request.corr_id, elapsed, str(last_exc)[:300],
        )
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "executor",
                "phase": "final",
                "status": "error",
                "corr_id": str(request.corr_id or ""),
                "task_id": str(request.task_id or ""),
                "message": t("executor.progress.error", _lang, error=str(last_exc)[:180]),
            },
        )
        return resp

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._runner.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._runner.resolve_question(question_id, answer)

    def clear_session_cache(self, session_id: str) -> None:
        self._runner.clear_session_cache(session_id)

    def get_plugin_ui(self, profile: ExecutorProfile) -> Dict[str, Any]:
        return self._tool_registry.build_bot_ui(profile.allowed_tools)
