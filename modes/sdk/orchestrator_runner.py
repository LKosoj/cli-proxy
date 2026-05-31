from __future__ import annotations

import asyncio
import dataclasses
import difflib
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from app.services.runtime_progress_service import emit_runtime_progress
from app.services.trace_contract import build_trace_event
from config import AppConfig
from modes.sdk.runtime.memory_store import ensure_chat_workspace
from session import session_runtime_uid
from sessions.conversation_scope import ConversationScope
from sessions.session_state_access import get_active_mode
from .runtime.cli_contracts import (
    CLIResponseFormat,
    CLIOutputType,
    collect_repo_review_runtime_gaps_from_outputs,
    degraded_mode_output,
    obligation_review_bundle_to_outputs,
    parse_bundle_for_response_format,
    strip_outer_code_fence,
    wrap_prompt_for_response_format,
)
from .runtime.cli_review_prompts import (
    build_followup_repo_final_review_prompt,
    build_gap_closure_prompt,
    build_repo_final_review_instruction,
)
from .runtime.final_qc import (
    REPO_GAP_LABELS,
    apply_runtime_readiness,
    build_assessment_schema,
    build_open_gaps_text,
    collect_implementation_handoff_gaps,
    collect_placeholder_gaps,
)
from .runtime.evidence_pipeline import (
    claim_has_repo_anchor,
    collect_step_evidence,
    verify_claim_ledger,
)
from .runtime.claim_ledger import normalize_claim_ledger, validate_claim_ledger
from .runtime.obligations import (
    build_obligation_matrix,
    build_task_contract,
    collect_open_blocking_obligations,
    split_obligations_by_blocking,
)
from .runtime.document_lint import (
    lint_markdown_document,
    repair_markdown_document,
    render_document_lint_report,
)
from .runtime.analyst_quality_metrics import build_analyst_quality_metrics
from .runtime.ask_user_generation import (
    ASK_USER_CLARIFICATION_SYSTEM,
    build_validated_ask_payload,
)
from .runtime.ask_user_schema import apply_ask_schema, is_non_semantic_ask_answer
from .runtime.cli_retry import run_cli_with_retry
from .runtime.json_normalizer import loads_safe, parse_normalize_validate
from .runtime.events import EventSeverity, EventType, OrchestratorEvent
from .runtime.reactions import ReactionAction, ReactionEngine, ReactionRule
from utils.text import strip_ansi
from .orchestrator_deps import OrchestratorDeps, load_default_deps

_ANALYST_INTENT_FLAGS_SESSION_ATTR = "analyst_intent_flags"
_ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR = "analyst_blocking_clarification_runtime"
_ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR = "analyst_blocking_clarification_text"
_SESSION_TASK_MODE_ID = "__session__"
_CLI_PROMPT_ARTIFACT_THRESHOLD_BYTES = 64 * 1024


def _is_stable_deterministic_analyst_plan(steps: List[Any]) -> bool:
    plan_ids = {
        str(getattr(step, "id", "") or "").strip()
        for step in (steps or [])
        if str(getattr(step, "id", "") or "").strip()
    }
    if not {"synthesize_final_tz", "validate_tz_completeness"}.issubset(plan_ids):
        return False
    return bool({"use_cli_repo_audit", "use_cli_repo_grounding"} & plan_ids)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _supports_strict_chat_json_contract(fn: Callable[..., Any]) -> bool:
    return bool(getattr(fn, "_supports_strict_json_contract", False))


def _contains_internal_runtime_markup(text: Any) -> bool:
    lowered = strip_ansi(str(text or "")).strip().lower()
    if not lowered:
        return False
    return any(token in lowered for token in ("[tool_", "[/tool_", "{tool =>", "corr_id"))


def _looks_like_nonfinal_rework_text(text: Any) -> bool:
    normalized = strip_ansi(str(text or "")).strip()
    if not normalized:
        return True
    lowered = normalized.lower()
    if len(lowered) > 200:
        return False
    return lowered.startswith(
        (
            "проанализирую",
            "сначала проанализирую",
            "сейчас проанализирую",
            "я проанализирую",
            "подготовлю ",
            "сначала подготовлю",
            "сейчас подготовлю",
            "я подготовлю",
            "проверю ",
            "сначала проверю",
            "я проверю",
            "изучу ",
            "сначала изучу",
            "я изучу",
        )
    )


def _is_analyst_runtime_context(
    session: Any,
    *,
    execution_context: Optional[Dict[str, Any]] = None,
) -> bool:
    mode_id = str(get_active_mode(session, "") or "").strip().lower()
    if mode_id == "analyst":
        return True
    executor_profile = str(getattr(session, "executor_profile", "") or "").strip().lower()
    if executor_profile == "analyst":
        return True
    cli_work_type = str(
        getattr(getattr(session, "cli", None), "cli_work_type", getattr(session, "cli_work_type", ""))
        or ""
    ).strip().lower()
    if cli_work_type == "analytics":
        return True
    if getattr(session, "analyst_run_artifact_handle", None) is not None:
        return True
    if isinstance(execution_context, dict) and execution_context:
        return True
    return False


def _is_analyst_telegram_delivery(
    session: Any,
    dest: Dict[str, Any],
    *,
    execution_context: Optional[Dict[str, Any]] = None,
) -> bool:
    if str((dest or {}).get("kind") or "telegram").strip() != "telegram":
        return False
    if (dest or {}).get("chat_id") is None:
        return False
    return _is_analyst_runtime_context(session, execution_context=execution_context)


def _resolve_intermediate_artifacts_dir(session: Any, cwd: str) -> str:
    handle = getattr(session, "analyst_run_artifact_handle", None)
    artifacts_dir = str(getattr(handle, "artifacts_dir", "") or "").strip()
    if artifacts_dir:
        os.makedirs(artifacts_dir, exist_ok=True)
        return artifacts_dir
    path = os.path.join(cwd, "_orchestrator")
    os.makedirs(path, exist_ok=True)
    return path


def _select_final_repo_review_draft_seed(
    step_results_local: List[Dict[str, Any]],
    *,
    polished_path: str = "",
    draft_path: str = "",
) -> tuple[str, str]:
    seen_paths: set[str] = set()

    def _iter_candidate_paths() -> List[str]:
        ordered: List[str] = []

        def _push(path_value: Any) -> None:
            path = str(path_value or "").strip()
            if not path or path in seen_paths or not os.path.exists(path):
                return
            seen_paths.add(path)
            ordered.append(path)

        for path in (polished_path, draft_path):
            _push(path)

        for preferred_step_id in ("synthesize_final_tz", "validate_tz_completeness"):
            for item in reversed(step_results_local or []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("task_id") or "").strip() != preferred_step_id:
                    continue
                outputs = item.get("outputs") or []
                if isinstance(outputs, list):
                    for output in outputs:
                        if not isinstance(output, dict):
                            continue
                        _push(output.get("path") or output.get("file_path"))
                _push(item.get("orchestrator_artifact"))
        return ordered

    for candidate_path in _iter_candidate_paths():
        try:
            with open(candidate_path, "r", encoding="utf-8") as fh:
                candidate_text = fh.read().strip()
        except Exception:
            continue
        if candidate_text:
            return candidate_text, candidate_path
    return "", ""


_ANALYST_USE_CLI_READONLY_CONTRACT = (
    "Режим analyst: это строго аналитическая, read-only работа.\n"
    "- Не изменяй код, конфиги, тесты, документацию и другие файлы проекта.\n"
    "- Не реализуй функциональность, не применяй патчи, не выполняй миграции и не запускай команды с side effects.\n"
    "- Если пользователь просит доработку, дообогащение функционала или дает внешний референс,\n"
    "  используй это только как вход для анализа, сравнения и подготовки ТЗ/аудита.\n"
    "- Если внешний референс релевантен, сохрани его отдельным implementation-guidance слоем\n"
    "  с source, extracted pattern, local mapping и статусом адаптации.\n"
    "- Внешние референсы не подменяют source of truth текущего репозитория: явно отделяй их от\n"
    "  подтвержденных фактов проекта."
)
_ANALYST_SOURCE_USER_TEXT_RUNTIME_ATTR = "analyst_source_user_text_runtime"
_ANALYST_STEP_RESULTS_SYNC_HOOK_ATTR = "analyst_step_results_sync_hook"


class OrchestratorRunner:
    _MAX_REPLANS = 25
    _MAX_REPLAN_CHECKS = 12
    _FORCE_REPLAN_EVERY_N_NONASK_STEPS = 2
    _MAX_FINAL_REWORK_PASSES = 2

    def __init__(
        self,
        config: AppConfig,
        *,
        max_clarifications: int = 3,
        continue_without_clarifications: bool = False,
        final_rework_enabled: bool = False,
        final_rework_passes: int = 0,
        template_provider: Optional[Callable[[Any], Dict[str, Any]]] = None,
        deps: Optional[OrchestratorDeps] = None,
    ):
        self._config = config
        self._max_clarifications = max(1, int(max_clarifications))
        self._continue_without_clarifications = bool(continue_without_clarifications)
        self._final_rework_enabled = bool(final_rework_enabled)
        self._final_rework_passes = max(
            0,
            min(int(final_rework_passes), self._MAX_FINAL_REWORK_PASSES),
        )
        self._template_provider = template_provider
        self._deps = deps or load_default_deps()
        tool_registry = self._deps.get_tool_registry(config)
        self._tool_registry = tool_registry
        self._executor = self._deps.Executor(config, tool_registry)
        self._dispatcher = self._deps.Dispatcher(config, tool_registry)
        self._log = logging.getLogger(__name__)
        self._reaction_engine = ReactionEngine(logger=self._log)
        self._retry_on_failed_step_rule = ReactionRule(
            rule_id="orchestrator.retry_on_failed_step",
            event_types=[EventType.STEP_FAILED],
            min_severity=EventSeverity.ERROR,
            actions=[ReactionAction(action_type="retry_step", params={"max_retries": self._MAX_REPLANS})],
        )

    async def _should_retry_via_reactions(
        self,
        *,
        session_id: str,
        step_id: str,
        step_status: str,
        summary: str,
        replan_count: int,
    ) -> bool:
        event = OrchestratorEvent(
            event_type=EventType.STEP_FAILED,
            severity=EventSeverity.ERROR,
            session_id=str(session_id or ""),
            step_id=str(step_id or ""),
            message=str(summary or ""),
            payload={
                "status": str(step_status or ""),
                "retry_count": int(replan_count),
            },
        )
        try:
            results = await self._reaction_engine.execute(
                event,
                [self._retry_on_failed_step_rule],
                ctx={"session_id": str(session_id or "")},
            )
        except Exception:
            self._log.exception("reactions v2 execution failed")
            return False
        for item in results:
            if str(item.get("action") or "") == "retry_step" and str(item.get("status") or "") == "queued":
                return True
        return False

    def _load_session(self, cwd: str) -> Dict[str, Any]:
        path = os.path.join(cwd, "SESSION.json")
        data = self._deps.read_json_locked(path, default={"orchestrator_by_task": {}})
        if isinstance(data, dict):
            data.setdefault("orchestrator_by_task", {})
            return data
        return {"orchestrator_by_task": {}}

    def _get_run_execution_context(self, session: Any) -> Dict[str, Any]:
        handle = getattr(session, "analyst_run_artifact_handle", None)
        state_path = str(getattr(handle, "state_path", "") or "").strip()
        if not state_path:
            return {}
        try:
            state = self._deps.read_json_locked(state_path, default={})
        except Exception as e:
            self._log.warning("failed to read orchestrator execution_context path=%s err=%s", state_path, e)
            return {}
        if not isinstance(state, dict):
            return {}
        mode_context = state.get("mode_context")
        if not isinstance(mode_context, dict):
            return {}
        execution_context = mode_context.get("execution_context")
        if not isinstance(execution_context, dict):
            return {}
        return execution_context

    @staticmethod
    def _resolve_runtime_source_user_text(
        session: Any,
        execution_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        raw_session_text = str(getattr(session, _ANALYST_SOURCE_USER_TEXT_RUNTIME_ATTR, "") or "").strip()
        if raw_session_text:
            return raw_session_text
        payload = execution_context if isinstance(execution_context, dict) else {}
        return str(payload.get("source_user_text") or payload.get("user_text_preview") or "").strip()

    @staticmethod
    def _extract_clarification_answers(text: str) -> List[str]:
        answers: List[str] = []
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("Ответ пользователя:"):
                continue
            answer = stripped.split(":", 1)[1].strip()
            if answer and not is_non_semantic_ask_answer(answer) and answer not in answers:
                answers.append(answer)
        return answers

    @staticmethod
    def _build_recovery_prompt_text(original_user_text: str, clarification_answers: List[str]) -> str:
        prompt = str(original_user_text or "").strip()
        for answer in clarification_answers or []:
            answer_text = str(answer or "").strip()
            if not answer_text:
                continue
            prompt = f"{prompt}\nОтвет пользователя: {answer_text}".strip()
        return prompt

    def _persist_recovery_input_bundle(
        self,
        session: Any,
        *,
        clarification_answers: List[str],
    ) -> None:
        handle = getattr(session, "analyst_run_artifact_handle", None)
        state_path = str(getattr(handle, "state_path", "") or "").strip()
        if not state_path:
            return

        def _updater(state: Any) -> Any:
            payload = dict(state or {}) if isinstance(state, dict) else {}
            mode_context = payload.get("mode_context")
            mode_context = dict(mode_context or {}) if isinstance(mode_context, dict) else {}
            execution_context = mode_context.get("execution_context")
            execution_context = (
                dict(execution_context or {}) if isinstance(execution_context, dict) else {}
            )
            input_bundle = mode_context.get("input_bundle")
            input_bundle = dict(input_bundle or {}) if isinstance(input_bundle, dict) else {}
            original_user_text = str(
                input_bundle.get("original_user_text")
                or mode_context.get("source_user_text")
                or execution_context.get("source_user_text")
                or execution_context.get("user_text_preview")
                or ""
            ).strip()
            deduped_answers: List[str] = []
            seen: set[str] = set()
            for item in clarification_answers or []:
                text = str(item or "").strip()
                if not text or is_non_semantic_ask_answer(text) or text in seen:
                    continue
                seen.add(text)
                deduped_answers.append(text)
            input_bundle["original_user_text"] = original_user_text
            input_bundle["clarification_answers"] = deduped_answers
            input_bundle["recovery_prompt_text"] = self._build_recovery_prompt_text(
                original_user_text,
                deduped_answers,
            )
            mode_context["input_bundle"] = input_bundle
            execution_context["clarification_answers"] = deduped_answers
            mode_context["execution_context"] = execution_context
            payload["mode_context"] = mode_context
            return payload

        try:
            self._deps.update_json_locked(state_path, _updater, default={})
        except Exception as e:
            self._log.warning("failed to persist analyst recovery input bundle path=%s err=%s", state_path, e)

    def _get_analyst_intent_flags(self, session: Any) -> Dict[str, Any]:
        raw_flags = getattr(session, _ANALYST_INTENT_FLAGS_SESSION_ATTR, None)
        if not isinstance(raw_flags, dict):
            return {}
        needs_clarification = bool(raw_flags.get("needs_clarification"))
        return {
            "document_kind": str(raw_flags.get("document_kind") or "").strip().lower(),
            "needs_clarification": needs_clarification,
            "requires_codebase_grounding": bool(raw_flags.get("requires_codebase_grounding")),
            "requires_repo_audit": bool(raw_flags.get("requires_repo_audit")),
            "requires_final_repo_review": bool(raw_flags.get("requires_final_repo_review")),
            "clarification_is_blocking": bool(needs_clarification or raw_flags.get("clarification_is_blocking")),
            "clarification_topic": str(raw_flags.get("clarification_topic") or "").strip(),
            "template_id": str(raw_flags.get("template_id") or "").strip(),
            "clarification_question": str(raw_flags.get("clarification_question") or "").strip(),
            "clarification_options": [
                str(item).strip()
                for item in (raw_flags.get("clarification_options") or [])
                if str(item).strip()
            ][:4],
            "required_inputs": [
                str(item).strip()
                for item in (raw_flags.get("required_inputs") or [])
                if str(item).strip()
            ],
        }

    def _effective_analyst_repo_flags(
        self,
        session: Any,
        template: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self._get_analyst_intent_flags(session)
        active_template = template
        merged = {
            "document_kind": str(payload.get("document_kind") or "").strip().lower(),
            "needs_clarification": bool(payload.get("needs_clarification")),
            "requires_codebase_grounding": bool(payload.get("requires_codebase_grounding")),
            "requires_repo_audit": bool(payload.get("requires_repo_audit")),
            "requires_final_repo_review": bool(payload.get("requires_final_repo_review")),
            "clarification_is_blocking": bool(payload.get("clarification_is_blocking")),
            "clarification_topic": str(payload.get("clarification_topic") or "").strip(),
            "template_id": str(payload.get("template_id") or "").strip(),
            "clarification_question": str(payload.get("clarification_question") or "").strip(),
            "clarification_options": [
                str(item).strip()
                for item in (payload.get("clarification_options") or [])
                if str(item).strip()
            ][:4],
            "required_inputs": [
                str(item).strip()
                for item in (payload.get("required_inputs") or [])
                if str(item).strip()
            ],
            "focus_paths": [
                str(item).strip()
                for item in (payload.get("focus_paths") or [])
                if str(item).strip()
            ][:5],
        }
        if not isinstance(active_template, dict):
            return merged
        if not merged["template_id"]:
            merged["template_id"] = str(active_template.get("_id") or "").strip()
        output_kind = str(active_template.get("output_kind") or "").strip().lower()
        if not merged["document_kind"] and output_kind in {"analysis", "spec", "audit"}:
            merged["document_kind"] = output_kind
        merged["requires_codebase_grounding"] = bool(
            merged["requires_codebase_grounding"] or _coerce_bool(active_template.get("repo_grounded_required"))
        )
        merged["requires_repo_audit"] = bool(
            merged["requires_repo_audit"] or _coerce_bool(active_template.get("repo_audit_required"))
        )
        merged["requires_final_repo_review"] = bool(
            merged["requires_final_repo_review"] or _coerce_bool(active_template.get("final_repo_review_required"))
        )
        if not merged["required_inputs"]:
            merged["required_inputs"] = [
                str(item).strip()
                for item in (active_template.get("required_inputs") or [])
                if str(item).strip()
            ]
        return merged

    def _build_session_mode_context(self, session: Any, template: Optional[Dict[str, Any]] = None) -> str:
        payload = self._effective_analyst_repo_flags(session, template)
        if not payload:
            return ""
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception as e:
            self._log.warning("failed to serialize analyst intent flags for planner context: %s", e)
            return ""
        return f"\nanalyst_intent_flags:\n{encoded}"

    def _requires_blocking_clarification(self, session: Any) -> bool:
        if bool(getattr(session, _ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR, False)):
            return True
        return bool(self._get_analyst_intent_flags(session).get("clarification_is_blocking"))

    def _required_repo_use_cli_step_ids(
        self,
        session: Any,
        template: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        flags = self._effective_analyst_repo_flags(session, template)
        required_step_ids: List[str] = []
        requires_codebase_grounding = bool(flags.get("requires_codebase_grounding"))
        requires_repo_audit = bool(flags.get("requires_repo_audit"))
        requires_final_repo_review = bool(flags.get("requires_final_repo_review"))
        if requires_codebase_grounding and not requires_repo_audit and not requires_final_repo_review:
            required_step_ids.append("use_cli_repo_grounding")
        if requires_repo_audit:
            required_step_ids.append("use_cli_repo_audit")
        if requires_final_repo_review:
            required_step_ids.append("use_cli_repo_final_review")
        return required_step_ids

    @staticmethod
    def _repo_step_root(session: Any) -> str:
        project_root = str(getattr(session, "project_root", "") or "").strip()
        workdir = str(getattr(session, "workdir", "") or "").strip()
        return project_root or workdir or "текущую рабочую директорию сессии"

    def _is_valid_repo_use_cli_step(
        self,
        step: Any,
        *,
        step_id: str,
        root: str,
    ) -> bool:
        if str(getattr(step, "id", "") or "").strip() != step_id:
            return False
        if str(getattr(step, "step_type", "") or "").strip() != "use_cli":
            return False
        instruction = str(getattr(step, "instruction", "") or "")
        return bool(root) and root in instruction

    def _missing_required_repo_use_cli_step_ids(
        self,
        session: Any,
        completed_ok: set[str],
        *,
        steps: Optional[List[Any]] = None,
        historical_steps: Optional[Dict[str, Any]] = None,
        historical_completed_ok: Optional[set[str]] = None,
        template: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        required_ids = self._required_repo_use_cli_step_ids(session, template)
        known_completed_ok = set(completed_ok or set())
        if historical_completed_ok:
            known_completed_ok.update(str(step_id or "").strip() for step_id in historical_completed_ok if str(step_id or "").strip())
        if not steps:
            return [step_id for step_id in required_ids if step_id not in known_completed_ok]
        step_by_id: Dict[str, Any] = {}
        if isinstance(historical_steps, dict):
            step_by_id.update(
                {
                    str(step_id or "").strip(): step
                    for step_id, step in historical_steps.items()
                    if str(step_id or "").strip()
                }
            )
        step_by_id.update(
            {
                str(getattr(step, "id", "") or "").strip(): step
                for step in steps
                if str(getattr(step, "id", "") or "").strip()
            }
        )
        missing: List[str] = []
        for step_id in required_ids:
            if step_id in known_completed_ok:
                # Trust the runtime execution status: if the step finished
                # successfully, don't second-guess it via instruction string
                # matching (root path may have changed between planning and
                # finalization, causing false negatives).
                continue
            missing.append(step_id)
        return missing

    def _build_use_cli_task_text(self, step: Any, session: Any, current_user_text: str) -> str:
        task_text = str(getattr(step, "instruction", "") or "").strip()
        if _is_analyst_runtime_context(session):
            task_text = f"{_ANALYST_USE_CLI_READONLY_CONTRACT}\n\n{task_text}".strip()
        context_blocks: List[str] = []

        handle = getattr(session, "analyst_run_artifact_handle", None)
        state_path = str(getattr(handle, "state_path", "") or "").strip()
        if state_path:
            try:
                state = self._deps.read_json_locked(state_path, default={})
            except Exception as e:
                self._log.warning("failed to read analyst run state for use_cli context path=%s err=%s", state_path, e)
                state = {}
            mode_context = state.get("mode_context") if isinstance(state, dict) else {}
            mode_context = mode_context if isinstance(mode_context, dict) else {}
            execution_context = mode_context.get("execution_context") if isinstance(mode_context, dict) else {}
            execution_context = execution_context if isinstance(execution_context, dict) else {}
            source_user_text = self._resolve_runtime_source_user_text(session, execution_context)
            if source_user_text:
                context_blocks.append(f"Исходный запрос пользователя:\n{source_user_text}")

        clarification_answers = self._extract_clarification_answers(current_user_text)
        if clarification_answers:
            context_blocks.append(
                "Полученные уточнения пользователя:\n"
                + "\n".join(f"- {answer}" for answer in clarification_answers)
            )

        if not context_blocks:
            return task_text
        return (
            f"{task_text}\n\n"
            "Контекст задачи и приоритеты пользователя:\n"
            f"{chr(10).join(context_blocks)}\n\n"
            "Опирайся на этот запрос и уточнения как на главный критерий релевантности анализа."
        )

    @staticmethod
    def _classify_use_cli_output_error(output: str) -> str:
        text = str(output or "").strip()
        if not text:
            return ""
        # Only check the first few lines — real CLI errors appear at the top.
        # Scanning the entire output causes false positives when analytical text
        # mentions error patterns (e.g. "API returns api error: 429 on overload").
        head_lines = text.splitlines()[:5]
        head_lowered = "\n".join(head_lines).lower()
        patterns = (
            "[api error:",
            "api error:",
            "quota exceeded",
            "api quota has been exhausted",
        )
        if any(pattern in head_lowered for pattern in patterns):
            first_line = head_lines[0].strip()
            return first_line[:300]
        return ""

    async def run(self, session: Any, user_text: str, bot: Any, context: Any, dest: Dict[str, Any]) -> str:
        chat_id = dest.get("chat_id")
        cwd = ensure_chat_workspace(self._config.defaults.workdir, chat_id)
        execution_context = self._get_run_execution_context(session)
        mode_id = str(get_active_mode(session, "") or "").strip()
        source_user_text = self._resolve_runtime_source_user_text(session, execution_context)
        raw_user_query = source_user_text or str(user_text or "").strip()
        raw_user_clarification_answers: List[str] = []
        seen_clarification_answers: set[str] = set()
        stored_clarification_answers = (
            execution_context.get("clarification_answers") if isinstance(execution_context, dict) else []
        )
        for item in list(stored_clarification_answers or []) + self._extract_clarification_answers(user_text):
            answer_text = str(item or "").strip()
            if not answer_text or is_non_semantic_ask_answer(answer_text) or answer_text in seen_clarification_answers:
                continue
            seen_clarification_answers.add(answer_text)
            raw_user_clarification_answers.append(answer_text)
        analyst_telegram_delivery = _is_analyst_telegram_delivery(
            session,
            dest,
            execution_context=execution_context,
        )
        if analyst_telegram_delivery:
            setattr(session, "analyst_runtime_final_output_delivered", False)
            setattr(session, "analyst_runtime_final_output_delivered_text", "")
        if source_user_text:
            self._log.info(
                "=== orchestrator run START session=%s chat=%s source_user_text=%r input_prompt=%r ===",
                session.id,
                chat_id,
                source_user_text[:200],
                user_text[:200],
            )
        else:
            self._log.info(
                "=== orchestrator run START session=%s chat=%s input_prompt=%r ===",
                session.id,
                chat_id,
                user_text[:200],
            )
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "orchestrator",
                "phase": "start",
                "status": "running",
                "message": "Запуск orchestrator",
            },
        )

        def _emit_orch(
            phase: str,
            status: str,
            message: str,
            *,
            corr_id: str = "",
            task_id: str = "",
            step_id: str = "",
            iteration: int = 0,
        ) -> None:
            emit_runtime_progress(
                session,
                {
                    "mode_id": mode_id,
                    "source": "orchestrator",
                    "phase": str(phase or "event"),
                    "status": str(status or "running"),
                    "corr_id": str(corr_id or ""),
                    "task_id": str(task_id or ""),
                    "step_id": str(step_id or ""),
                    "iteration": int(iteration or 0),
                    "message": str(message or ""),
                },
            )
        template: Optional[Dict[str, Any]] = None
        should_resolve_template = bool(
            callable(self._template_provider)
            and (
                self._final_rework_enabled
                or bool(getattr(session, _ANALYST_INTENT_FLAGS_SESSION_ATTR, None))
                or bool(getattr(session, "analyst_template_id", None))
                or bool(getattr(getattr(session, "modes", None), "analyst_template_id", None))
                or _is_analyst_runtime_context(session, execution_context=execution_context)
            )
        )
        if should_resolve_template:
            template = self._template_provider(session)
        intent_flags = self._effective_analyst_repo_flags(session, template)
        strict_analyst_runtime_context = bool(
            _is_analyst_runtime_context(
                session,
                execution_context=execution_context,
            )
        )
        analyst_runtime_context = bool(
            strict_analyst_runtime_context
            or isinstance(getattr(session, _ANALYST_INTENT_FLAGS_SESSION_ATTR, None), dict)
        )
        allow_continue_without_clarifications = bool(
            self._continue_without_clarifications and not strict_analyst_runtime_context
        )
        initial_blocking_clarification = bool(intent_flags.get("clarification_is_blocking"))
        if not analyst_runtime_context:
            initial_blocking_clarification = bool(
                initial_blocking_clarification
                or getattr(session, _ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR, False)
            )
        setattr(
            session,
            _ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR,
            initial_blocking_clarification,
        )
        if not initial_blocking_clarification:
            setattr(session, _ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR, "")

        def _resolve_needs_input_message(resp: Any | None = None) -> str:
            next_questions = list(getattr(resp, "next_questions", []) or [])
            for item in next_questions:
                text = str(item or "").strip()
                if text:
                    return text
            summary = str(getattr(resp, "summary", "") or "").strip()
            if summary:
                return summary
            stored = str(getattr(session, _ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR, "") or "").strip()
            if stored:
                return stored
            clarification_question = str(intent_flags.get("clarification_question") or "").strip()
            if clarification_question:
                return clarification_question
            required_inputs = _normalize_required_input_gaps(intent_flags.get("required_inputs") or [])
            if required_inputs:
                return (
                    "Нужно уточнение пользователя, чтобы завершить работу.\n\n"
                    "Не закрыты обязательные входы задачи:\n"
                    + "\n".join(f"- {item}" for item in required_inputs)
                    + "\n\nОтветьте следующим сообщением и закройте эти пункты."
                )
            return "Нужно уточнение пользователя, но вопрос не сформирован."

        final_rework_passes = self._final_rework_passes
        target_size_hint = ""
        repo_grounded_required = False
        if isinstance(template, dict):
            target_size_hint = str(template.get("target_size_hint") or "").strip().lower()
            repo_grounded_required = _coerce_bool(template.get("repo_grounded_required"))
        repo_grounded_required = bool(
            repo_grounded_required
            or intent_flags.get("requires_codebase_grounding")
            or intent_flags.get("requires_repo_audit")
            or intent_flags.get("requires_final_repo_review")
        )
        is_large_spec = target_size_hint == "large"
        max_final_rework_passes = 3 if is_large_spec and repo_grounded_required else self._MAX_FINAL_REWORK_PASSES
        if is_large_spec and repo_grounded_required and final_rework_passes <= 0:
            final_rework_passes = 3
        protected_spec_shell = (
            dict(template.get("protected_spec_shell") or {})
            if isinstance(template, dict) and isinstance(template.get("protected_spec_shell"), dict)
            else {}
        )
        system_prompt_addition = None
        if isinstance(template, dict):
            system_prompt_addition = (template.get("system_prompt_addition") or "").strip() or None
        memory_text = self._deps.read_memory(cwd)
        memory_context = self._deps.trim_for_context(memory_text, max_chars=2000)
        # Surface critical routing context to the planner.
        # Planner does not receive structured session state, only this textual context.
        ctx_summary = f"session_id={session.id} chat_id={chat_id}"
        active_mode = str(get_active_mode(session, "") or "").strip()
        executor_profile = str(getattr(session, "executor_profile", "") or "").strip() or "default"
        project_root = str(getattr(session, "project_root", "") or "").strip()
        workdir = str(getattr(session, "workdir", "") or "").strip()
        cli_work_type = str(
            getattr(getattr(session, "cli", None), "cli_work_type", getattr(session, "cli_work_type", ""))
            or ""
        ).strip()
        if active_mode:
            ctx_summary += f"\nactive_mode={active_mode}"
        ctx_summary += f"\nexecutor_profile={executor_profile}"
        if cli_work_type:
            ctx_summary += f"\ncli_work_type={cli_work_type}"
        if project_root:
            ctx_summary += f"\nproject_root={project_root}"
        if workdir:
            ctx_summary += f"\nworkdir={workdir}"
        ctx_summary += self._build_session_mode_context(session, template)
        if memory_context:
            ctx_summary = f"{ctx_summary}\nmemory:\n{memory_context}"
            self._log.info("memory context loaded, %d chars", len(memory_context))
        task_key = session.id
        # Do not feed persisted orchestrator history from previous top-level runs back into the
        # planner. It causes independent reruns in the same session to "continue" old plans,
        # reusing step numbering/dependencies (for example, starting from step7 and depending on
        # missing step1-step6). Replans within the current run already use step_results_so_far
        # and prior_steps, which is the only continuity the planner should rely on.
        retrieved_items = self._deps.retrieve_relevant_context(cwd, user_text, limit=6)
        retrieved_context = self._deps.format_retrieved_context(retrieved_items, max_chars=1600)
        if retrieved_context:
            ctx_summary += f"\nretrieved_context:\n{retrieved_context}"
            self._log.info("retrieved context loaded, items=%d chars=%d", len(retrieved_items), len(retrieved_context))
        replan_count = 0
        # Auto-replan after each executed step to adapt based on intermediate results.
        # `prior_step_results` stores the latest known status per task_id across replans.
        # `step_results` stores the full attempt history, including retries for the same task_id.
        prior_step_results: Dict[str, Dict[str, Any]] = {}
        results: List[str] = []
        step_results: List[Dict[str, Any]] = []
        step_title_by_id: Dict[str, str] = {}
        known_step_defs_by_id: Dict[str, Any] = {}
        clarification_limit_reached = False
        clarification_notice_added = False
        replan_checks_done = 0
        nonask_steps_since_plan = 0
        repo_steps_recovery_attempted = False
        final_answer_max_tokens = 32768
        external_references_section = (
            str(protected_spec_shell.get("external_references_section") or "Внешние референсы и примеры реализации").strip()
            or "Внешние референсы и примеры реализации"
        )
        external_references_conditional = bool(protected_spec_shell.get("external_references_conditional", True))
        source_task_section = (
            str(protected_spec_shell.get("source_task_section") or "Исходная задача").strip() or "Исходная задача"
        )
        open_questions_section = (
            str(protected_spec_shell.get("open_questions_section") or "Открытые вопросы и валидационные шаги").strip()
            or "Открытые вопросы и валидационные шаги"
        )
        assumptions_section = "Допущения и незакрытые входы"

        class _AwaitingUserInput(RuntimeError):
            def __init__(self, message: str):
                super().__init__(message)
                self.message = str(message or "")

        class _RestartPlanningAfterClarification(RuntimeError):
            pass

        def _refresh_raw_user_clarification_answers() -> None:
            nonlocal raw_user_clarification_answers
            answers: List[str] = []
            seen_answers: set[str] = set()
            for item in self._extract_clarification_answers(user_text):
                answer_text = str(item or "").strip()
                if not answer_text or answer_text in seen_answers:
                    continue
                seen_answers.add(answer_text)
                answers.append(answer_text)
            raw_user_clarification_answers = answers

        def _extract_reference_urls(*texts: Any) -> List[str]:
            urls: List[str] = []
            seen: set[str] = set()
            for text in texts:
                for match in re.findall(r"https?://[^\s<>()\"']+", str(text or "")):
                    url = str(match or "").rstrip(".,);]").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    urls.append(url)
            return urls

        def _extract_local_mapping_items(*texts: Any, limit: int = 5) -> List[str]:
            pattern = re.compile(
                r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:py|md|ya?ml|json|ts|tsx|js|jsx|php|go|rs)"
            )
            items: List[str] = []
            seen: set[str] = set()
            for text in texts:
                for match in pattern.findall(str(text or "")):
                    candidate = str(match or "").strip()
                    if not candidate or candidate in seen:
                        continue
                    seen.add(candidate)
                    items.append(candidate)
                    if len(items) >= limit:
                        return items
            return items

        def _read_text_excerpt(path: str) -> str:
            candidate = str(path or "").strip()
            if not candidate or not os.path.exists(candidate):
                return ""
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    return fh.read(12000)
            except Exception:
                self._log.warning("failed to read external reference artifact path=%s", candidate, exc_info=True)
                return ""

        def _extract_research_summary(text: str) -> str:
            lines: List[str] = []
            for raw_line in str(text or "").splitlines():
                stripped = raw_line.strip()
                if (
                    not stripped
                    or stripped.startswith("#")
                    or stripped.startswith("```")
                    or stripped.startswith("|")
                    or stripped.startswith(">")
                ):
                    continue
                normalized = stripped[2:].strip() if stripped.startswith("- ") else stripped
                lowered = normalized.lower()
                if lowered.startswith(("title:", "step_type:", "status:", "дата:", "референс:", "доп. источники:")):
                    continue
                lines.append(normalized)
                if len(lines) >= 3:
                    break
            return " ".join(lines).strip()

        def _collect_external_reference_entries(
            *,
            user_query: str,
            mapping_text: str = "",
            step_results_local: Optional[List[Dict[str, Any]]] = None,
        ) -> List[Dict[str, str]]:
            if not protected_spec_shell or not external_references_conditional:
                return []

            local_mapping_items = _extract_local_mapping_items(mapping_text)
            entries_by_source: Dict[str, Dict[str, str]] = {}

            def _upsert_entry(
                source: str,
                *,
                extracted_pattern: str = "",
                adaptation_status: str = "requires-validation",
                source_kind: str = "user_reference",
                research_artifact: str = "",
            ) -> None:
                source_text = str(source or "").strip()
                if not source_text:
                    return
                entry = entries_by_source.setdefault(
                    source_text,
                    {
                        "source": source_text,
                        "source_kind": source_kind,
                        "research_artifact": str(research_artifact or "").strip(),
                        "extracted_pattern": "",
                        "local_mapping": "",
                        "adaptation_status": "",
                    },
                )
                if extracted_pattern and not entry["extracted_pattern"]:
                    entry["extracted_pattern"] = extracted_pattern
                if research_artifact and not entry["research_artifact"]:
                    entry["research_artifact"] = str(research_artifact or "").strip()
                if adaptation_status and not entry["adaptation_status"]:
                    entry["adaptation_status"] = adaptation_status
                if local_mapping_items:
                    entry["local_mapping"] = "; ".join(local_mapping_items)

            for url in _extract_reference_urls(user_query, raw_user_clarification_answers):
                _upsert_entry(
                    url,
                    extracted_pattern=(
                        "Внешний референс из исходного запроса; использовать как пример реализации "
                        "и источник паттернов для адаптации."
                    ),
                )

            candidate_results = step_results_local if isinstance(step_results_local, list) else step_results
            research_paths: List[str] = []
            for item in candidate_results or []:
                if not isinstance(item, dict):
                    continue
                outputs = item.get("outputs") or []
                for output in outputs if isinstance(outputs, list) else []:
                    if not isinstance(output, dict):
                        continue
                    for key in ("path", "file_path", "name"):
                        candidate = str(output.get(key) or "").strip()
                        if candidate and "research" in os.path.basename(candidate).lower():
                            research_paths.append(candidate)
                artifact_path = str(item.get("orchestrator_artifact") or "").strip()
                if artifact_path and "research" in os.path.basename(artifact_path).lower():
                    research_paths.append(artifact_path)

            seen_paths: set[str] = set()
            for candidate_path in research_paths:
                normalized_path = str(candidate_path or "").strip()
                if not normalized_path or normalized_path in seen_paths:
                    continue
                seen_paths.add(normalized_path)
                excerpt = _read_text_excerpt(normalized_path)
                if not excerpt:
                    continue
                summary = _extract_research_summary(excerpt)
                status = "direct-adapt" if "напрямую" in summary.lower() else "requires-validation"
                urls = _extract_reference_urls(excerpt)
                if not urls:
                    _upsert_entry(
                        normalized_path,
                        extracted_pattern=summary or "Внешнее исследование; использовать как implementation guidance.",
                        adaptation_status=status,
                        source_kind="research_artifact",
                        research_artifact=normalized_path,
                    )
                    continue
                for url in urls:
                    _upsert_entry(
                        url,
                        extracted_pattern=summary or "Внешнее исследование; использовать как implementation guidance.",
                        adaptation_status=status,
                        source_kind="research_artifact",
                        research_artifact=normalized_path,
                    )

            for entry in entries_by_source.values():
                if not entry["extracted_pattern"]:
                    entry["extracted_pattern"] = "Использовать как внешний пример реализации и источник паттернов."
                if not entry["local_mapping"]:
                    entry["local_mapping"] = "[Нужно уточнить локальные файлы/контракты для адаптации]"
                if not entry["adaptation_status"]:
                    entry["adaptation_status"] = "requires-validation"
            return list(entries_by_source.values())

        def _render_external_references_block(entries: List[Dict[str, str]]) -> str:
            blocks: List[str] = [f"## {external_references_section}"]
            for index, entry in enumerate(entries or [], start=1):
                source = str(entry.get("source") or "").strip()
                if not source:
                    continue
                blocks.extend(
                    [
                        f"### Референс {index}",
                        f"- Источник: {source}",
                        f"- Извлечённый паттерн: {str(entry.get('extracted_pattern') or '').strip()}",
                        f"- Local mapping: {str(entry.get('local_mapping') or '').strip()}",
                        f"- Статус адаптации: {str(entry.get('adaptation_status') or '').strip()}",
                    ]
                )
                research_artifact = str(entry.get("research_artifact") or "").strip()
                if research_artifact:
                    blocks.append(f"- Артефакт исследования: {research_artifact}")
            return "\n".join(blocks).strip()

        def _external_reference_target_key(entry: Dict[str, str]) -> str:
            source = str(entry.get("source") or "").strip()
            local_mapping = str(entry.get("local_mapping") or "").strip()
            adaptation_status = str(entry.get("adaptation_status") or "").strip()
            parts: List[str] = []
            if source:
                parts.append(source)
            if local_mapping:
                parts.append(f"-> {local_mapping}")
            if adaptation_status:
                parts.append(f"[{adaptation_status}]")
            return " ".join(parts).strip()

        def _build_task_contract_protected_spec_shell(text: str) -> Dict[str, Any]:
            if not protected_spec_shell:
                return {}

            mapping_fragments: List[str] = [str(text or "").strip()]
            for item in step_results:
                if not isinstance(item, dict):
                    continue
                summary = str(item.get("summary") or "").strip()
                if summary:
                    mapping_fragments.append(summary)
                outputs = item.get("outputs") or []
                if not isinstance(outputs, list):
                    continue
                for output in outputs:
                    if not isinstance(output, dict):
                        continue
                    for key in ("content", "content_preview", "preview", "path", "file_path", "name"):
                        candidate = str(output.get(key) or "").strip()
                        if candidate:
                            mapping_fragments.append(candidate)
            external_entries = _collect_external_reference_entries(
                user_query=raw_user_query,
                mapping_text="\n".join(fragment for fragment in mapping_fragments if fragment),
                step_results_local=step_results,
            )
            normalized_shell = dict(protected_spec_shell)
            if external_entries:
                normalized_shell["external_references_section"] = external_references_section
                normalized_shell["external_reference_targets"] = [
                    {
                        "source": str(entry.get("source") or "").strip(),
                        "local_mapping": str(entry.get("local_mapping") or "").strip(),
                        "adaptation_status": str(entry.get("adaptation_status") or "").strip(),
                        "research_artifact": str(entry.get("research_artifact") or "").strip(),
                    }
                    for entry in external_entries
                    if _external_reference_target_key(entry)
                ]
            return normalized_shell

        def _collect_external_reference_runtime_gaps(text: str) -> Dict[str, List[str]]:
            protected_shell_contract = (
                dict(task_contract_payload.get("protected_spec_shell") or {})
                if isinstance(task_contract_payload, dict)
                else {}
            )
            section_name = str(protected_shell_contract.get("external_references_section") or "").strip()
            external_targets = protected_shell_contract.get("external_reference_targets") or []
            if not section_name or not isinstance(external_targets, list) or not external_targets:
                return {"missing_sections": [], "external_reference_gaps": []}

            _, _, sections = _split_level_two_sections(str(text or "").strip())
            external_section_body = ""
            for heading, content in sections:
                if _normalize_shell_heading(heading) == _normalize_shell_heading(section_name):
                    external_section_body = str(content or "").strip()
                    break

            missing_sections: List[str] = []
            external_reference_gaps: List[str] = []
            if not external_section_body:
                missing_sections.append(section_name)
                for target in external_targets:
                    if not isinstance(target, dict):
                        continue
                    target_key = _external_reference_target_key(target)
                    if target_key:
                        external_reference_gaps.append(target_key)
                return {
                    "missing_sections": missing_sections,
                    "external_reference_gaps": external_reference_gaps,
                }

            section_text = str(external_section_body or "").strip()
            for target in external_targets:
                if not isinstance(target, dict):
                    continue
                source = str(target.get("source") or "").strip()
                local_mapping = str(target.get("local_mapping") or "").strip()
                adaptation_status = str(target.get("adaptation_status") or "").strip()
                missing_parts: List[str] = []
                if source and source not in section_text:
                    missing_parts.append("source")
                if local_mapping and local_mapping not in section_text:
                    missing_parts.append("local_mapping")
                if (
                    adaptation_status
                    and f"Статус адаптации: {adaptation_status}" not in section_text
                    and "Статус адаптации:" not in section_text
                ):
                    missing_parts.append("adaptation_status")
                if missing_parts:
                    target_key = _external_reference_target_key(target)
                    if target_key:
                        external_reference_gaps.append(target_key)

            return {
                "missing_sections": missing_sections,
                "external_reference_gaps": external_reference_gaps,
            }

        def _normalize_shell_heading(value: str) -> str:
            return str(value or "").strip().lower()

        def _split_level_two_sections(text: str) -> tuple[str, str, List[tuple[str, str]]]:
            title = ""
            preamble: List[str] = []
            sections: List[tuple[str, str]] = []
            current_heading: Optional[str] = None
            current_lines: List[str] = []
            for raw_line in str(text or "").splitlines():
                stripped = raw_line.strip()
                if not title and stripped.startswith("# ") and not stripped.startswith("##"):
                    title = stripped[2:].strip()
                    continue
                if stripped.startswith("## "):
                    if current_heading is not None:
                        sections.append((current_heading, "\n".join(current_lines).strip()))
                    current_heading = stripped[3:].strip()
                    current_lines = []
                    continue
                if current_heading is None:
                    if stripped or preamble:
                        preamble.append(raw_line)
                else:
                    current_lines.append(raw_line)
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            return title, "\n".join(preamble).strip(), sections

        def _render_level_two_sections(title: str, sections: List[tuple[str, str]]) -> str:
            lines: List[str] = [f"# {title}"]
            for heading, content in sections:
                section_heading = str(heading or "").strip()
                if not section_heading:
                    continue
                lines.extend(["", f"## {section_heading}"])
                content_text = str(content or "").strip()
                if content_text:
                    lines.append(content_text)
            return "\n".join(lines).strip()

        def _apply_protected_spec_shell(text: str, *, user_query: str) -> str:
            if not protected_spec_shell:
                return str(text or "").strip()

            body = str(text or "").strip()
            title = str(protected_spec_shell.get("title") or "Техническое задание").strip() or "Техническое задание"
            external_reference_entries = _collect_external_reference_entries(
                user_query=user_query,
                mapping_text=body,
            )

            def _strip_leading_h1(value: str) -> str:
                lines = value.splitlines()
                while lines and not lines[0].strip():
                    lines.pop(0)
                if lines and lines[0].strip().startswith("# ") and not lines[0].strip().startswith("##"):
                    lines.pop(0)
                    while lines and not lines[0].strip():
                        lines.pop(0)
                return "\n".join(lines).strip()

            def _has_heading(value: str, heading: str) -> bool:
                normalized_heading = str(heading or "").strip().lower()
                if not normalized_heading:
                    return False
                for line in str(value or "").splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("#"):
                        candidate = stripped.lstrip("#").strip().lower()
                        if candidate == normalized_heading:
                            return True
                return False

            body = _strip_leading_h1(body)
            source_task_lines = [str(user_query or "").strip() or "[Нужно уточнить]"]
            if raw_user_clarification_answers:
                source_task_lines.extend(
                    [
                        "",
                        "Уточнения пользователя:",
                        *[f"- {answer}" for answer in raw_user_clarification_answers],
                    ]
                )
            if not _has_heading(body, source_task_section):
                source_task_block = f"## {source_task_section}\n" + "\n".join(source_task_lines).strip()
                body = f"{source_task_block}\n\n{body}".strip() if body else source_task_block
            if external_reference_entries and not _has_heading(body, external_references_section):
                external_references_block = _render_external_references_block(external_reference_entries)
                body = f"{body.rstrip()}\n\n{external_references_block}".strip() if body else external_references_block
            if not _has_heading(body, open_questions_section):
                open_questions_block = f"## {open_questions_section}\n- (нет)"
                body = f"{body.rstrip()}\n\n{open_questions_block}".strip() if body else open_questions_block
            return f"# {title}\n\n{body}".strip()

        def _resolve_required_top_level_sections(*, user_query: str, text: str = "") -> List[str]:
            active_template = template if isinstance(template, dict) else {}
            required_sections = active_template.get("required_sections") if isinstance(active_template, dict) else []
            ordered_sections: List[str] = []
            seen_sections: set[str] = set()
            for item in required_sections or []:
                heading = str(item or "").strip()
                normalized_heading = _normalize_shell_heading(heading)
                if not normalized_heading or normalized_heading in seen_sections:
                    continue
                seen_sections.add(normalized_heading)
                ordered_sections.append(heading)
            if (
                _collect_external_reference_entries(user_query=user_query, mapping_text=text)
                and _normalize_shell_heading(external_references_section) not in seen_sections
            ):
                ordered_sections.append(external_references_section)
            return ordered_sections

        def _collect_required_top_level_section_contract_gaps(
            text: str,
            *,
            user_query: str,
            required_sections: List[str],
        ) -> Dict[str, List[str]]:
            if not analyst_runtime_context or not protected_spec_shell:
                return {"missing_sections": [], "section_contract_gaps": []}
            normalized_text = _apply_protected_spec_shell(text, user_query=user_query)
            _, _, sections = _split_level_two_sections(normalized_text)
            actual_section_indices: Dict[str, int] = {}
            for idx, (heading, _content) in enumerate(sections):
                normalized_heading = _normalize_shell_heading(heading)
                if normalized_heading and normalized_heading not in actual_section_indices:
                    actual_section_indices[normalized_heading] = idx

            ordered_contract: List[str] = []
            seen_contract_headings: set[str] = set()

            def _append_contract_heading(value: str) -> None:
                heading = str(value or "").strip()
                normalized_heading = _normalize_shell_heading(heading)
                if not normalized_heading or normalized_heading in seen_contract_headings:
                    return
                seen_contract_headings.add(normalized_heading)
                ordered_contract.append(heading)

            if protected_spec_shell:
                _append_contract_heading(source_task_section)
            for heading in required_sections or []:
                _append_contract_heading(heading)
            if protected_spec_shell:
                _append_contract_heading(open_questions_section)

            if not ordered_contract:
                return {"missing_sections": [], "section_contract_gaps": []}

            missing_sections: List[str] = []
            section_contract_gaps: List[str] = []
            for heading in ordered_contract:
                if _normalize_shell_heading(heading) not in actual_section_indices:
                    missing_sections.append(heading)

            for previous_heading, next_heading in zip(ordered_contract, ordered_contract[1:]):
                previous_index = actual_section_indices.get(_normalize_shell_heading(previous_heading))
                next_index = actual_section_indices.get(_normalize_shell_heading(next_heading))
                if previous_index is None or next_index is None:
                    continue
                if previous_index > next_index:
                    section_contract_gaps.append(
                        "Нарушен порядок обязательных разделов: "
                        f"`{previous_heading}` должен идти раньше `{next_heading}`."
                    )

            return {
                "missing_sections": missing_sections,
                "section_contract_gaps": section_contract_gaps,
            }

        def _normalize_required_input_gaps(required_input_gaps: List[str]) -> List[str]:
            normalized_gaps: List[str] = []
            seen_gaps: set[str] = set()
            for item in required_input_gaps or []:
                gap = str(item or "").strip()
                if not gap or gap in seen_gaps:
                    continue
                seen_gaps.add(gap)
                normalized_gaps.append(gap)
            return normalized_gaps

        def _pause_on_analyst_required_input_gaps(
            required_input_gaps: List[str],
            *,
            stage: str,
        ) -> str:
            normalized_gaps = _normalize_required_input_gaps(required_input_gaps)
            if not normalized_gaps or not strict_analyst_runtime_context:
                return ""
            pause_message = _build_required_input_pause_message(normalized_gaps)
            setattr(session, _ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR, True)
            setattr(session, _ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR, pause_message)
            self._log.info(
                "analyst finalization paused: unresolved required inputs detected stage=%s session=%s gaps=%s",
                stage,
                getattr(session, "id", ""),
                normalized_gaps,
            )
            _emit_orch(
                "awaiting_input",
                "needs_input",
                pause_message,
            )
            return pause_message

        def _build_required_input_pause_message(required_input_gaps: List[str]) -> str:
            normalized_gaps = _normalize_required_input_gaps(required_input_gaps)
            return (
                "Нужно уточнение пользователя, чтобы завершить работу.\n\n"
                "Не закрыты обязательные входы задачи:\n"
                + "\n".join(f"- {item}" for item in normalized_gaps)
                + "\n\nОтветьте следующим сообщением и закройте эти пункты."
            )

        async def _build_required_input_ask_step(required_input_gaps: List[str], *, stage: str) -> Any:
            normalized_gaps = _normalize_required_input_gaps(required_input_gaps)
            if not normalized_gaps:
                raise RuntimeError("required_input_gaps are empty")

            seed_question = str(intent_flags.get("clarification_question") or "").strip()
            seed_options = [
                str(item).strip()
                for item in (intent_flags.get("clarification_options") or [])
                if str(item).strip()
            ]

            question, options, issues = apply_ask_schema(seed_question, seed_options)
            if issues:
                prompt_parts = [
                    f"Запрос пользователя:\n{raw_user_query or user_text}",
                    f"Приоритетная тема уточнения:\n{str(intent_flags.get('clarification_topic') or '').strip() or '(не задана)'}",
                    "Обязательные входные данные:",
                ]
                prompt_parts.extend(f"- {item}" for item in normalized_gaps)
                prompt_parts.extend(
                    [
                        "",
                        f"Уже полученные ответы пользователя:\n{json.dumps(raw_user_clarification_answers, ensure_ascii=False)}",
                        f"Seed question:\n{seed_question or '(не задан)'}",
                        f"Seed options:\n{json.dumps(seed_options, ensure_ascii=False)}",
                    ]
                )
                question, options = await build_validated_ask_payload(
                    self._config,
                    user_prompt="\n".join(prompt_parts),
                    system_prompt=ASK_USER_CLARIFICATION_SYSTEM,
                    chat_completion_fn=self._deps.chat_completion,
                    log=self._log,
                    log_prefix="orchestrator_required_input",
                )

            return self._deps.PlanStep(
                id=f"ask_user_required_input_{_slug(stage, fallback='late_gap')}",
                title="Уточнение обязательного входа",
                instruction="Запросить уточнение у пользователя по обязательному входу задачи",
                step_type="ask_user",
                ask_question=question,
                ask_options=options,
            )

        async def _request_required_input_clarification(
            required_input_gaps: List[str],
            *,
            stage: str,
            current_text: str,
            assessment_snapshot: Dict[str, Any],
        ) -> None:
            nonlocal user_text, replan_count, clarification_limit_reached, clarification_notice_added
            nonlocal last_final_assessment, last_model_text_before_runtime

            normalized_gaps = _normalize_required_input_gaps(required_input_gaps)
            if not normalized_gaps or not strict_analyst_runtime_context:
                return

            last_final_assessment = dict(assessment_snapshot or {})
            last_model_text_before_runtime = current_text

            try:
                ask_step = await _build_required_input_ask_step(normalized_gaps, stage=stage)
            except Exception as e:
                self._log.warning(
                    "failed to build ask_user clarification for required_input_gaps stage=%s session=%s err=%s",
                    stage,
                    getattr(session, "id", ""),
                    e,
                )
                raise RuntimeError(
                    "failed to build ask_user clarification for required_input_gaps without fallback"
                ) from e

            setattr(session, _ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR, True)
            setattr(session, _ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR, str(ask_step.ask_question or "").strip())
            resp = await self._execute_step(
                ask_step,
                session,
                bot,
                context,
                dest,
                ctx_summary,
                current_user_text=user_text,
                constraints=system_prompt_addition,
            )
            _record_step_result(
                {
                    "task_id": resp.task_id,
                    "status": resp.status,
                    "summary": resp.summary,
                    "title": ask_step.title,
                    "step_type": ask_step.step_type,
                    "ask_question": ask_step.ask_question,
                    "ask_options": list(ask_step.ask_options or []),
                    "outputs": _compact_outputs(
                        resp.outputs or [],
                        task_id=str(resp.task_id or ""),
                        step_type=str(ask_step.step_type or ""),
                    ),
                    "claims": list(getattr(resp, "claims", []) or []),
                    "tool_calls": resp.tool_calls,
                }
            )

            if getattr(resp, "status", "") == "needs_input":
                pause_message = _resolve_needs_input_message(resp)
                setattr(session, _ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR, pause_message)
                self._log.info(
                    "analyst finalization paused on ask_user clarification stage=%s session=%s gaps=%s",
                    stage,
                    getattr(session, "id", ""),
                    normalized_gaps,
                )
                _emit_orch("awaiting_input", "needs_input", pause_message)
                raise _AwaitingUserInput(pause_message)

            answer = ""
            if getattr(resp, "outputs", None):
                answer = str(resp.outputs[0].get("content") or "")
            if getattr(resp, "status", "") != "ok" or not answer:
                self._log.warning(
                    "required-input ask_user did not return an explicit answer stage=%s status=%s session=%s",
                    stage,
                    getattr(resp, "status", ""),
                    getattr(session, "id", ""),
                )
                raise _AwaitingUserInput(_pause_on_analyst_required_input_gaps(normalized_gaps, stage=stage))

            setattr(session, _ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR, "")
            selected, explicit_selection = _extract_ask_user_selected(answer)
            if _is_analyst_runtime_context(session) and not explicit_selection:
                pause_message = _pause_on_analyst_required_input_gaps(normalized_gaps, stage=stage)
                self._log.warning(
                    "required-input ask_user returned opaque success; pausing stage=%s session=%s answer=%r",
                    stage,
                    getattr(session, "id", ""),
                    answer[:200],
                )
                raise _AwaitingUserInput(pause_message)

            if is_non_semantic_ask_answer(selected):
                self._log.info(
                    "required-input ask_user returned control answer, not persisting as clarification: %r",
                    selected[:200],
                )
            else:
                user_text = f"{user_text}\nОтвет пользователя: {selected}"
                _refresh_raw_user_clarification_answers()
            self._persist_recovery_input_bundle(
                session,
                clarification_answers=self._extract_clarification_answers(user_text),
            )
            replan_count += 1
            if replan_count > self._max_clarifications:
                if allow_continue_without_clarifications:
                    clarification_limit_reached = True
                    if not clarification_notice_added:
                        user_text = (
                            f"{user_text}\n"
                            "Служебная пометка: лимит уточнений исчерпан, "
                            "дальше продолжаем с допущениями без новых вопросов."
                        )
                        clarification_notice_added = True
                    _emit_orch(
                        "clarification_limit",
                        "partial",
                        f"Лимит уточнений достигнут ({replan_count}), продолжаем с допущениями",
                        iteration=replan_count,
                    )
                    raise _RestartPlanningAfterClarification()
                raise _AwaitingUserInput("⚠️ Слишком много уточнений. Остановлено.")

            _emit_orch(
                "replan",
                "running",
                "Запрошен replan после late-stage ask_user",
                iteration=replan_count,
            )
            raise _RestartPlanningAfterClarification()

        def _apply_required_input_assumptions_section(
            text: str,
            *,
            user_query: str,
            required_input_gaps: List[str],
        ) -> str:
            normalized_text = _apply_protected_spec_shell(text, user_query=user_query)
            if strict_analyst_runtime_context:
                return normalized_text
            if not protected_spec_shell:
                return normalized_text

            normalized_gaps = _normalize_required_input_gaps(required_input_gaps)
            if not normalized_gaps:
                return normalized_text

            title, _, sections = _split_level_two_sections(normalized_text)
            assumptions_heading_norm = _normalize_shell_heading(assumptions_section)
            open_questions_heading_norm = _normalize_shell_heading(open_questions_section)
            existing_lines: List[str] = []
            filtered_sections: List[tuple[str, str]] = []
            inserted = False
            for heading, content in sections:
                normalized_heading = _normalize_shell_heading(heading)
                if normalized_heading == assumptions_heading_norm:
                    existing_lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
                    continue
                if not inserted and normalized_heading == open_questions_heading_norm:
                    filtered_sections.append((assumptions_section, ""))
                    inserted = True
                filtered_sections.append((heading, content))
            if not inserted:
                filtered_sections.append((assumptions_section, ""))

            required_lines = [f"- {gap}" for gap in normalized_gaps]
            merged_lines: List[str] = [
                "Обязательные входы задачи, которые не удалось закрыть в рамках текущего прогона:",
            ]
            seen_lines = {line for line in merged_lines if line}
            for line in existing_lines + required_lines:
                normalized_line = str(line or "").strip()
                if not normalized_line or normalized_line in seen_lines:
                    continue
                seen_lines.add(normalized_line)
                merged_lines.append(normalized_line)

            final_sections: List[tuple[str, str]] = []
            for heading, content in filtered_sections:
                if _normalize_shell_heading(heading) == assumptions_heading_norm:
                    final_sections.append((assumptions_section, "\n".join(merged_lines).strip()))
                    continue
                final_sections.append((heading, content))

            rendered = _render_level_two_sections(
                title or str(protected_spec_shell.get("title") or "Техническое задание").strip(),
                final_sections,
            )
            return _apply_protected_spec_shell(rendered, user_query=user_query)

        def _merge_reworked_spec_text(*, current_text: str, candidate_text: str, user_query: str) -> str:
            if not protected_spec_shell:
                return str(candidate_text or "").strip()

            current_normalized = _apply_protected_spec_shell(current_text, user_query=user_query)
            candidate_raw = str(candidate_text or "").strip()
            if not candidate_raw:
                return current_normalized

            current_title, _, current_sections = _split_level_two_sections(current_normalized)
            _, candidate_preamble, raw_candidate_sections = _split_level_two_sections(candidate_raw)
            candidate_title, _, normalized_candidate_sections = _split_level_two_sections(
                _apply_protected_spec_shell(candidate_raw, user_query=user_query)
            )

            if not raw_candidate_sections and candidate_preamble:
                first_core_section = next(
                    (
                        str(section or "").strip()
                        for section in (protected_spec_shell.get("core_sections") or [])
                        if str(section or "").strip()
                    ),
                    "",
                )
                if first_core_section:
                    raw_candidate_sections = [(first_core_section, candidate_preamble)]

            normalized_candidate_map = {
                _normalize_shell_heading(heading): (heading, content)
                for heading, content in normalized_candidate_sections
                if str(heading or "").strip()
            }
            candidate_map: Dict[str, tuple[str, str]] = {}
            for heading, content in raw_candidate_sections:
                normalized_heading = _normalize_shell_heading(heading)
                if not normalized_heading:
                    continue
                candidate_map[normalized_heading] = normalized_candidate_map.get(
                    normalized_heading,
                    (str(heading or "").strip(), str(content or "").strip()),
                )

            current_map = {
                _normalize_shell_heading(heading): (heading, content)
                for heading, content in current_sections
                if str(heading or "").strip()
            }
            used_headings: set[str] = set()
            merged_sections: List[tuple[str, str]] = []
            for heading, content in current_sections:
                normalized_heading = _normalize_shell_heading(heading)
                if normalized_heading in candidate_map:
                    merged_sections.append(candidate_map[normalized_heading])
                    used_headings.add(normalized_heading)
                    continue
                merged_sections.append((heading, content))
                if normalized_heading in {
                    _normalize_shell_heading(source_task_section),
                    _normalize_shell_heading(open_questions_section),
                    *{
                        _normalize_shell_heading(item)
                        for item in (protected_spec_shell.get("core_sections") or [])
                        if str(item or "").strip()
                    },
                    _normalize_shell_heading(external_references_section),
                }:
                    self._log.warning(
                        "final rework preservation-first merge kept missing protected section heading=%s",
                        heading,
                    )
            for heading, content in raw_candidate_sections:
                normalized_heading = _normalize_shell_heading(heading)
                if not normalized_heading or normalized_heading in used_headings or normalized_heading in current_map:
                    continue
                merged_sections.append(candidate_map.get(normalized_heading, (heading, content)))

            rendered = _render_level_two_sections(
                current_title or candidate_title or str(protected_spec_shell.get("title") or "Техническое задание").strip(),
                merged_sections,
            )
            return _apply_protected_spec_shell(rendered, user_query=user_query)

        def _extract_ask_user_selected(answer_text: str) -> tuple[str, bool]:
            raw = str(answer_text or "").strip()
            prefixes = (
                "User selected:",
                "Ответ пользователя:",
            )
            for prefix in prefixes:
                if raw.startswith(prefix):
                    return raw[len(prefix):].strip(), True
            return raw, False

        def _norm_title(s: str) -> str:
            s = str(s or "").strip().lower()
            # Keep letters/digits/spaces only for a stable comparison.
            out = []
            for ch in s:
                if ch.isalnum():
                    out.append(ch)
                elif ch.isspace():
                    out.append(" ")
            return " ".join("".join(out).split())

        def _stabilize_step_ids(steps: List[Any]) -> List[Any]:
            """
            Best-effort stabilization of step ids across replans.

            Planner is not guaranteed to keep stable ids. We map by (step_type, title similarity)
            to already-known steps from prior_step_results.
            """
            if not steps or not prior_step_results:
                return steps

            # Build reference candidates from prior results (already executed/skipped).
            ref: List[Dict[str, str]] = []
            for sid, r in prior_step_results.items():
                title = str((r or {}).get("title") or "").strip()
                stype = str((r or {}).get("step_type") or "").strip() or "task"
                if sid and title:
                    ref.append({"id": sid, "title": title, "step_type": stype})
            if not ref:
                return steps

            used_old: set[str] = set()
            used_new: set[str] = {str(s.id) for s in steps if getattr(s, "id", None)}
            # new_id -> old_id
            mapping: Dict[str, str] = {}

            # Prefer exact matches first.
            ref_by_key = {(x["step_type"], _norm_title(x["title"])): x["id"] for x in ref}
            for s in steps:
                new_id = str(getattr(s, "id", "") or "")
                if not new_id:
                    continue
                stype = str(getattr(s, "step_type", "") or "").strip() or "task"
                key = (stype, _norm_title(getattr(s, "title", "") or ""))
                old_id = ref_by_key.get(key)
                if old_id and old_id not in used_old and old_id not in used_new:
                    mapping[new_id] = old_id
                    used_old.add(old_id)
                    used_new.add(old_id)

            # Fuzzy match remaining by normalized title similarity.
            for s in steps:
                new_id = str(getattr(s, "id", "") or "")
                if not new_id or new_id in mapping:
                    continue
                title_n = _norm_title(getattr(s, "title", "") or "")
                if not title_n:
                    continue
                stype = str(getattr(s, "step_type", "") or "").strip() or "task"
                best_old = None
                best_ratio = 0.0
                for cand in ref:
                    if cand["id"] in used_old:
                        continue
                    if (cand.get("step_type") or "task") != stype:
                        continue
                    ratio = difflib.SequenceMatcher(None, title_n, _norm_title(cand.get("title") or "")).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_old = cand["id"]
                if best_old and best_ratio >= 0.90 and best_old not in used_new:
                    mapping[new_id] = best_old
                    used_old.add(best_old)
                    used_new.add(best_old)

            if not mapping:
                return steps

            def _rewrite_id(v: str) -> str:
                return mapping.get(v, v)

            # Apply mapping to step ids and dependency edges.
            for s in steps:
                sid = str(getattr(s, "id", "") or "")
                if sid in mapping:
                    s.id = mapping[sid]
            for s in steps:
                deps = [str(d) for d in (getattr(s, "depends_on", None) or []) if d]
                s.depends_on = [_rewrite_id(d) for d in deps if _rewrite_id(d) != str(getattr(s, "id", "") or "")]

            # Ensure uniqueness if any collisions occurred.
            seen: set[str] = set()
            for s in steps:
                base = str(getattr(s, "id", "") or "").strip()
                if not base:
                    base = "step"
                candidate = base
                i = 2
                while candidate in seen:
                    candidate = f"{base}_{i}"
                    i += 1
                s.id = candidate
                seen.add(candidate)

            return steps

        def _record_step_result(entry: Dict[str, Any]) -> None:
            summary = str(entry.get("summary") or "").strip()
            if summary:
                results.append(summary)
            entry["artifacts"] = _collect_artifacts_from_outputs(entry.get("outputs") or [])
            _persist_step_result_artifact(entry)
            step_results.append(entry)
            task_id = str(entry.get("task_id") or "").strip()
            if task_id:
                prior_step_results[task_id] = entry
            sync_hook = getattr(session, _ANALYST_STEP_RESULTS_SYNC_HOOK_ATTR, None)
            if callable(sync_hook):
                try:
                    sync_hook(list(step_results))
                except Exception:
                    self._log.exception(
                        "analyst incremental step-results sync failed session=%s task_id=%s",
                        getattr(session, "id", "?"),
                        task_id or "?",
                    )

        def _replan_heuristic_trigger(text: str) -> bool:
            """
            Cheap pre-filter before calling LLM.
            We only consult the LLM when the last step output hints at a major plan change.
            """
            s = str(text or "").lower()
            if not s:
                return False
            triggers = [
                # RU
                "обнаруж",
                "выяснил",
                "оказал",  # оказалось/оказалась
                "полностью меня",
                "надо поменя",
                "нужно поменя",
                "нужно иначе",
                "не поддерж",
                "несовмест",
                "конфликт",
                "противореч",
                "архитектур",
                "требован",  # новое требование/требуется
                "вместо этого",
                # EN
                "pivot",
                "breaking change",
                "incompatible",
                "deprecated",
                "new requirement",
            ]
            return any(t in s for t in triggers)

        async def _should_replan_after_success(
            *,
            step: Any,
            resp: Any,
            steps_local: List[Any],
            completed_ok_local: set[str],
            completed_fail_local: set[str],
        ) -> tuple[bool, str]:
            """
            Decide whether we should replan after a successful step due to new facts.

            Returns (needs_replan, reason).
            """
            nonlocal replan_checks_done
            if replan_checks_done >= self._MAX_REPLAN_CHECKS:
                return False, "лимит проверок перепланирования исчерпан"
            if step.step_type == "ask_user":
                return False, ""
            status = str(getattr(resp, "status", "") or "")
            if status not in ("ok", "partial"):
                return False, ""
            if (
                analyst_runtime_context
                and repo_grounded_required
                and str(getattr(step, "id", "") or "").strip() == "validate_tz_completeness"
            ):
                return False, ""
            if _is_stable_deterministic_analyst_plan(steps_local):
                return False, ""
            remaining = [s for s in steps_local if s.id not in completed_ok_local and s.id not in completed_fail_local]
            if not remaining:
                return False, ""

            # Build a small text signal for heuristic gating.
            parts: List[str] = []
            parts.append(str(getattr(resp, "summary", "") or ""))
            try:
                for o in (getattr(resp, "outputs", None) or [])[:4]:
                    if not isinstance(o, dict):
                        continue
                    if str(o.get("type") or "") == "text":
                        parts.append(str(o.get("content") or ""))
            except Exception as e:
                self._log.warning("replan signal build degraded for step=%s: %s", getattr(step, "id", "?"), e)
            signal = "\n".join([p for p in parts if p]).strip()
            if not _replan_heuristic_trigger(signal):
                return False, ""

            # LLM check (bounded payload).
            replan_checks_done += 1
            system = (
                "Ты — классификатор необходимости перепланирования.\n"
                "Тебе дан текущий план шагов и результат последнего успешного шага.\n"
                "Реши: нужно ли ПЕРЕСТРОИТЬ план из-за новых фактов, которые существенно меняют оставшуюся работу.\n"
                "Верни строго JSON:\n"
                "{\n"
                '  "needs_replan": true|false,\n'
                '  "reason": "краткая причина"\n'
                "}\n"
                "Правила:\n"
                "- needs_replan=true только если без перепланирования высок риск делать лишнее/неверное.\n"
                "- Не перепланируй из-за косметики, уточнений формулировок или локальных деталей.\n"
                "- reason 1 строка, по делу.\n"
            )
            try:
                plan_payload: List[Dict[str, Any]] = []
                for s in steps_local[:60]:
                    plan_payload.append(
                        {
                            "id": s.id,
                            "title": s.title,
                            "step_type": s.step_type,
                            "depends_on": s.depends_on or [],
                        }
                    )
                last_payload = {
                    "task_id": str(getattr(resp, "task_id", "") or ""),
                    "step_id": str(getattr(step, "id", "") or ""),
                    "title": str(getattr(step, "title", "") or ""),
                    "status": status,
                    "summary": str(getattr(resp, "summary", "") or ""),
                    "signal_preview": signal,
                }
                user = (
                    "План (включая уже выполненные):\n"
                    f"{json.dumps(plan_payload, ensure_ascii=False)}\n\n"
                    "Последний результат:\n"
                    f"{json.dumps(last_payload, ensure_ascii=False)}\n"
                )
                raw = await self._deps.chat_completion(
                    self._config,
                    system,
                    user,
                    response_format={"type": "json_object"},
                )
                if not raw:
                    return False, ""
                payload = loads_safe(raw, strict_first=False)
                if not isinstance(payload, dict):
                    return False, ""
                needs = bool(payload.get("needs_replan"))
                reason = str(payload.get("reason") or "").strip()
                if len(reason) > 180:
                    reason = reason[:177] + "..."
                return needs, reason
            except Exception as e:
                self._log.warning("replan check failed: %s", e)
                return False, ""

        OUTPUT_SPILLOVER_PREVIEW_CHARS = 2000
        OUTPUT_SPILLOVER_THRESHOLD_CHARS = 6000

        def _compact_outputs(
            outputs: List[Dict[str, Any]],
            *,
            task_id: str = "",
            step_type: str = "",
        ) -> List[Dict[str, Any]]:
            compacted: List[Dict[str, Any]] = []
            for o in outputs or []:
                if not isinstance(o, dict):
                    continue
                t = str(o.get("type") or "")
                if t == "text":
                    content = str(o.get("content") or "")
                    path = str(o.get("path") or "").strip()
                    content_preview = content
                    content_spilled = False
                    if len(content) > OUTPUT_SPILLOVER_THRESHOLD_CHARS and content:
                        spill_task = _slug(task_id or "step-output", fallback="step-output")
                        spill_type = _slug(step_type or "task", fallback="task")
                        spill_index = len(orchestrator_artifacts) + len(compacted) + 1
                        filename = f"{spill_task}_{spill_type}_output_{spill_index}.md"
                        path = _write_text_artifact(filename, content)
                        _register_artifact(
                            "step_output_spill",
                            path,
                            title=f"Spilled output for {task_id or 'step'}",
                            meta={
                                "task_id": str(task_id or "").strip(),
                                "step_type": str(step_type or "").strip(),
                                "output_type": t,
                                "content_len": len(content),
                            },
                        )
                        content_preview = content[:OUTPUT_SPILLOVER_PREVIEW_CHARS]
                        content_spilled = True
                    entry = {
                        "type": "text",
                        "content_len": len(content),
                        "content_preview": content_preview,
                    }
                    if path:
                        entry["path"] = path
                    if content_spilled:
                        entry["content_spilled"] = True
                    compacted.append(entry)
                    continue
                # Preserve non-text outputs as-is (e.g., file/image/audio references).
                compacted.append(dict(o))
            return compacted

        def _collect_artifacts_from_outputs(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            artifacts: List[Dict[str, Any]] = []
            for o in outputs or []:
                if not isinstance(o, dict):
                    continue
                t = str(o.get("type") or "")
                # Convention: {"type": "...", "path": "...", "name": "..."}.
                path = o.get("path") or o.get("file_path")
                if not path:
                    continue
                if t not in ("file", "document", "image", "audio", "video"):
                    # Keep it permissive: any output with a path is a candidate artifact.
                    t = "file"
                artifacts.append({"type": t, "path": str(path), "name": str(o.get("name") or "")})
            return artifacts

        orchestrator_artifacts: List[Dict[str, Any]] = []

        def _artifact_dir() -> str:
            return _resolve_intermediate_artifacts_dir(session, cwd)

        def _slug(value: str, *, fallback: str = "artifact") -> str:
            text = str(value or "").strip().lower()
            out_chars: List[str] = []
            prev_dash = False
            for ch in text:
                if ch.isalnum():
                    out_chars.append(ch)
                    prev_dash = False
                    continue
                if ch in {"-", "_"} and not prev_dash:
                    out_chars.append("-")
                    prev_dash = True
                    continue
                if not prev_dash:
                    out_chars.append("-")
                    prev_dash = True
            normalized = "".join(out_chars).strip("-")
            return normalized or fallback

        def _write_text_artifact(filename: str, text: str) -> str:
            path = os.path.join(_artifact_dir(), filename)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(text or "").rstrip() + "\n")
            return path

        def _persist_analyst_final_candidate(text: str, *, stage: str) -> str:
            if not analyst_runtime_context:
                return ""
            candidate_path = str(
                getattr(session, "analyst_runtime_final_candidate_path", "") or ""
            ).strip()
            candidate_text = str(text or "").strip()
            if not candidate_path or not candidate_text:
                return ""
            try:
                os.makedirs(os.path.dirname(candidate_path), exist_ok=True)
                with open(candidate_path, "w", encoding="utf-8") as fh:
                    fh.write(candidate_text.rstrip() + "\n")
            except Exception as exc:
                self._log.warning(
                    "compose_final_answer failed to persist staged candidate session=%s stage=%s path=%s err=%s",
                    getattr(session, "id", ""),
                    str(stage or "").strip() or "unknown",
                    candidate_path,
                    exc,
                )
                return ""
            self._log.info(
                "compose_final_answer staged candidate session=%s stage=%s path=%s output_len=%d",
                getattr(session, "id", ""),
                str(stage or "").strip() or "unknown",
                candidate_path,
                len(candidate_text),
            )
            return candidate_path

        def _prepare_cli_prompt_via_artifact(
            prompt_text: str,
            *,
            filename_hint: str,
            response_format: str = "",
        ) -> str:
            raw_prompt = str(prompt_text or "")
            prompt_bytes = len(raw_prompt.encode("utf-8"))
            if prompt_bytes < _CLI_PROMPT_ARTIFACT_THRESHOLD_BYTES:
                return raw_prompt
            prompt_artifact_path = _write_text_artifact(filename_hint, raw_prompt)
            try:
                prompt_ref = os.path.relpath(prompt_artifact_path, cwd)
            except Exception:
                prompt_ref = prompt_artifact_path
            prompt_ref = str(prompt_ref or "").replace(os.sep, "/")
            wrapper_lines = [
                f"Сначала полностью прочитай файл @{prompt_ref} без сокращений, пропусков и выборочного чтения.",
                "Смысл работы находится в этом файле; опирайся на него целиком, а не на краткий пересказ.",
                "После полного чтения выполни все инструкции из файла строго и целиком.",
            ]
            if response_format:
                wrapper_lines.extend(
                    [
                        "",
                        f"CLI_RESPONSE_FORMAT: {str(response_format or '').strip()}",
                    ]
                )
            self._log.info(
                "spilled large cli prompt to artifact session=%s path=%s bytes=%d format=%s",
                getattr(session, "id", ""),
                prompt_ref,
                prompt_bytes,
                str(response_format or "").strip(),
            )
            return "\n".join(wrapper_lines)

        def _file_sha1(path: str) -> str:
            candidate = str(path or "").strip()
            if not candidate or not os.path.exists(candidate):
                return ""
            digest = hashlib.sha1()
            with open(candidate, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()

        def _build_artifact_binding(path: str) -> Dict[str, str]:
            candidate = str(path or "").strip()
            if not candidate:
                return {"path": "", "sha1": ""}
            return {
                "path": candidate,
                "sha1": _file_sha1(candidate),
            }

        def _register_artifact(kind: str, path: str, *, title: str = "", meta: Optional[Dict[str, Any]] = None) -> str:
            record = {
                "kind": str(kind or "artifact"),
                "path": str(path or "").strip(),
                "title": str(title or "").strip(),
                "meta": dict(meta or {}),
            }
            existing_idx = next(
                (
                    idx
                    for idx, item in enumerate(orchestrator_artifacts)
                    if str(item.get("path") or "").strip() == record["path"]
                ),
                None,
            )
            if existing_idx is None:
                orchestrator_artifacts.append(record)
            else:
                orchestrator_artifacts[existing_idx] = record
            return record["path"]

        def _write_artifacts_index() -> str:
            payload = {
                "session_id": str(getattr(session, "id", "") or ""),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "artifacts": orchestrator_artifacts,
            }
            path = _write_text_artifact(
                f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_artifacts_index.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
            _register_artifact("artifact_index", path, title="Artifacts index")
            return path

        def _render_step_result_artifact(entry: Dict[str, Any]) -> str:
            task_id = str(entry.get("task_id") or "").strip() or "unknown_step"
            title = str(entry.get("title") or "").strip() or task_id
            status = str(entry.get("status") or "").strip() or "unknown"
            step_type = str(entry.get("step_type") or "").strip() or "task"
            lines = [
                f"# Шаг {task_id}",
                "",
                f"- title: {title}",
                f"- step_type: {step_type}",
                f"- status: {status}",
            ]
            summary = str(entry.get("summary") or "").strip()
            if summary:
                lines.extend(["", "## Summary", "", summary])
            ask_question = str(entry.get("ask_question") or "").strip()
            ask_options = entry.get("ask_options") or []
            if ask_question:
                lines.extend(["", "## Ask User", "", f"- question: {ask_question}"])
                if isinstance(ask_options, list) and ask_options:
                    lines.append("- options:")
                    for option in ask_options:
                        text = str(option or "").strip()
                        if text:
                            lines.append(f"  - {text}")
            outputs = entry.get("outputs") or []
            if isinstance(outputs, list) and outputs:
                lines.extend(["", "## Outputs", ""])
                for idx, output in enumerate(outputs, start=1):
                    if not isinstance(output, dict):
                        continue
                    out_type = str(output.get("type") or "text").strip()
                    lines.append(f"### Output {idx} ({out_type})")
                    preview = str(output.get("content_preview") or output.get("content") or "").strip()
                    if preview:
                        lines.extend(["", preview, ""])
                    path = str(output.get("path") or output.get("file_path") or "").strip()
                    if path:
                        lines.append(f"- path: {path}")
                    name = str(output.get("name") or "").strip()
                    if name:
                        lines.append(f"- name: {name}")
                    lines.append("")
            artifacts = entry.get("artifacts") or []
            if isinstance(artifacts, list) and artifacts:
                lines.extend(["## Referenced artifacts", ""])
                for artifact in artifacts:
                    if not isinstance(artifact, dict):
                        continue
                    lines.append(
                        f"- {str(artifact.get('path') or '').strip()} ({str(artifact.get('type') or 'file').strip()})"
                    )
                lines.append("")
            return "\n".join(lines).strip()

        def _persist_step_result_artifact(entry: Dict[str, Any]) -> None:
            try:
                task_id = str(entry.get("task_id") or "").strip() or f"step-{len(step_results) + 1}"
                filename = f"{_slug(task_id, fallback='step')}.md"
                path = _write_text_artifact(filename, _render_step_result_artifact(entry))
                entry["orchestrator_artifact"] = path
                _register_artifact(
                    "step_result",
                    path,
                    title=str(entry.get("title") or task_id),
                    meta={
                        "task_id": task_id,
                        "status": str(entry.get("status") or "").strip(),
                        "step_type": str(entry.get("step_type") or "").strip(),
                    },
                )
                _write_artifacts_index()
            except Exception as e:
                self._log.warning("failed to persist step artifact task_id=%s err=%s", entry.get("task_id"), e)

        followup_repo_review_outputs: List[Dict[str, Any]] = []
        followup_obligation_review_payload: Dict[str, Any] = {}
        spec_fix_payload: Dict[str, Any] = {}
        runtime_degraded_modes: List[str] = []
        supplemental_claim_entries: List[Dict[str, Any]] = []
        compose_final_answer_normalize_fallback_used = False
        structured_bundle_calls = 0
        structured_bundle_successes = 0
        cli_fallbacks = 0
        retry_successes = 0
        retry_exhausted = 0
        structured_bundle_stage_stats: Dict[str, Dict[str, int]] = {
            "gap_closure": {"calls": 0, "successes": 0, "fallbacks": 0, "retry_successes": 0, "retry_exhausted": 0},
            "followup_review": {"calls": 0, "successes": 0, "fallbacks": 0, "retry_successes": 0, "retry_exhausted": 0},
        }
        last_final_assessment: Dict[str, Any] = {}
        last_model_text_before_runtime = ""
        task_contract_payload: Dict[str, Any] = {}
        claim_ledger: List[Dict[str, Any]] = []
        fact_pack_path = ""
        claim_ledger_path = ""
        draft_path = ""
        polished_path = ""
        open_gaps_path = ""
        artifacts_index_path = ""
        task_contract_path = ""
        obligation_matrix_path = ""
        blocking_stage_states: Dict[str, Dict[str, Any]] = {}
        stage_missing_required_artifacts: set[str] = set()

        def _ensure_stage_stats(stage_name: str) -> Dict[str, int]:
            key = str(stage_name or "unknown").strip() or "unknown"
            return structured_bundle_stage_stats.setdefault(
                key,
                {"calls": 0, "successes": 0, "fallbacks": 0, "retry_successes": 0, "retry_exhausted": 0},
            )

        def _set_blocking_stage_state(
            stage_id: str,
            *,
            attempts: int,
            max_attempts: int,
            failure_kind: str = "",
            retry_context: str = "",
            final_status: str,
        ) -> None:
            blocking_stage_states[str(stage_id or "").strip() or "stage"] = {
                "attempts": max(0, int(attempts)),
                "max_attempts": max(1, int(max_attempts)),
                "failure_kind": str(failure_kind or "").strip(),
                "retry_context": str(retry_context or "").strip(),
                "final_status": str(final_status or "").strip() or "unknown",
            }

        def _append_runtime_degraded_mode(reason: str) -> None:
            text = str(reason or "").strip()
            if text and text not in runtime_degraded_modes:
                runtime_degraded_modes.append(text)

        def _missing_required_artifacts(
            *,
            draft_override: str = "",
            require_open_gaps: bool = False,
            require_obligation_matrix: bool = False,
        ) -> List[str]:
            required_pairs = [
                ("task_contract", task_contract_path),
                ("claim_ledger", claim_ledger_path),
                ("fact_pack", fact_pack_path),
                ("draft", draft_override or draft_path),
                ("artifacts_index", artifacts_index_path),
            ]
            if require_open_gaps:
                required_pairs.append(("open_gaps", open_gaps_path))
            if require_obligation_matrix:
                required_pairs.append(("obligation_matrix", obligation_matrix_path))
            return [name for name, path in required_pairs if not str(path or "").strip()]

        def _build_spec_fix_supplemental_entry() -> Optional[Dict[str, Any]]:
            claims = list(spec_fix_payload.get("claims") or [])
            evidence_items = list(spec_fix_payload.get("evidence") or [])
            corrections = [
                str(item).strip()
                for item in (spec_fix_payload.get("corrections_applied") or [])
                if str(item).strip()
            ]
            if not claims and not evidence_items and not corrections:
                return None
            outputs: List[Dict[str, Any]] = []
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue
                ref_path = str(item.get("path") or "").strip()
                preview = str(item.get("preview") or "").strip()
                if not ref_path and not preview:
                    continue
                outputs.append(
                    {
                        "type": str(item.get("type") or "file").strip() or "file",
                        "path": ref_path,
                        "content_preview": preview,
                    }
                )
            artifacts = [{"type": "md", "path": polished_path}] if str(polished_path or "").strip() else []
            summary = "; ".join(corrections[:3]).strip() or "repo-grounded gap closure"
            return {
                "task_id": "final_cli_gap_closure",
                "title": "Final CLI gap closure",
                "status": "ok",
                "summary": summary,
                "outputs": outputs,
                "claims": claims,
                "artifacts": artifacts,
                "orchestrator_artifact": str(polished_path or "").strip(),
            }

        def _refresh_supporting_runtime_artifacts(
            *,
            supplemental_entries: Optional[List[Dict[str, Any]]] = None,
        ) -> None:
            nonlocal claim_ledger, claim_ledger_path, fact_pack_path, artifacts_index_path
            claim_ledger, claim_ledger_path = _persist_claim_ledger_for_compose(
                step_results,
                supplemental_entries=supplemental_entries,
            )
            fact_pack_text = _build_fact_pack_text(
                user_query=raw_user_query,
                plan_steps_local=steps,
                step_results_local=step_results,
                supplemental_entries=supplemental_entries,
            )
            fact_pack_path = _write_text_artifact(
                f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_fact_pack.md",
                fact_pack_text,
            )
            _register_artifact("fact_pack", fact_pack_path, title="Fact pack")
            artifacts_index_path = _write_artifacts_index()

        def _build_stage_bundle_assessment_payload(assessment_payload: Dict[str, Any]) -> Dict[str, Any]:
            def _merge_unique(*groups: List[str]) -> List[str]:
                merged: List[str] = []
                seen: set[str] = set()
                for group in groups:
                    for item in group:
                        text = str(item).strip()
                        if not text or text in seen:
                            continue
                        seen.add(text)
                        merged.append(text)
                return merged

            merged_payload = dict(assessment_payload or {})
            merged_payload["missing_required_artifacts"] = _merge_unique(
                [str(item).strip() for item in (merged_payload.get("missing_required_artifacts") or []) if str(item).strip()],
                list(stage_missing_required_artifacts),
            )
            merged_payload["fix_closed_obligations"] = _merge_unique(
                [str(item).strip() for item in (merged_payload.get("fix_closed_obligations") or []) if str(item).strip()],
                [str(item).strip() for item in (spec_fix_payload.get("closed_obligations") or []) if str(item).strip()],
            )
            merged_payload["fix_remaining_obligations"] = list(
                spec_fix_payload.get("remaining_obligations")
                or merged_payload.get("fix_remaining_obligations")
                or []
            )
            merged_payload["followup_closed_blocking_obligations"] = _merge_unique(
                [
                    str(item).strip()
                    for item in (merged_payload.get("followup_closed_blocking_obligations") or [])
                    if str(item).strip()
                ],
                [
                    str(item).strip()
                    for item in (followup_obligation_review_payload.get("closed_blocking_obligations") or [])
                    if str(item).strip()
                ],
            )
            if followup_obligation_review_payload:
                merged_payload["followup_open_blocking_obligations"] = list(
                    followup_obligation_review_payload.get("open_blocking_obligations") or []
                )
                merged_payload["followup_false_closures"] = list(
                    followup_obligation_review_payload.get("false_closures") or []
                )
            return merged_payload

        def _structured_stage_name_for_step(step: Any, response_format: str) -> str:
            step_id = str(getattr(step, "id", "") or "").strip().lower()
            normalized = str(response_format or "").strip().lower()
            if normalized == CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON:
                if step_id == "use_cli_repo_final_review":
                    return "final_review"
                return f"repo_review_step:{step_id or 'unknown'}"
            if normalized == CLIResponseFormat.CLAIM_BUNDLE_JSON:
                return f"claim_bundle_step:{step_id or 'unknown'}"
            if normalized == CLIResponseFormat.JSON_OBJECT:
                return f"json_object_step:{step_id or 'unknown'}"
            return f"structured_use_cli:{step_id or 'unknown'}"

        def _record_structured_use_cli_step_metrics(step: Any, resp: Any) -> None:
            nonlocal structured_bundle_calls, structured_bundle_successes, cli_fallbacks, retry_successes, retry_exhausted
            response_format = str(getattr(step, "_use_cli_response_format", "") or "").strip()
            if not response_format:
                return
            stage_name = _structured_stage_name_for_step(step, response_format)
            stats = _ensure_stage_stats(stage_name)
            structured_bundle_calls += 1
            stats["calls"] += 1

            outputs = list(getattr(resp, "outputs", []) or [])
            output_types = {
                str(item.get("type") or "").strip()
                for item in outputs
                if isinstance(item, dict)
            }
            degraded_texts = [
                str(item.get("content_preview") or item.get("content") or "").strip()
                for item in outputs
                if isinstance(item, dict) and str(item.get("type") or "").strip() == CLIOutputType.DEGRADED_MODE
            ]
            has_degraded = bool(degraded_texts)
            has_retry_notice = CLIOutputType.CLI_RETRY_NOTICE in output_types
            if has_retry_notice:
                retry_successes += 1
                stats["retry_successes"] += 1
            if any("retry_exhausted" in text for text in degraded_texts):
                retry_exhausted += 1
                stats["retry_exhausted"] += 1

            structured_success = getattr(resp, "status", "") == "ok" and not has_degraded
            if structured_success:
                structured_bundle_successes += 1
                stats["successes"] += 1
            else:
                cli_fallbacks += 1
                stats["fallbacks"] += 1

        def _persist_analyst_quality_metrics(
            *,
            claim_ledger_local: List[Dict[str, Any]],
            assessment_local: Dict[str, Any],
            required_step_statuses: Dict[str, str],
            model_text_before_runtime: str,
        ) -> None:
            handle = getattr(session, "analyst_run_artifact_handle", None)
            metrics_path = str(getattr(handle, "metrics_path", "") or "").strip()
            if not metrics_path:
                return
            state_path = str(getattr(handle, "state_path", "") or "").strip()
            template_resolution: Dict[str, Any] = {}
            if state_path:
                try:
                    state_payload = self._deps.read_json_locked(state_path, default={})
                    mode_context = state_payload.get("mode_context") if isinstance(state_payload, dict) else {}
                    input_bundle = mode_context.get("input_bundle") if isinstance(mode_context, dict) else {}
                    raw_template_resolution = input_bundle.get("template_resolution") if isinstance(input_bundle, dict) else {}
                    template_resolution = (
                        dict(raw_template_resolution or {}) if isinstance(raw_template_resolution, dict) else {}
                    )
                except Exception as e:
                    self._log.warning(
                        "failed to load template_resolution for analyst quality metrics path=%s err=%s",
                        state_path,
                        e,
                    )
            quality = build_analyst_quality_metrics(
                claim_ledger=claim_ledger_local,
                assessment=assessment_local,
                repo_grounded_required=repo_grounded_required,
                required_step_statuses=required_step_statuses,
                model_text_before_runtime=model_text_before_runtime,
                structured_bundle_calls=structured_bundle_calls,
                structured_bundle_successes=structured_bundle_successes,
                cli_fallbacks=cli_fallbacks,
                retry_successes=retry_successes,
                retry_exhausted=retry_exhausted,
                structured_bundle_stage_stats=structured_bundle_stage_stats,
                template_resolution=template_resolution,
            )

            def _updater(current: Dict[str, Any]) -> Dict[str, Any]:
                payload = dict(current or {}) if isinstance(current, dict) else {}
                payload["analyst_quality"] = quality
                return payload

            try:
                self._deps.update_json_locked(metrics_path, _updater, default={})
            except Exception as e:
                self._log.warning("failed to persist analyst quality metrics path=%s err=%s", metrics_path, e)

        def _build_claim_ledger(
            step_results_local: List[Dict[str, Any]],
            *,
            supplemental_entries: Optional[List[Dict[str, Any]]] = None,
        ) -> List[Dict[str, Any]]:
            def _claim_status(entry: Dict[str, Any]) -> str:
                status = str(entry.get("status") or "").strip().lower()
                if status == "ok":
                    return "confirmed"
                if status in {"partial", "needs_input"}:
                    return "needs_check"
                if status in {"error", "blocked"}:
                    return "unconfirmed"
                return "needs_check"

            def _fallback_claim_text(entry: Dict[str, Any], step_evidence: List[Dict[str, Any]]) -> str:
                if repo_grounded_required:
                    return ""
                for ev in step_evidence:
                    preview = " ".join(str(ev.get("preview") or "").split()).strip()
                    if preview:
                        return preview
                return " ".join(str(entry.get("summary") or "").split()).strip()

            ledger: List[Dict[str, Any]] = []
            for item in list(step_results_local) + list(supplemental_entries or []):
                if not isinstance(item, dict):
                    continue
                task_id = str(item.get("task_id") or "").strip() or "(unknown)"
                title = str(item.get("title") or "").strip()
                explicit_claims = item.get("claims") or []
                step_evidence = collect_step_evidence(item)
                if isinstance(explicit_claims, list) and explicit_claims:
                    for idx, claim in enumerate(explicit_claims, start=1):
                        if not isinstance(claim, dict):
                            continue
                        claim_text = str(claim.get("text") or "").strip()
                        if not claim_text:
                            continue
                        evidence = claim.get("evidence") or []
                        deduped: List[Dict[str, Any]] = []
                        seen_keys: set[tuple[str, str, str]] = set()
                        if isinstance(evidence, list):
                            for ev in evidence:
                                if not isinstance(ev, dict):
                                    continue
                                key = (
                                    str(ev.get("type") or ""),
                                    str(ev.get("path") or ""),
                                    str(ev.get("preview") or ""),
                                )
                                if key in seen_keys:
                                    continue
                                seen_keys.add(key)
                                deduped.append(
                                    {
                                        "type": str(ev.get("type") or "text").strip() or "text",
                                        "path": str(ev.get("path") or "").strip(),
                                        "preview": str(ev.get("preview") or "").strip(),
                                    }
                                )
                        for ev in step_evidence:
                            key = (
                                str(ev.get("type") or ""),
                                str(ev.get("path") or ""),
                                str(ev.get("preview") or ""),
                            )
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)
                            deduped.append(dict(ev))
                        claim_status = str(claim.get("status") or _claim_status(item)).strip() or _claim_status(item)
                        if repo_grounded_required and str(claim_status).strip().lower() == "confirmed":
                            if not claim_has_repo_anchor({"evidence": deduped}):
                                claim_status = "needs_check"
                        ledger.append(
                            {
                                "claim_id": str(claim.get("claim_id") or f"claim_{task_id}_{idx}").strip(),
                                "task_id": task_id,
                                "source_step_id": task_id,
                                "title": title,
                                "status": claim_status,
                                "text": claim_text,
                                "evidence": deduped,
                                "component_scope": str(claim.get("component_scope") or "general").strip() or "general",
                                "allowed_final_usage": str(claim.get("allowed_final_usage") or "").strip(),
                                "step_artifact": str(item.get("orchestrator_artifact") or "").strip(),
                            }
                        )
                    continue
                fallback_claim_text = _fallback_claim_text(item, step_evidence)
                if not fallback_claim_text:
                    continue
                ledger.append(
                    {
                        "claim_id": f"claim_{task_id}_fallback",
                        "task_id": task_id,
                        "source_step_id": task_id,
                        "title": title,
                        "status": _claim_status(item),
                        "text": fallback_claim_text,
                        "evidence": [dict(ev) for ev in step_evidence],
                        "component_scope": "general",
                        "allowed_final_usage": "",
                        "step_artifact": str(item.get("orchestrator_artifact") or "").strip(),
                    }
                )
            return normalize_claim_ledger(ledger)

        def _build_fact_pack_text(
            *,
            user_query: str,
            plan_steps_local: List[Any],
            step_results_local: List[Dict[str, Any]],
            supplemental_entries: Optional[List[Dict[str, Any]]] = None,
        ) -> str:
            claim_ledger = _build_claim_ledger(
                step_results_local,
                supplemental_entries=supplemental_entries,
            )

            lines = [
                "# Fact Pack",
                "",
                "Этот пакет — исходный набор фактов для финального документа.",
                "Используй только подтвержденные наблюдения из шагов и явно помеченные неподтвержденные зоны.",
                "",
                "## User Query",
                "",
                str(user_query or "").strip() or "(empty)",
                "",
                "## Plan",
                "",
            ]
            for step in plan_steps_local:
                lines.append(
                    f"- {str(getattr(step, 'id', '') or '').strip()}: "
                    f"{str(getattr(step, 'title', '') or '').strip()} "
                    f"({str(getattr(step, 'step_type', '') or 'task').strip()})"
                )
            lines.extend(["", "## Step Facts", ""])
            for item in list(step_results_local) + list(supplemental_entries or []):
                if not isinstance(item, dict):
                    continue
                task_id = str(item.get("task_id") or "").strip() or "(unknown)"
                status = str(item.get("status") or "").strip() or "unknown"
                claim_status = next(
                    (
                        str(claim.get("status") or "").strip()
                        for claim in claim_ledger
                        if str(claim.get("task_id") or "").strip() == task_id
                    ),
                    "needs_check",
                )
                lines.append(f"### {task_id}")
                lines.append("")
                lines.append(f"- status: {status}")
                lines.append(f"- claim_status: {claim_status}")
                title = str(item.get("title") or "").strip()
                if title:
                    lines.append(f"- title: {title}")
                summary = str(item.get("summary") or "").strip()
                if summary:
                    lines.extend(["- summary:", "", summary, ""])
                outputs = item.get("outputs") or []
                if isinstance(outputs, list) and outputs:
                    lines.append("- evidence_previews:")
                    for output in outputs[:5]:
                        if not isinstance(output, dict):
                            continue
                        preview = str(output.get("content_preview") or output.get("content") or "").strip()
                        if preview:
                            lines.append(f"  - {' '.join(preview.split())}")
                        path = str(output.get("path") or output.get("file_path") or "").strip()
                        if path:
                            lines.append(f"  - file: {path}")
                artifacts = item.get("artifacts") or []
                if isinstance(artifacts, list) and artifacts:
                    lines.append("- referenced_artifacts:")
                    for artifact in artifacts:
                        if not isinstance(artifact, dict):
                            continue
                        path = str(artifact.get("path") or "").strip()
                        if path:
                            lines.append(f"  - {path}")
                artifact_path = str(item.get("orchestrator_artifact") or "").strip()
                if artifact_path:
                    lines.append(f"- step_artifact: {artifact_path}")
                lines.append("")
            ledger_validation = validate_claim_ledger(claim_ledger)
            verification = verify_claim_ledger(claim_ledger, repo_grounded_required=repo_grounded_required)
            lines.extend(["## Claim Ledger", ""])
            for claim in claim_ledger:
                task_id = str(claim.get("task_id") or "").strip() or "(unknown)"
                claim_text = str(claim.get("text") or "").strip()
                claim_status = str(claim.get("status") or "").strip()
                evidence_items = claim.get("evidence") or []
                lines.append(f"### CLAIM {task_id}")
                lines.append("")
                lines.append(f"- status: {claim_status}")
                lines.append(f"- text: {claim_text}")
                if evidence_items:
                    lines.append("- evidence:")
                    for ev in evidence_items:
                        if not isinstance(ev, dict):
                            continue
                        ref = str(ev.get("path") or ev.get("preview") or "").strip()
                        if ref:
                            lines.append(f"  - {ref}")
                else:
                    lines.append("- evidence: (not captured)")
                lines.append("")
            if ledger_validation.get("warnings"):
                lines.extend(["## Claim Ledger Warnings", ""])
                for item in ledger_validation.get("warnings") or []:
                    lines.append(f"- {item}")
                lines.append("")
            if ledger_validation.get("errors"):
                lines.extend(["## Claim Ledger Errors", ""])
                for item in ledger_validation.get("errors") or []:
                    lines.append(f"- {item}")
                lines.append("")
            lines.extend(["## Evidence Verification", ""])
            evidence_gaps = verification.get("evidence_gaps") or []
            if evidence_gaps:
                for item in evidence_gaps:
                    lines.append(f"- {str(item).strip()}")
            else:
                lines.append("- evidence gaps not detected")
            return "\n".join(lines).strip()

        def _persist_claim_ledger_for_compose(
            step_results_local: List[Dict[str, Any]],
            *,
            supplemental_entries: Optional[List[Dict[str, Any]]] = None,
        ) -> tuple[List[Dict[str, Any]], str]:
            """Build claim ledger, write to file, return (ledger, path)."""
            try:
                ledger = _build_claim_ledger(
                    step_results_local,
                    supplemental_entries=supplemental_entries,
                )
                path = _write_text_artifact(
                    f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_claim_ledger.json",
                    json.dumps(ledger, ensure_ascii=False, indent=2),
                )
                _register_artifact("claim_ledger", path, title="Claim ledger")
                return ledger, path
            except Exception as exc:
                self._log.warning("failed to persist claim ledger: %s", exc)
                return [], ""

        def _recover_compose_final_answer_normalize_error(content: str, exc: Exception) -> str | None:
            nonlocal compose_final_answer_normalize_fallback_used
            source = strip_outer_code_fence(strip_ansi(str(content or "")).strip())
            if not source:
                return None

            def _extract_truncated_final_text(raw_text: str) -> str:
                key = '"final_text"'
                key_pos = raw_text.find(key)
                if key_pos < 0:
                    return ""
                colon_pos = raw_text.find(":", key_pos + len(key))
                if colon_pos < 0:
                    return ""
                idx = colon_pos + 1
                while idx < len(raw_text) and raw_text[idx].isspace():
                    idx += 1
                if idx >= len(raw_text) or raw_text[idx] != '"':
                    return ""
                idx += 1
                out: List[str] = []
                escaped = False
                while idx < len(raw_text):
                    ch = raw_text[idx]
                    if escaped:
                        if ch == "n":
                            out.append("\n")
                        elif ch == "r":
                            out.append("\r")
                        elif ch == "t":
                            out.append("\t")
                        elif ch == "b":
                            out.append("\b")
                        elif ch == "f":
                            out.append("\f")
                        elif ch in {'"', "\\", "/"}:
                            out.append(ch)
                        elif ch == "u" and idx + 4 < len(raw_text):
                            codepoint = raw_text[idx + 1:idx + 5]
                            if all(c in "0123456789abcdefABCDEF" for c in codepoint):
                                out.append(chr(int(codepoint, 16)))
                                idx += 4
                            else:
                                out.append(ch)
                        else:
                            out.append(ch)
                        escaped = False
                        idx += 1
                        continue
                    if ch == "\\":
                        escaped = True
                        idx += 1
                        continue
                    if ch == '"':
                        break
                    out.append(ch)
                    idx += 1
                if escaped:
                    out.append("\\")
                return "".join(out).strip()

            candidate = _extract_truncated_final_text(source)
            if not candidate and not (source.startswith("{") or source.startswith("[")):
                candidate = source
            candidate = candidate.strip()
            if not candidate:
                return None
            if _contains_internal_runtime_markup(candidate) or _looks_like_nonfinal_rework_text(candidate):
                return None
            compose_final_answer_normalize_fallback_used = True
            self._log.warning("compose_final_answer normalize fallback used: %s", exc)
            return json.dumps({"final_text": candidate}, ensure_ascii=False)

        def _extract_command_candidates(*texts: Any, limit: int = 6) -> List[str]:
            markers = (".venv/bin/pytest", "pytest", "playwright", "npm test", "pnpm", "yarn")
            normalized: List[str] = []
            seen: set[str] = set()
            for text in texts:
                for raw_line in str(text or "").splitlines():
                    line = " ".join(str(raw_line or "").split()).strip().strip("`")
                    if not line or not any(marker in line for marker in markers):
                        continue
                    if line in seen:
                        continue
                    seen.add(line)
                    normalized.append(line)
                    if len(normalized) >= limit:
                        return normalized
            return normalized

        def _collect_repo_path_candidates_from_step_results(
            step_results_local: List[Dict[str, Any]],
            *,
            limit: int = 8,
        ) -> List[str]:
            repo_root = str(self._repo_step_root(session) or "").strip()
            normalized: List[str] = []
            seen: set[str] = set()

            def _push(path_value: Any) -> None:
                path = str(path_value or "").strip()
                if not path or ".cli-proxy/" in path.replace("\\", "/"):
                    return
                if repo_root and path.startswith(repo_root.rstrip(os.sep) + os.sep):
                    try:
                        path = os.path.relpath(path, repo_root)
                    except Exception:
                        path = path
                if path in seen:
                    return
                seen.add(path)
                normalized.append(path)

            for item in step_results_local:
                if not isinstance(item, dict):
                    continue
                for evidence in collect_step_evidence(item):
                    if not isinstance(evidence, dict):
                        continue
                    _push(evidence.get("path"))
                    if len(normalized) >= limit:
                        return normalized
                outputs = item.get("outputs") or []
                if not isinstance(outputs, list):
                    continue
                for output in outputs:
                    if not isinstance(output, dict):
                        continue
                    _push(output.get("path") or output.get("file_path"))
                    if len(normalized) >= limit:
                        return normalized
            return normalized

        def _collect_command_candidates_from_step_results(
            step_results_local: List[Dict[str, Any]],
            *,
            limit: int = 6,
        ) -> List[str]:
            fragments: List[str] = []
            for item in step_results_local:
                if not isinstance(item, dict):
                    continue
                summary = str(item.get("summary") or "").strip()
                if summary:
                    fragments.append(summary)
                outputs = item.get("outputs") or []
                if not isinstance(outputs, list):
                    continue
                for output in outputs:
                    if not isinstance(output, dict):
                        continue
                    preview = str(output.get("content_preview") or output.get("content") or "").strip()
                    if preview:
                        fragments.append(preview)
            return _extract_command_candidates(*fragments, limit=limit)

        def _repair_required_top_level_sections_from_fallback(
            *,
            current_text: str,
            user_query: str,
            required_sections: List[str],
            step_results_local: List[Dict[str, Any]],
            reason: str,
        ) -> str:
            if not analyst_runtime_context or not protected_spec_shell:
                return ""
            fallback_text = _build_template_aware_final_answer_fallback(
                user_query=user_query,
                step_results_local=step_results_local,
                reason=reason,
            )
            if not fallback_text:
                return ""

            normalized_current = _apply_protected_spec_shell(current_text, user_query=user_query)
            normalized_fallback = _apply_protected_spec_shell(fallback_text, user_query=user_query)
            title, _, current_sections = _split_level_two_sections(normalized_current)
            fallback_title, _, fallback_sections = _split_level_two_sections(normalized_fallback)
            current_map = {
                _normalize_shell_heading(heading): (heading, content)
                for heading, content in current_sections
                if str(heading or "").strip()
            }
            fallback_map = {
                _normalize_shell_heading(heading): (heading, content)
                for heading, content in fallback_sections
                if str(heading or "").strip()
            }

            ordered_headings: List[str] = []
            seen_headings: set[str] = set()

            def _append_heading(heading: str) -> None:
                normalized = _normalize_shell_heading(heading)
                if not normalized or normalized in seen_headings:
                    return
                seen_headings.add(normalized)
                ordered_headings.append(heading)

            _append_heading(source_task_section)
            for heading in required_sections or []:
                _append_heading(heading)
            _append_heading(open_questions_section)

            merged_sections: List[tuple[str, str]] = []
            open_questions_pair: Optional[tuple[str, str]] = None
            for heading in ordered_headings:
                normalized = _normalize_shell_heading(heading)
                pair = current_map.get(normalized) or fallback_map.get(normalized)
                if not pair:
                    continue
                if normalized == _normalize_shell_heading(open_questions_section):
                    open_questions_pair = pair
                    continue
                merged_sections.append(pair)

            for heading, content in current_sections:
                normalized = _normalize_shell_heading(heading)
                if normalized in seen_headings:
                    continue
                merged_sections.append((heading, content))
                seen_headings.add(normalized)

            if open_questions_pair is not None:
                merged_sections.append(open_questions_pair)

            rendered = _render_level_two_sections(
                title or fallback_title or str(protected_spec_shell.get("title") or "Техническое задание").strip(),
                merged_sections,
            )
            return _apply_protected_spec_shell(rendered, user_query=user_query)

        async def _compose_final_answer_text(
            *,
            user_query: str,
            plan_steps_local: List[Any],
            step_results_local: List[Dict[str, Any]],
        ) -> str:
            nonlocal compose_final_answer_normalize_fallback_used
            compose_final_answer_normalize_fallback_used = False

            def _normalize_compose_finding(value: Any, *, limit: int = 320) -> str:
                text = " ".join(str(value or "").split()).strip()
                if not text:
                    return ""
                if len(text) <= limit:
                    return text
                return text[: limit - 1].rstrip() + "…"

            def _collect_compose_critical_findings(items: List[Dict[str, Any]]) -> List[str]:
                review_step_ids = {
                    "validate_tz_completeness",
                    "use_cli_repo_final_review",
                    "followup_obligation_review",
                }
                review_output_types = {
                    CLIOutputType.REPO_REVIEW_MISMATCH,
                    CLIOutputType.REPO_REVIEW_UNVERIFIED_CLAIM,
                    CLIOutputType.REPO_REVIEW_CORRECTION,
                    CLIOutputType.OBLIGATION_BLOCKING_OPEN,
                    CLIOutputType.OBLIGATION_FALSE_CLOSURE,
                    "open_gap",
                }
                findings: List[str] = []
                seen: set[str] = set()

                def _append(value: Any) -> None:
                    normalized = _normalize_compose_finding(value)
                    if not normalized or normalized in seen:
                        return
                    seen.add(normalized)
                    findings.append(normalized)

                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    task_id = str(item.get("task_id") or "").strip()
                    if task_id not in review_step_ids:
                        continue
                    summary_text = _normalize_compose_finding(item.get("summary"))
                    if summary_text and task_id == "validate_tz_completeness":
                        _append(summary_text)
                    outputs = item.get("outputs") or []
                    if not isinstance(outputs, list):
                        continue
                    for output in outputs:
                        if not isinstance(output, dict):
                            continue
                        output_type = str(output.get("type") or "").strip()
                        if output_type not in review_output_types:
                            continue
                        _append(output.get("content_preview") or output.get("content"))
                return findings[:20]

            def _sanitize_step_results(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                """Prepare a user-facing-safe payload for the final answer model.

                Important: do not include internal tool call logs or correlation ids.
                """

                sanitized: List[Dict[str, Any]] = []
                for r in (items or [])[-25:]:
                    if not isinstance(r, dict):
                        continue
                    outputs = r.get("outputs") or []
                    if not isinstance(outputs, list):
                        outputs = []

                    safe_outputs: List[Dict[str, Any]] = []
                    for o in outputs:
                        if not isinstance(o, dict):
                            continue
                        t = str(o.get("type") or "")
                        if t == "text":
                            prev = str(o.get("content_preview") or "")
                            safe_outputs.append(
                                {
                                    "type": "text",
                                    "content_preview": prev,
                                    "content_len": int(o.get("content_len") or 0),
                                    "content_spilled": bool(o.get("content_spilled")),
                                }
                            )
                            for k in ("path", "file_path", "name"):
                                if o.get(k):
                                    safe_outputs[-1][k] = str(o.get(k))
                        else:
                            # Pass through only minimal, non-sensitive fields.
                            out = {"type": t or "file"}
                            for k in ("path", "file_path", "name"):
                                if o.get(k):
                                    out[k] = str(o.get(k))
                            safe_outputs.append(out)

                    sanitized.append(
                        {
                            "task_id": str(r.get("task_id") or ""),
                            "title": str(r.get("title") or ""),
                            "status": str(r.get("status") or ""),
                            "summary": str(r.get("summary") or ""),
                            "outputs": safe_outputs,
                            "artifacts": _collect_artifacts_from_outputs(outputs),
                            "step_artifact": str(r.get("orchestrator_artifact") or ""),
                        }
                    )
                return sanitized

            # Keep context bounded to avoid blowing up the model input.
            steps_payload: List[Dict[str, Any]] = []
            for s in plan_steps_local:
                steps_payload.append(
                    {
                        "id": s.id,
                        "title": s.title,
                        "step_type": s.step_type,
                        "depends_on": s.depends_on or [],
                    }
                )
            claim_ledger, ledger_path = _persist_claim_ledger_for_compose(
                step_results_local,
                supplemental_entries=supplemental_claim_entries,
            )
            fact_pack_text = _build_fact_pack_text(
                user_query=user_query,
                plan_steps_local=plan_steps_local,
                step_results_local=step_results_local,
                supplemental_entries=supplemental_claim_entries,
            )
            external_reference_entries = _collect_external_reference_entries(
                user_query=user_query,
                mapping_text=fact_pack_text,
                step_results_local=step_results_local,
            )
            critical_findings = _collect_compose_critical_findings(step_results_local)
            fact_pack_path = ""
            user_query_path = ""
            artifacts_index_path = ""
            try:
                fact_pack_path = _write_text_artifact(
                    f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_compose_fact_pack.md",
                    fact_pack_text,
                )
                _register_artifact("fact_pack", fact_pack_path, title="Fact pack")
                user_query_path = _write_text_artifact(
                    f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_original_user_text.md",
                    str(user_query or "").strip(),
                )
                _register_artifact("original_user_text", user_query_path, title="Original user text")
                artifacts_index_path = _write_artifacts_index()
            except Exception as e:
                self._log.warning("failed to persist compose artifact bundle: %s", e)
            payload = {
                "user_query": user_query,
                "user_query_path": user_query_path,
                "plan": steps_payload,
                "fact_pack_text": fact_pack_text,
                "fact_pack_path": fact_pack_path,
                "claim_ledger": claim_ledger,
                "external_references": external_reference_entries,
                "critical_findings": critical_findings,
                # IMPORTANT: sanitize to avoid leaking internal tool logs into the user-facing answer.
                "step_results": _sanitize_step_results(step_results_local),
                "claim_ledger_path": ledger_path,
                "artifacts_index_path": artifacts_index_path,
                "artifact_bundle": {
                    "primary_sources": [
                        path
                        for path in [user_query_path, fact_pack_path, ledger_path, artifacts_index_path]
                        if str(path or "").strip()
                    ],
                    "step_artifacts": [
                        str(item.get("orchestrator_artifact") or "").strip()
                        for item in step_results_local
                        if isinstance(item, dict) and str(item.get("orchestrator_artifact") or "").strip()
                    ],
                    "external_reference_sources": [
                        str(item.get("source") or "").strip()
                        for item in external_reference_entries
                        if str(item.get("source") or "").strip()
                    ],
                    "compose_mode": "artifacts_first",
                },
            }
            if raw_user_clarification_answers:
                payload["clarification_answers"] = list(raw_user_clarification_answers)
            try:
                raw = json.dumps(payload, ensure_ascii=False)
            except Exception as e:
                self._log.warning("failed to serialize final compose payload: %s", e)
                raw = ""
            active_template = template if isinstance(template, dict) else {}
            compose_mode = str(active_template.get("compose_mode") or "").strip().lower()
            required_inputs = [
                str(item).strip()
                for item in (
                    self._effective_analyst_repo_flags(session, active_template).get("required_inputs") or []
                )
                if str(item).strip()
            ]
            required_sections = _resolve_required_top_level_sections(
                user_query=user_query,
                text="\n".join(
                    fragment
                    for fragment in (
                        raw_user_query,
                        raw,
                        *[
                            str(item.get("summary") or "").strip()
                            for item in step_results_local
                            if isinstance(item, dict)
                        ],
                    )
                    if str(fragment or "").strip()
                ),
            )
            template_scope_constraints = str(active_template.get("system_prompt_addition") or "").strip()
            # Analyst templates are section-driven by design. If a template declares required sections,
            # default to composing the final document against that contract unless the template opts out.
            use_template_first_compose = bool(required_sections) and compose_mode not in {
                "legacy_final_answer",
                "generic_final_answer",
            }
            structured_compose = _supports_strict_chat_json_contract(self._deps.chat_completion)
            if use_template_first_compose:
                req = "\n".join(f"- {x}" for x in required_sections)
                handoff_required = any(
                    "implementation handoff" in _normalize_shell_heading(item)
                    for item in required_sections
                )
                scope_lines = [
                    "ФОКУС ПО SCOPE:",
                    "- Держи документ в границах исходного запроса пользователя.",
                    "- Не перечисляй несвязанные модули, поверхности и части проекта "
                    "только потому, что они встретились в артефактах или step_results.",
                    "- Включай только те компоненты, файлы и сценарии, которые прямо "
                    "влияют на требуемую доработку и подтверждены материалами.",
                ]
                if external_reference_entries:
                    scope_lines.extend(
                        [
                            "- Если пользователь дал внешний референс, сохрани его отдельным разделом "
                            "как implementation guidance и примеры реализации.",
                            "- Для каждого внешнего референса укажи source, extracted pattern, "
                            "local mapping и статус direct-adapt vs requires-validation.",
                            "- Не смешивай внешние референсы с repo facts текущего проекта.",
                        ]
                    )
                if template_scope_constraints:
                    scope_lines.extend(
                        [
                            "",
                            "ДОПОЛНИТЕЛЬНЫЕ ОГРАНИЧЕНИЯ АКТИВНОГО ШАБЛОНА:",
                            template_scope_constraints,
                        ]
                    )
                if handoff_required:
                    scope_lines.extend(
                        [
                            "",
                            "ОБЯЗАТЕЛЬНЫЙ HANDOFF:",
                            '- Раздел "Implementation handoff по компонентам и файлам" MUST быть исполнимым.',
                            "- Для каждой подтвержденной затронутой единицы укажи: "
                            "компонент/файл -> что меняется -> как проверить -> какие тесты/команды запускать.",
                            '- Не оставляй в handoff placeholders уровня TODO, TBD или "дописать позже".',
                            '- Не используй формулировки уровня "реализационная деталь", '
                            '"не является source of truth" или "решим в реализации" вместо конкретики.',
                            "- Если точное имя helper/class не критично, все равно зафиксируй "
                            "конкретный файл/модуль, seam интеграции и observable verification.",
                            "- Если repo evidence пока не хватает для полного контракта, переведи это "
                            "в explicit manual validation gate: какой artifact/fixture/команда нужны, "
                            "какой файл/компонент от этого зависит и какой acceptance signal закрывает гэп.",
                            "- Если поверхность не подтверждена репозиторием и не входит в исходный scope, "
                            "явно пометь её вне scope этой задачи и не удерживай как implementation blocker.",
                        ]
                    )
                critical_lines: List[str] = []
                if critical_findings:
                    critical_lines.extend(
                        [
                            "",
                            "CRITICAL FINDINGS ИЗ ФИНАЛЬНОЙ ПРОВЕРКИ:",
                            "- Каждый пункт из critical_findings ниже must быть либо исправлен в документе, "
                            "либо явно удержан как конкретный manual validation gate / open gap.",
                            '- Не оставляй формальные заглушки уровня "(нет)", если critical_findings непустой.',
                            "- Не маскируй reviewer corrections общими фразами; отражай их в тексте документа предметно.",
                        ]
                    )
                required_input_closure_lines: List[str] = []
                if required_inputs:
                    required_input_closure_lines.extend(
                        [
                            "",
                            "ОБЯЗАТЕЛЬНО ЗАКРОЙ REQUIRED_INPUTS:",
                            "- Документ MUST явно закрывать каждый required_input ниже, "
                            "а не только подразумевать его в тексте.",
                            "- Если evidence недостаточно, перенеси это в конкретный "
                            "validation gate или open gap, но не выдавай за confirmed fact.",
                            "Required inputs to close:",
                            *[f"- {item}" for item in required_inputs],
                        ]
                    )
                    impacted_scope_required = any(
                        "затронут" in str(item or "").casefold()
                        and (
                            "компон" in str(item or "").casefold()
                            or "модул" in str(item or "").casefold()
                            or "файл" in str(item or "").casefold()
                        )
                        for item in required_inputs
                    )
                    if impacted_scope_required:
                        required_input_closure_lines.extend(
                            [
                                "- В разделе про изменения по компонентам явно раздели "
                                "`точно затронутые` и `предположительно затронутые / требуют "
                                "отдельной проверки` зоны.",
                                "- Не смешивай confirmed impact и hypothesis в один список.",
                                "- Для каждой предположительно затронутой зоны укажи, "
                                "почему она попала в validation scope и какой signal "
                                "подтвердит или снимет её.",
                            ]
                        )
                    compatibility_required = any(
                        "совмест" in str(item or "").casefold()
                        or "обратн" in str(item or "").casefold()
                        for item in required_inputs
                    )
                    if compatibility_required:
                        required_input_closure_lines.extend(
                            [
                                "- Для обратной совместимости укажи, какие текущие "
                                "сценарии обязаны не регрессировать, где риск, и как "
                                "он проверяется.",
                            ]
                        )
                system = (
                    "Ты — оркестратор. Сформируй итоговый документ по материалам (JSON).\n"
                    "Пиши на русском.\n"
                    "Верни только готовый документ в Markdown.\n"
                    "\n"
                    "СТРОГИЙ КОНТРАКТ ДОКУМЕНТА:\n"
                    "Собери результат строго по списку разделов ниже и строго в этом порядке.\n"
                    "Не добавляй top-level разделы вне списка.\n"
                    "Если данных недостаточно, не пропускай раздел: явно помечай допущения или [Нужно уточнить].\n"
                    "Primary source of truth: artifact_bundle.primary_sources и step_artifacts. "
                    "step_results используй только как fallback summary, если в артефактах чего-то не хватает.\n"
                    "\n"
                    "Обязательные разделы:\n"
                    f"{req}\n"
                    "\n"
                    f"{chr(10).join(scope_lines + required_input_closure_lines + critical_lines)}\n"
                    "\n"
                    "ЗАПРЕТЫ:\n"
                    "- Не упоминай внутренние инструменты, tool_calls, corr_id, логи, технические поля.\n"
                    "- Не описывай процесс выполнения шагов, только итоговый документ.\n"
                    "- Не заменяй список разделов своими заголовками.\n"
                    "- Не добавляй собственный статус готовности/реализации: runtime добавит его отдельно.\n"
                )
            else:
                system = (
                    "Ты — оркестратор. Сформируй итоговый ответ пользователю по материалам (JSON).\n"
                    "Пиши на русском.\n"
                    "\n"
                    "ЖЁСТКИЙ КОНТРАКТ (обязательные разделы, в этом порядке):\n"
                    "1) Результат (2–5 строк): что получилось в итоге.\n"
                    "2) Детали: ключевые факты/выводы (список).\n"
                    "3) Как проверить (если применимо): конкретные шаги/команды/пункты.\n"
                    "4) Что не удалось (если есть шаги со status=error/blocked): кратко, по пунктам.\n"
                    "5) Нужно от вас (если не хватает данных): вопросы к пользователю, по пунктам.\n"
                    "6) Допущения (если делал предположения): перечисли явно.\n"
                    "Primary source of truth: artifact_bundle.primary_sources и step_artifacts. "
                    "step_results используй только как fallback summary, если в артефактах чего-то не хватает.\n"
                    "\n"
                    "ЗАПРЕТЫ:\n"
                    "- Не упоминай внутренние инструменты, tool_calls, corr_id, логи, технические поля.\n"
                    "- Не описывай процесс выполнения шагов, только итог и полезные инструкции.\n"
                    "- Не добавляй собственный статус готовности/реализации: runtime добавит его отдельно.\n"
                    "\n"
                    "Форматируй в Markdown с короткими заголовками. Не используй code fences вокруг всего ответа.\n"
                )
            if structured_compose:
                system += (
                    "\nФОРМАТ ОТВЕТА:\n"
                    'Верни только JSON-объект вида {"final_text":"<готовый markdown>"}.\n'
                    "Поле final_text должно содержать весь итоговый ответ целиком.\n"
                    "Не имитируй tool use и не используй псевдо-разметку вроде [TOOL_CALL].\n"
                )
            user_prompt = f"Материалы (JSON):\n{raw}"
            cli_compose_supported = bool(
                use_template_first_compose and callable(getattr(session, "run_prompt", None))
            )
            for attempt in (1, 2):
                prompt_system = system
                if attempt == 2:
                    if structured_compose:
                        prompt_system += (
                            "\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ. "
                            'Верни только JSON-объект вида {"final_text":"..."} без внутренних tool-маркеров.'
                        )
                    else:
                        prompt_system += (
                            "\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ. "
                            "Верни только готовый итоговый текст без внутренних tool-маркеров."
                        )
                final_text = ""
                if cli_compose_supported:
                    cli_prompt = wrap_prompt_for_response_format(
                        f"{prompt_system}\n\n{user_prompt}",
                        CLIResponseFormat.CLAIM_BUNDLE_JSON,
                    )
                    cli_prompt = _prepare_cli_prompt_via_artifact(
                        cli_prompt,
                        filename_hint=f"{session.id}_compose_final_answer_prompt_attempt_{attempt}.txt",
                        response_format=CLIResponseFormat.CLAIM_BUNDLE_JSON,
                    )
                    try:
                        retry_info = await run_cli_with_retry(lambda: session.run_prompt(cli_prompt), max_attempts=2)
                        out = strip_outer_code_fence(strip_ansi(str(retry_info.get("output") or ""))).strip()
                        self._log.info(
                            "compose_final_answer cli output received session=%s attempt=%d output_len=%d",
                            getattr(session, "id", ""),
                            attempt,
                            len(out),
                        )
                        if retry_info.get("retried"):
                            self._log.warning(
                                "compose_final_answer cli retried session=%s attempts=%s reason=%s",
                                getattr(session, "id", ""),
                                retry_info.get("attempts"),
                                retry_info.get("retry_reason") or "",
                            )
                        if retry_info.get("retry_exhausted"):
                            self._log.warning(
                                "compose_final_answer cli retry exhausted session=%s reason=%s",
                                getattr(session, "id", ""),
                                retry_info.get("retry_reason") or "",
                            )
                        payload = parse_bundle_for_response_format(out, CLIResponseFormat.CLAIM_BUNDLE_JSON)
                        if isinstance(payload, dict):
                            final_text = str(payload.get("final_text") or "").strip()
                            self._log.info(
                                "compose_final_answer cli parsed final_text session=%s attempt=%d text_len=%d",
                                getattr(session, "id", ""),
                                attempt,
                                len(final_text),
                            )
                        else:
                            self._log.warning(
                                "compose_final_answer cli parse failed attempt=%d/2 session=%s",
                                attempt,
                                getattr(session, "id", ""),
                            )
                    except Exception as e:
                        self._log.warning(
                            "compose_final_answer cli failed attempt=%d/2 session=%s: %s",
                            attempt,
                            getattr(session, "id", ""),
                            e,
                        )
                if not final_text:
                    if structured_compose:
                        out = await self._deps.chat_completion(
                            self._config,
                            prompt_system,
                            user_prompt,
                            response_format={"type": "json_object"},
                            max_tokens=final_answer_max_tokens,
                            normalize_error_handler=_recover_compose_final_answer_normalize_error,
                        )
                        try:
                            payload = loads_safe(out, strict_first=False)
                        except Exception as e:
                            self._log.warning("compose_final_answer parse failed attempt=%d/2: %s", attempt, e)
                            payload = None
                        if isinstance(payload, dict):
                            final_text = str(payload.get("final_text") or "").strip()
                    else:
                        out = await self._deps.chat_completion(self._config, prompt_system, user_prompt)
                        final_text = str(out or "").strip()
                if not final_text:
                    self._log.warning("compose_final_answer empty final_text attempt=%d/2", attempt)
                    continue
                if _contains_internal_runtime_markup(final_text):
                    self._log.warning("compose_final_answer returned internal runtime markup attempt=%d/2", attempt)
                    continue
                _persist_analyst_final_candidate(final_text, stage=f"compose_attempt_{attempt}_raw")
                normalized_final_text = _apply_protected_spec_shell(final_text, user_query=user_query)
                section_contract = _collect_required_top_level_section_contract_gaps(
                    normalized_final_text,
                    user_query=user_query,
                    required_sections=required_sections,
                )
                if section_contract["missing_sections"] or section_contract["section_contract_gaps"]:
                    repaired_final_text = _repair_required_top_level_sections_from_fallback(
                        current_text=normalized_final_text,
                        user_query=user_query,
                        required_sections=required_sections,
                        step_results_local=step_results_local,
                        reason=(
                            "compose_final_answer section repair: "
                            + ", ".join(section_contract["missing_sections"] + section_contract["section_contract_gaps"])
                        ),
                    )
                    if repaired_final_text:
                        repaired_contract = _collect_required_top_level_section_contract_gaps(
                            repaired_final_text,
                            user_query=user_query,
                            required_sections=required_sections,
                        )
                        if not repaired_contract["missing_sections"] and not repaired_contract["section_contract_gaps"]:
                            self._log.info(
                                "compose_final_answer repaired section contract attempt=%d/2",
                                attempt,
                            )
                            _persist_analyst_final_candidate(
                                repaired_final_text,
                                stage=f"compose_attempt_{attempt}_repaired",
                            )
                            return repaired_final_text
                    self._log.warning(
                        "compose_final_answer violated section contract attempt=%d/2 missing=%s order=%s",
                        attempt,
                        section_contract["missing_sections"],
                        section_contract["section_contract_gaps"],
                    )
                    continue
                _persist_analyst_final_candidate(
                    normalized_final_text,
                    stage=f"compose_attempt_{attempt}_normalized",
                )
                return normalized_final_text
            return ""

        def _build_raw_summary_fallback(step_results_local: List[Dict[str, Any]]) -> str:
            return (
                "\n\n".join(
                    str(item.get("summary") or "").strip()
                    for item in step_results_local
                    if isinstance(item, dict) and str(item.get("summary") or "").strip()
                ).strip()
                or "(empty response)"
            )

        def _build_template_aware_final_answer_fallback(
            *,
            user_query: str,
            step_results_local: List[Dict[str, Any]],
            reason: str,
        ) -> str:
            if not analyst_runtime_context or not protected_spec_shell:
                return ""

            def _normalize_inline_text(value: Any, *, limit: int = 280) -> str:
                text = " ".join(str(value or "").split()).strip()
                if not text:
                    return ""
                if len(text) <= limit:
                    return text
                return text[: limit - 1].rstrip() + "…"

            active_template = template if isinstance(template, dict) else {}
            core_sections = [
                str(item).strip()
                for item in (
                    active_template.get("required_sections")
                    or protected_spec_shell.get("core_sections")
                    or []
                )
                if str(item).strip()
            ]
            if not core_sections:
                core_sections = ["Контекст", "Требования"]

            reserved_headings = {
                _normalize_shell_heading(source_task_section),
                _normalize_shell_heading(open_questions_section),
                _normalize_shell_heading(external_references_section),
                _normalize_shell_heading(assumptions_section),
            }
            core_sections = [
                heading
                for heading in core_sections
                if _normalize_shell_heading(heading) not in reserved_headings
            ]
            if not core_sections:
                core_sections = ["Контекст", "Требования"]

            summary_points: List[str] = []
            evidence_points: List[str] = []
            artifact_points: List[str] = []
            seen_summaries: set[str] = set()
            seen_evidence: set[str] = set()
            seen_artifacts: set[str] = set()
            for item in step_results_local:
                if not isinstance(item, dict):
                    continue
                summary_text = _normalize_inline_text(item.get("summary"))
                if summary_text and summary_text not in seen_summaries:
                    seen_summaries.add(summary_text)
                    summary_points.append(summary_text)
                outputs = item.get("outputs") or []
                if not isinstance(outputs, list):
                    continue
                for output in outputs[:3]:
                    if not isinstance(output, dict):
                        continue
                    preview_text = _normalize_inline_text(
                        output.get("content_preview") or output.get("content"),
                    )
                    if (
                        preview_text
                        and preview_text not in seen_evidence
                        and not _contains_internal_runtime_markup(preview_text)
                        and not _looks_like_nonfinal_rework_text(preview_text)
                    ):
                        seen_evidence.add(preview_text)
                        evidence_points.append(preview_text)
                    artifact_path = str(output.get("path") or output.get("file_path") or "").strip()
                    if artifact_path and artifact_path not in seen_artifacts:
                        seen_artifacts.add(artifact_path)
                        artifact_points.append(artifact_path)

            supporting_points = summary_points or evidence_points
            repo_path_points = _collect_repo_path_candidates_from_step_results(step_results_local)
            command_candidates = _collect_command_candidates_from_step_results(step_results_local)

            def _extend_bullets(lines: List[str], items: List[str], *, limit: int = 6) -> None:
                for item in items[:limit]:
                    lines.append(f"- {item}")

            sections: List[tuple[str, str]] = []
            consumed_supporting_points = False
            for heading in core_sections:
                normalized_heading = _normalize_shell_heading(heading)
                lines: List[str] = []
                if any(token in normalized_heading for token in ("контекст", "цель", "фон", "исход")):
                    lines.extend(
                        [
                            "Исходный запрос пользователя:",
                            str(user_query or "").strip() or "[Нужно уточнить]",
                        ]
                    )
                    if supporting_points:
                        lines.extend(["", "Подтвержденные материалы текущего прогона:"])
                        _extend_bullets(lines, supporting_points, limit=4)
                        consumed_supporting_points = True
                elif any(
                    token in normalized_heading
                    for token in ("требован", "измен", "доработ", "решен", "scope", "объем", "сценар", "функц")
                ):
                    if supporting_points:
                        lines.append("Подтвержденные материалы текущего прогона:")
                        _extend_bullets(lines, supporting_points, limit=6)
                        consumed_supporting_points = True
                    else:
                        lines.append("- [Нужно уточнить]")
                    if artifact_points:
                        lines.extend(["", "Связанные артефакты текущего прогона:"])
                        _extend_bullets(lines, artifact_points, limit=5)
                elif "implementation handoff" in normalized_heading:
                    lines.append("Подтвержденные точки реализации:")
                    if repo_path_points:
                        for path in repo_path_points[:6]:
                            lines.append(f"- Компонент/файл: {path}")
                    else:
                        lines.append("- Компонент/файл: см. подтвержденные изменения по компонентам и evidence текущего прогона.")
                    lines.extend(["", "Что меняется:"])
                    if summary_points:
                        _extend_bullets(lines, summary_points, limit=4)
                    else:
                        lines.append("- Реализовать подтвержденные изменения из профильных разделов этого ТЗ без расширения scope.")
                    lines.extend(["", "Как проверить:"])
                    if command_candidates:
                        _extend_bullets(lines, command_candidates, limit=4)
                    else:
                        lines.append("- Сверить результат с подтвержденными сценариями и acceptance-критериями текущего прогона.")
                    lines.extend(["", "Тесты/команды:"])
                    if command_candidates:
                        _extend_bullets(lines, command_candidates, limit=4)
                    else:
                        lines.append("- Запустить `.venv/bin/pytest -q` на targeted-наборе тестов по подтвержденным затронутым файлам.")
                elif any(token in normalized_heading for token in ("провер", "валидац", "тест")):
                    if artifact_points:
                        lines.append("Проверка должна опираться на артефакты текущего прогона:")
                        _extend_bullets(lines, artifact_points, limit=5)
                    else:
                        lines.append("- Сверить итоговый документ с подтвержденными материалами текущего прогона.")
                else:
                    if supporting_points and not consumed_supporting_points:
                        lines.append("Подтвержденные материалы текущего прогона:")
                        _extend_bullets(lines, supporting_points, limit=6)
                        consumed_supporting_points = True
                    elif evidence_points:
                        lines.append("Подтвержденные материалы текущего прогона:")
                        _extend_bullets(lines, evidence_points, limit=6)
                    elif artifact_points:
                        lines.append("Связанные артефакты текущего прогона:")
                        _extend_bullets(lines, artifact_points, limit=5)
                    else:
                        lines.append("- [Нужно уточнить]")
                sections.append((heading, "\n".join(lines).strip()))

            open_questions_lines = [
                (
                    "- Требуется ручная валидация итогового документа: "
                    "автоматическая сборка финального markdown завершилась через deterministic fallback."
                ),
            ]
            reason_text = _normalize_inline_text(reason, limit=200)
            if reason_text:
                open_questions_lines.append(f"- Причина fallback: {reason_text}.")
            sections.append((open_questions_section, "\n".join(open_questions_lines)))
            title = str(protected_spec_shell.get("title") or "Техническое задание").strip() or "Техническое задание"
            rendered = _render_level_two_sections(title, sections)
            return _apply_protected_spec_shell(rendered, user_query=user_query)

        async def _send_final_answer(
            *,
            user_query: str,
            plan_steps_local: List[Any],
            step_results_local: List[Dict[str, Any]],
        ) -> str:
            chat_id = dest.get("chat_id")
            corr_id = f"{session.id}:compose_final_answer"
            self._log.info("step start corr_id=%s step_type=%s", corr_id, "compose_final_answer")
            final_text = ""
            try:
                if chat_id is None or bot is None:
                    if analyst_runtime_context and protected_spec_shell:
                        final_text = await _compose_final_answer_text(
                            user_query=user_query,
                            plan_steps_local=plan_steps_local,
                            step_results_local=step_results_local,
                        )
                        if not final_text:
                            final_text = _build_template_aware_final_answer_fallback(
                                user_query=user_query,
                                step_results_local=step_results_local,
                                reason="compose_final_answer returned empty final_text in no-transport path",
                            )
                    else:
                        return _build_raw_summary_fallback(step_results_local)
                else:
                    final_text = await _compose_final_answer_text(
                        user_query=user_query,
                        plan_steps_local=plan_steps_local,
                        step_results_local=step_results_local,
                    )
                    if not final_text:
                        if analyst_runtime_context and protected_spec_shell:
                            final_text = _build_template_aware_final_answer_fallback(
                                user_query=user_query,
                                step_results_local=step_results_local,
                                reason="compose_final_answer returned empty final_text before delivery",
                            )
                        else:
                            final_text = _build_raw_summary_fallback(step_results_local)
                _persist_analyst_final_candidate(final_text, stage="before_rework")
                final_text = await _maybe_rework_final_text(
                    final_text=final_text,
                    user_query=user_query,
                )
                lint_issues = lint_markdown_document(final_text).get("issues") or []
                lint_repairs: List[str] = []
                if lint_issues:
                    final_text, lint_repairs = repair_markdown_document(final_text)
                    try:
                        lint_report_path = _write_text_artifact(
                            f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_document_lint.md",
                            render_document_lint_report(issues=lint_issues, repairs=lint_repairs),
                        )
                        _register_artifact("document_lint", lint_report_path, title="Document lint")
                        _write_artifacts_index()
                    except Exception as e:
                        self._log.warning(
                            "failed to persist document lint report session=%s err=%s",
                            getattr(session, "id", ""),
                            e,
                        )
                _persist_analyst_final_candidate(final_text, stage="final_ready")

                # Collect artifacts before sending the main answer (but send them after the main HTML+summary).
                artifacts: List[Dict[str, Any]] = []
                for r in step_results_local:
                    artifacts.extend(_collect_artifacts_from_outputs(r.get("outputs") or []))
                # Make artifacts visible in the main answer too (deterministic), not only as uploads.
                # The final-answer LLM may ignore artifacts unless explicitly required.
                if artifacts and not analyst_telegram_delivery:
                    seen_paths: set[str] = set()
                    unique: List[Dict[str, Any]] = []
                    for a in artifacts:
                        p = str(a.get("path") or "").strip()
                        if not p or p in seen_paths:
                            continue
                        seen_paths.add(p)
                        unique.append(a)
                    max_items = 20
                    lines: List[str] = []
                    lines.append("")
                    lines.append("### Артефакты")
                    for a in unique[:max_items]:
                        p = str(a.get("path") or "").strip()
                        t = str(a.get("type") or "").strip()
                        name = str(a.get("name") or "").strip()
                        suffix = f" ({t})" if t else ""
                        if name:
                            lines.append(f"- {p}{suffix} — {name}")
                        else:
                            lines.append(f"- {p}{suffix}")
                    if len(unique) > max_items:
                        lines.append(f"- ...и ещё {len(unique) - max_items}")
                    # Do not duplicate the section if the model already listed artifacts.
                    if "артефакт" not in (final_text or "").lower():
                        final_text = (final_text.rstrip() + "\n" + "\n".join(lines).lstrip()).strip()

                if chat_id is None or bot is None:
                    self._log.info("step end corr_id=%s status=%s transport=%s", corr_id, "ok", "none")
                    return final_text

                # 1) Short ready message
                try:
                    await bot.send_message(context, chat_id=chat_id, text="Готово. Результат ниже.")
                except Exception as e:
                    self._log.exception("compose_final_answer: failed to send ready message: %s", e)

                def _resolve_notification_scope() -> Optional[ConversationScope]:
                    if str((dest or {}).get("kind") or "telegram").strip() != "telegram":
                        return None
                    target_chat_id = (dest or {}).get("chat_id")
                    if target_chat_id is None:
                        return None
                    message_thread_id = (dest or {}).get("message_thread_id")
                    if message_thread_id is None:
                        scope = getattr(session, "conversation_scope", None)
                        if (
                            isinstance(scope, ConversationScope)
                            and int(scope.chat_id) == int(target_chat_id)
                            and scope.message_thread_id is not None
                        ):
                            message_thread_id = int(scope.message_thread_id)
                    return ConversationScope.from_parts(target_chat_id, message_thread_id)

                async def _deliver_payload_bg() -> None:
                    self._log.info(
                        "compose_final_answer: delivery start corr_id=%s analyst=%s chat_id=%s output_len=%s",
                        corr_id,
                        analyst_telegram_delivery,
                        chat_id,
                        len(final_text or ""),
                    )
                    try:
                        if analyst_telegram_delivery:
                            # TODO(M3): route large output via a transport-agnostic MessagingService.send_large_output when available.
                            await bot.send_output(
                                session,
                                dest,
                                final_text,
                                context,
                                send_header=False,
                                force_html=False,
                            )
                        else:
                            # 2) One HTML+summary via send_output (no header)
                            # TODO(M3): route large output via a transport-agnostic MessagingService.send_large_output when available.
                            await bot.send_output(
                                session,
                                dest,
                                final_text,
                                context,
                                send_header=False,
                                force_html=True,
                            )
                        if analyst_telegram_delivery:
                            setattr(session, "analyst_runtime_final_output_delivered", True)
                            setattr(session, "analyst_runtime_final_output_delivered_text", str(final_text or ""))
                        self._log.info(
                            "compose_final_answer: delivery ok corr_id=%s analyst=%s chat_id=%s",
                            corr_id,
                            analyst_telegram_delivery,
                            chat_id,
                        )
                    except Exception as e:
                        self._log.exception("compose_final_answer: failed to send final output: %s", e)
                        if analyst_telegram_delivery:
                            raise

                    if analyst_telegram_delivery:
                        return

                    # 3) Additional materials as separate messages
                    for a in artifacts:
                        path = a.get("path") or ""
                        if not path or not os.path.exists(path):
                            continue
                        try:
                            with open(path, "rb") as f:
                                await bot._send_document(context, chat_id=chat_id, document=f)
                        except Exception as e:
                            self._log.exception("compose_final_answer: failed to send artifact %r: %s", path, e)

                async def _send_payload_bg() -> None:
                    queue_service = getattr(bot, "notification_queue_service", None)
                    scope = _resolve_notification_scope()
                    if (
                        scope is not None
                        and queue_service is not None
                        and not queue_service.is_executing_scope(scope)
                    ):
                        await queue_service.enqueue(
                            scope,
                            operation="background_report",
                            factory=_deliver_payload_bg,
                        )
                        return
                    await _deliver_payload_bg()

                def _schedule_session_background_send(coro) -> None:
                    mode_tasks = getattr(bot, "mode_tasks", None)
                    session_uid = session_runtime_uid(session)
                    if mode_tasks is not None and session_uid and hasattr(mode_tasks, "create"):
                        try:
                            mode_tasks.create(
                                session_uid=session_uid,
                                mode_id=_SESSION_TASK_MODE_ID,
                                coro=coro,
                                name="orchestrator_final_send",
                            )
                            return
                        except Exception:
                            self._log.exception(
                                "compose_final_answer: failed to track background send session_uid=%s",
                                session_uid,
                            )
                    task = asyncio.create_task(coro)

                    def _cb(t: asyncio.Task) -> None:
                        try:
                            t.result()
                        except asyncio.CancelledError:
                            return
                        except Exception as e:
                            self._log.exception("compose_final_answer: background send failed: %s", e)

                    task.add_done_callback(_cb)

                if analyst_telegram_delivery:
                    await _send_payload_bg()
                else:
                    _schedule_session_background_send(_send_payload_bg())

                self._log.info("step end corr_id=%s status=%s", corr_id, "ok")
                return final_text
            except (_AwaitingUserInput, _RestartPlanningAfterClarification):
                raise
            except Exception as e:
                self._log.exception("step end corr_id=%s status=error err=%s", corr_id, e)
                if final_text:
                    return final_text
                if analyst_runtime_context and protected_spec_shell:
                    return _build_template_aware_final_answer_fallback(
                        user_query=user_query,
                        step_results_local=step_results_local,
                        reason=f"compose_final_answer exception: {type(e).__name__}",
                    )
                return _build_raw_summary_fallback(step_results_local)

        async def _prepare_final_repo_review_step(step: Any, *, plan_steps_local: List[Any]) -> None:
            if str(getattr(step, "id", "") or "").strip() != "use_cli_repo_final_review":
                return
            base_instruction = str(getattr(step, "_original_instruction", "") or getattr(step, "instruction", "") or "").strip()
            if not base_instruction:
                return
            try:
                _ = plan_steps_local
                draft_text, draft_source = _select_final_repo_review_draft_seed(
                    step_results,
                    polished_path=polished_path,
                    draft_path=draft_path,
                )
                if not draft_text:
                    draft_text = _build_template_aware_final_answer_fallback(
                        user_query=raw_user_query,
                        step_results_local=step_results,
                        reason="final_repo_review_preflight_missing_draft",
                    )
                    if draft_text:
                        draft_source = "template_fallback"
                draft_text = str(draft_text or "").strip()
                if not draft_text:
                    raw_summary_fallback = _build_raw_summary_fallback(step_results)
                    if raw_summary_fallback and raw_summary_fallback != "(empty response)":
                        draft_text = raw_summary_fallback
                        draft_source = "step_summaries"
                if not draft_text:
                    return
                draft_text = _apply_protected_spec_shell(draft_text, user_query=raw_user_query)
                draft_dir = _resolve_intermediate_artifacts_dir(session, cwd)
                repo_review_draft_path = os.path.join(draft_dir, f"{session.id}_repo_final_review_draft.md")
                with open(repo_review_draft_path, "w", encoding="utf-8") as fh:
                    fh.write(draft_text.rstrip() + "\n")
                self._log.info(
                    "prepared final repo review draft session=%s source=%s",
                    getattr(session, "id", ""),
                    draft_source or "(unknown)",
                )
                root = self._repo_step_root(session)
                setattr(step, "_original_instruction", base_instruction)
                step.instruction = build_repo_final_review_instruction(
                    base_instruction=base_instruction,
                    draft_path=repo_review_draft_path,
                    repo_root=root,
                )
                setattr(step, "_use_cli_response_format", CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON)
            except Exception:
                self._log.exception(
                    "failed to prepare final repo review draft session=%s step=%s",
                    getattr(session, "id", ""),
                    getattr(step, "id", ""),
                )

        async def _run_cli_gap_closure_polish(
            *,
            draft_path: str,
            fact_pack_path: str,
            claim_ledger_path: str,
            open_gaps_path: str,
            artifacts_index_path: str,
            task_contract_path: str,
            obligation_matrix_path: str,
            refresh_preflight: Optional[Callable[[], None]] = None,
            retry_context_override: str = "",
        ) -> Dict[str, Any]:
            nonlocal structured_bundle_calls, structured_bundle_successes, cli_fallbacks, retry_successes, retry_exhausted
            nonlocal spec_fix_payload
            if not repo_grounded_required:
                return {}
            if not hasattr(session, "run_prompt"):
                retry_context = "final_cli_gap_closure execution_failed_cli_unavailable"
                failure_reason = "final_cli_gap_closure retry_exhausted execution_failed_cli_unavailable"
                _append_runtime_degraded_mode(failure_reason)
                _set_blocking_stage_state(
                    "final_cli_gap_closure",
                    attempts=1,
                    max_attempts=1,
                    failure_kind="execution_failed",
                    retry_context=retry_context,
                    final_status="retry_exhausted",
                )
                spec_fix_payload = {}
                return {
                    "final_text": "",
                    "claims": [],
                    "evidence": [],
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "degraded_modes": [failure_reason],
                }
            retry_context = str(retry_context_override or "").strip()
            for preflight_attempt in (1, 2):
                missing_stage_artifacts = _missing_required_artifacts(
                    draft_override=draft_path,
                    require_open_gaps=True,
                    require_obligation_matrix=True,
                )
                if not missing_stage_artifacts:
                    break
                reason = "final_cli_gap_closure bundle_incomplete missing: " + ", ".join(missing_stage_artifacts)
                retry_context = reason
                if preflight_attempt == 1 and refresh_preflight is not None:
                    _set_blocking_stage_state(
                        "final_cli_gap_closure",
                        attempts=preflight_attempt,
                        max_attempts=2,
                        failure_kind="bundle_incomplete",
                        retry_context=reason,
                        final_status="retry_requested",
                    )
                    try:
                        refresh_preflight()
                    except Exception as exc:
                        self._log.warning(
                            "final cli gap closure preflight refresh failed session=%s err=%s",
                            getattr(session, "id", ""),
                            exc,
                        )
                        retry_context = f"{reason}; refresh_failed: {exc}"
                    continue
                _append_runtime_degraded_mode(reason)
                _set_blocking_stage_state(
                    "final_cli_gap_closure",
                    attempts=preflight_attempt,
                    max_attempts=2,
                    failure_kind="bundle_incomplete",
                    retry_context=reason,
                    final_status="retry_exhausted",
                )
                spec_fix_payload = {}
                return {
                    "final_text": "",
                    "claims": [],
                    "evidence": [],
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "degraded_modes": [reason],
                }
            payload: Dict[str, Any] | None = None
            final_failure_kind = "invalid_bundle"
            for semantic_attempt in (1, 2):
                try:
                    prompt = build_gap_closure_prompt(
                        repo_root=self._repo_step_root(session),
                        draft_path=draft_path,
                        fact_pack_path=fact_pack_path,
                        claim_ledger_path=claim_ledger_path,
                        open_gaps_path=open_gaps_path,
                        artifacts_index_path=artifacts_index_path,
                        task_contract_path=task_contract_path,
                        obligation_matrix_path=obligation_matrix_path,
                        retry_context=retry_context,
                    )
                    prompt = _prepare_cli_prompt_via_artifact(
                        prompt,
                        filename_hint=f"{session.id}_final_gap_closure_prompt_attempt_{semantic_attempt}.txt",
                        response_format=CLIResponseFormat.SPEC_FIX_BUNDLE_JSON,
                    )
                    retry_info = await run_cli_with_retry(lambda: session.run_prompt(prompt), max_attempts=2)
                    structured_bundle_calls += 1
                    structured_bundle_stage_stats["gap_closure"]["calls"] += 1
                    out = str(retry_info.get("output") or "")
                    if retry_info.get("retried"):
                        retry_successes += 1
                        structured_bundle_stage_stats["gap_closure"]["retry_successes"] += 1
                        self._log.warning(
                            "final cli gap closure retried session=%s attempts=%s reason=%s",
                            getattr(session, "id", ""),
                            retry_info.get("attempts"),
                            retry_info.get("retry_reason") or "",
                        )
                    if retry_info.get("retry_exhausted"):
                        retry_exhausted += 1
                        structured_bundle_stage_stats["gap_closure"]["retry_exhausted"] += 1
                    sanitized = strip_ansi(out)
                    payload = parse_bundle_for_response_format(sanitized, CLIResponseFormat.SPEC_FIX_BUNDLE_JSON)
                    if payload is not None:
                        structured_bundle_successes += 1
                        structured_bundle_stage_stats["gap_closure"]["successes"] += 1
                        spec_fix_payload = dict(payload)
                        for item in payload.get("degraded_modes") or []:
                            _append_runtime_degraded_mode(str(item or "").strip())
                        _set_blocking_stage_state(
                            "final_cli_gap_closure",
                            attempts=semantic_attempt,
                            max_attempts=2,
                            final_status="ok",
                        )
                        return payload
                    final_failure_kind = "invalid_bundle"
                    retry_context = (
                        "Spec-fixer не вернул валидный structured bundle. "
                        "Обязательно верни final_text, closed_obligations, remaining_obligations, "
                        "corrections_applied, claims, evidence, degraded_modes."
                    )
                except Exception as e:
                    self._log.warning(
                        "final cli gap closure failed session=%s attempt=%d/2 err=%s",
                        getattr(session, "id", ""),
                        semantic_attempt,
                        e,
                    )
                    final_failure_kind = "execution_failed"
                    retry_context = (
                        "Предыдущая попытка завершилась execution_failed. "
                        "Повтори шаг и верни валидный structured bundle без prose."
                    )
                if semantic_attempt == 1:
                    _set_blocking_stage_state(
                        "final_cli_gap_closure",
                        attempts=semantic_attempt,
                        max_attempts=2,
                        failure_kind=final_failure_kind,
                        retry_context=retry_context,
                        final_status="retry_requested",
                    )
            cli_fallbacks += 1
            structured_bundle_stage_stats["gap_closure"]["fallbacks"] += 1
            failure_reason = f"final_cli_gap_closure retry_exhausted {final_failure_kind}"
            _append_runtime_degraded_mode(failure_reason)
            _set_blocking_stage_state(
                "final_cli_gap_closure",
                attempts=2,
                max_attempts=2,
                failure_kind=final_failure_kind,
                retry_context=retry_context,
                final_status="retry_exhausted",
            )
            spec_fix_payload = {}
            return {
                "final_text": "",
                "claims": [],
                "evidence": [],
                "closed_obligations": [],
                "remaining_obligations": [],
                "corrections_applied": [],
                "degraded_modes": [failure_reason],
            }

        async def _maybe_rework_final_text(*, final_text: str, user_query: str) -> str:
            nonlocal followup_repo_review_outputs
            nonlocal followup_obligation_review_payload
            nonlocal spec_fix_payload
            nonlocal supplemental_claim_entries
            nonlocal last_final_assessment
            nonlocal last_model_text_before_runtime
            nonlocal task_contract_payload
            nonlocal claim_ledger
            nonlocal fact_pack_path, claim_ledger_path, draft_path, polished_path
            nonlocal open_gaps_path, artifacts_index_path, task_contract_path, obligation_matrix_path
            nonlocal blocking_stage_states
            nonlocal stage_missing_required_artifacts
            final_text = _apply_protected_spec_shell(final_text, user_query=user_query)
            quality_passes = min(final_rework_passes, max_final_rework_passes)
            if repo_grounded_required and quality_passes <= 0:
                quality_passes = 1
            remaining_quality_passes = quality_passes
            if quality_passes <= 0:
                return final_text
            if not final_text.strip():
                return final_text

            repo_gap_labels = REPO_GAP_LABELS

            active = template if isinstance(template, dict) else {}
            effective_analyst_flags = self._effective_analyst_repo_flags(session, template)
            required = _resolve_required_top_level_sections(
                user_query=user_query,
                text=final_text,
            )
            required_inputs = [
                str(item).strip()
                for item in (effective_analyst_flags.get("required_inputs") or [])
                if str(item).strip()
            ]
            qa_prompt = ""
            if isinstance(active, dict):
                qa_prompt = str(active.get("qa_prompt") or "").strip()
            current = final_text
            task_contract_payload = {}
            spec_fix_payload = {}
            followup_obligation_review_payload = {}
            claim_ledger = []
            fact_pack_path = ""
            claim_ledger_path = ""
            draft_path = ""
            polished_path = ""
            open_gaps_path = ""
            artifacts_index_path = ""
            task_contract_path = ""
            obligation_matrix_path = ""
            blocking_stage_states = {}
            stage_missing_required_artifacts = set()

            def _required_repo_step_statuses() -> Dict[str, str]:
                out: Dict[str, str] = {}
                for step_id in self._required_repo_use_cli_step_ids(session, template):
                    result = prior_step_results.get(step_id)
                    out[step_id] = str((result or {}).get("status") or "").strip().lower()
                return out

            def _persist_task_contract() -> tuple[Dict[str, Any], str]:
                nonlocal task_contract_payload
                payload = build_task_contract(
                    user_query=user_query,
                    required_sections=required,
                    repo_grounded_required=repo_grounded_required,
                    qa_prompt=qa_prompt,
                    template_name=str(active.get("name") or "").strip(),
                    template_description=str(active.get("description") or "").strip(),
                    is_large_spec=is_large_spec,
                    required_step_ids=sorted(_required_repo_step_statuses()),
                    required_inputs=required_inputs,
                    traceability_rules=[
                        str(item).strip()
                        for item in (active.get("traceability_rules") or [])
                        if str(item).strip()
                    ],
                    protected_spec_shell=_build_task_contract_protected_spec_shell(current),
                )
                try:
                    path = _write_text_artifact(
                        f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_task_contract.json",
                        json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                    _register_artifact("task_contract", path, title="Task contract")
                except Exception as exc:
                    self._log.warning("failed to persist task contract session=%s err=%s", getattr(session, "id", ""), exc)
                    path = ""
                task_contract_payload = payload
                return payload, path

            def _persist_obligation_matrix(assessment_payload: Dict[str, Any]) -> tuple[List[Dict[str, Any]], str]:
                obligations = build_obligation_matrix(
                    task_contract=task_contract_payload,
                    assessment=assessment_payload,
                    required_step_statuses=_required_repo_step_statuses(),
                )
                try:
                    path = _write_text_artifact(
                        f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_obligation_matrix.json",
                        json.dumps(obligations, ensure_ascii=False, indent=2),
                    )
                    _register_artifact("obligation_matrix", path, title="Obligation matrix")
                except Exception as exc:
                    self._log.warning("failed to persist obligation matrix session=%s err=%s", getattr(session, "id", ""), exc)
                    path = ""
                return obligations, path

            claim_ledger = []
            claim_ledger_path = ""
            missing_required_artifacts = []
            if repo_grounded_required:
                for artifact_attempt in (1, 2):
                    task_contract_path = ""
                    claim_ledger_path = ""
                    fact_pack_path = ""
                    draft_path = ""
                    artifacts_index_path = ""
                    try:
                        task_contract_payload, task_contract_path = _persist_task_contract()
                        claim_ledger, claim_ledger_path = _persist_claim_ledger_for_compose(
                            step_results,
                            supplemental_entries=supplemental_claim_entries,
                        )
                        fact_pack_text = _build_fact_pack_text(
                            user_query=user_query,
                            plan_steps_local=steps,
                            step_results_local=step_results,
                            supplemental_entries=supplemental_claim_entries,
                        )
                        fact_pack_path = _write_text_artifact(
                            f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_fact_pack.md",
                            fact_pack_text,
                        )
                        _register_artifact("fact_pack", fact_pack_path, title="Fact pack")
                        draft_path = _write_text_artifact(
                            f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_draft.md",
                            _apply_protected_spec_shell(current, user_query=user_query),
                        )
                        _register_artifact("draft", draft_path, title="Initial draft")
                        artifacts_index_path = _write_artifacts_index()
                    except Exception as e:
                        self._log.warning(
                            "failed to prepare final artifact bundle session=%s attempt=%d/2 err=%s",
                            getattr(session, "id", ""),
                            artifact_attempt,
                            e,
                        )
                        missing_required_artifacts = _missing_required_artifacts()
                        if artifact_attempt == 1:
                            _set_blocking_stage_state(
                                "artifact_bundle",
                                attempts=artifact_attempt,
                                max_attempts=2,
                                failure_kind="execution_failed",
                                retry_context="artifact bundle preparation failed; retrying once",
                                final_status="retry_requested",
                            )
                            continue
                        break
                    missing_required_artifacts = _missing_required_artifacts()
                    if not missing_required_artifacts:
                        _set_blocking_stage_state(
                            "artifact_bundle",
                            attempts=artifact_attempt,
                            max_attempts=2,
                            final_status="ok",
                        )
                        break
                    self._log.warning(
                        "final artifact bundle incomplete session=%s attempt=%d/2 missing=%s",
                        getattr(session, "id", ""),
                        artifact_attempt,
                        ",".join(missing_required_artifacts),
                    )
                    if artifact_attempt == 1:
                        _set_blocking_stage_state(
                            "artifact_bundle",
                            attempts=artifact_attempt,
                            max_attempts=2,
                            failure_kind="bundle_incomplete",
                            retry_context="missing artifacts: " + ", ".join(missing_required_artifacts),
                            final_status="retry_requested",
                        )
                        continue
                    break
                if missing_required_artifacts:
                    _set_blocking_stage_state(
                        "artifact_bundle",
                        attempts=2,
                        max_attempts=2,
                        failure_kind="bundle_incomplete",
                        retry_context="missing artifacts: " + ", ".join(missing_required_artifacts),
                        final_status="retry_exhausted",
                    )
            else:
                try:
                    claim_ledger, claim_ledger_path = _persist_claim_ledger_for_compose(
                        step_results,
                        supplemental_entries=supplemental_claim_entries,
                    )
                    fact_pack_text = _build_fact_pack_text(
                        user_query=user_query,
                        plan_steps_local=steps,
                        step_results_local=step_results,
                        supplemental_entries=supplemental_claim_entries,
                    )
                    fact_pack_path = _write_text_artifact(
                        f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_fact_pack.md",
                        fact_pack_text,
                    )
                    _register_artifact("fact_pack", fact_pack_path, title="Fact pack")
                    draft_path = _write_text_artifact(
                        f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_draft.md",
                        _apply_protected_spec_shell(current, user_query=user_query),
                    )
                    _register_artifact("draft", draft_path, title="Initial draft")
                    artifacts_index_path = _write_artifacts_index()
                except Exception as e:
                    self._log.warning("failed to prepare final artifact bundle session=%s err=%s", getattr(session, "id", ""), e)
            mandatory_bundle_ready = not repo_grounded_required or not missing_required_artifacts
            if repo_grounded_required and "task_contract" in missing_required_artifacts:
                runtime_degraded_modes.append("final_task_contract_missing")
            if repo_grounded_required and "claim_ledger" in missing_required_artifacts:
                runtime_degraded_modes.append("final_claim_ledger_missing")
            if repo_grounded_required and not mandatory_bundle_ready:
                runtime_degraded_modes.append("final_artifact_bundle_incomplete")

            def _prepare_cli_stage_bundle(
                assessment_payload: Dict[str, Any],
                *,
                stage_id: str,
                include_open_gaps: bool = True,
            ) -> None:
                nonlocal open_gaps_path, obligation_matrix_path, artifacts_index_path
                if not repo_grounded_required:
                    return
                if not include_open_gaps:
                    stage_missing_required_artifacts.discard("open_gaps")
                last_reason = ""
                bundle_assessment = _build_stage_bundle_assessment_payload(assessment_payload)
                for attempt in (1, 2):
                    obligation_matrix_candidate = ""
                    open_gaps_candidate = ""
                    try:
                        _, obligation_matrix_candidate = _persist_obligation_matrix(bundle_assessment)
                        if include_open_gaps:
                            open_gaps_text = build_open_gaps_text(bundle_assessment, repo_gap_labels)
                            open_gaps_candidate = _write_text_artifact(
                                f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_open_gaps.md",
                                open_gaps_text,
                            )
                            _register_artifact("open_gaps", open_gaps_candidate, title="Open gaps")
                        artifacts_candidate = _write_artifacts_index()
                    except Exception as exc:
                        last_reason = f"{stage_id} execution_failed {exc}"
                        if attempt == 1:
                            _set_blocking_stage_state(
                                stage_id,
                                attempts=attempt,
                                max_attempts=2,
                                failure_kind="execution_failed",
                                retry_context=last_reason,
                                final_status="retry_requested",
                            )
                            continue
                        runtime_degraded_modes.append(f"{stage_id} retry_exhausted execution_failed")
                        _set_blocking_stage_state(
                            stage_id,
                            attempts=attempt,
                            max_attempts=2,
                            failure_kind="execution_failed",
                            retry_context=last_reason,
                            final_status="retry_exhausted",
                        )
                        return
                    missing_stage_artifacts = [
                        name
                        for name, path in (
                            ("obligation_matrix", obligation_matrix_candidate),
                            ("artifacts_index", artifacts_candidate),
                            *((("open_gaps", open_gaps_candidate),) if include_open_gaps else ()),
                        )
                        if not str(path or "").strip()
                    ]
                    if not missing_stage_artifacts:
                        obligation_matrix_path = obligation_matrix_candidate
                        if include_open_gaps:
                            open_gaps_path = open_gaps_candidate
                        artifacts_index_path = artifacts_candidate
                        cleared_stage_artifacts = {"obligation_matrix", "artifacts_index"}
                        if include_open_gaps:
                            cleared_stage_artifacts.add("open_gaps")
                        stage_missing_required_artifacts.difference_update(cleared_stage_artifacts)
                        _set_blocking_stage_state(
                            stage_id,
                            attempts=attempt,
                            max_attempts=2,
                            final_status="ok",
                        )
                        return
                    last_reason = f"{stage_id} bundle_incomplete missing: " + ", ".join(missing_stage_artifacts)
                    stage_missing_required_artifacts.update(missing_stage_artifacts)
                    if attempt == 1:
                        _set_blocking_stage_state(
                            stage_id,
                            attempts=attempt,
                            max_attempts=2,
                            failure_kind="bundle_incomplete",
                            retry_context=last_reason,
                            final_status="retry_requested",
                        )
                        continue
                runtime_degraded_modes.append(f"{stage_id} retry_exhausted bundle_incomplete")
                _set_blocking_stage_state(
                    stage_id,
                    attempts=2,
                    max_attempts=2,
                    failure_kind="bundle_incomplete",
                    retry_context=last_reason,
                    final_status="retry_exhausted",
                )

            def _refresh_stage_preflight_artifacts(
                *,
                draft_text: str,
                assessment_payload: Dict[str, Any],
                stage_id: str,
                include_open_gaps: bool = True,
            ) -> None:
                nonlocal task_contract_payload, task_contract_path
                nonlocal claim_ledger, claim_ledger_path, fact_pack_path, draft_path, artifacts_index_path
                if not task_contract_path:
                    task_contract_payload, task_contract_path = _persist_task_contract()
                if not claim_ledger_path:
                    claim_ledger, claim_ledger_path = _persist_claim_ledger_for_compose(
                        step_results,
                        supplemental_entries=supplemental_claim_entries,
                    )
                if not fact_pack_path:
                    fact_pack_text = _build_fact_pack_text(
                        user_query=user_query,
                        plan_steps_local=steps,
                        step_results_local=step_results,
                        supplemental_entries=supplemental_claim_entries,
                    )
                    fact_pack_path = _write_text_artifact(
                        f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_fact_pack.md",
                        fact_pack_text,
                    )
                    _register_artifact("fact_pack", fact_pack_path, title="Fact pack")
                if not draft_path:
                    draft_path = _write_text_artifact(
                        f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_draft.md",
                        _apply_protected_spec_shell(draft_text, user_query=user_query),
                    )
                    _register_artifact("draft", draft_path, title="Current draft")
                artifacts_index_path = _write_artifacts_index()
                _prepare_cli_stage_bundle(
                    assessment_payload,
                    stage_id=stage_id,
                    include_open_gaps=include_open_gaps,
                )

            def _build_repo_evidence_text() -> str:
                required_ids = set(self._required_repo_use_cli_step_ids(session, template))
                relevant: List[str] = []
                for item in step_results:
                    if not isinstance(item, dict):
                        continue
                    step_id = str(item.get("task_id") or "").strip()
                    step_type = str(item.get("step_type") or "").strip()
                    if step_id not in required_ids and step_type != "use_cli":
                        continue
                    summary = str(item.get("summary") or "").strip()
                    outputs = item.get("outputs") or []
                    previews: List[str] = []
                    if isinstance(outputs, list):
                        for output in outputs[:3]:
                            if not isinstance(output, dict):
                                continue
                            preview = str(output.get("content_preview") or output.get("content") or "").strip()
                            if preview:
                                previews.append(preview)
                    block_lines = [f"- step_id={step_id or '(unknown)'}"]
                    if summary:
                        block_lines.append(f"  summary: {summary}")
                    for preview in previews:
                        block_lines.append(f"  evidence: {preview}")
                    relevant.append("\n".join(block_lines))
                if not relevant:
                    return "- (repo evidence from use_cli steps отсутствует)"
                return "\n".join(relevant)

            async def _run_followup_repo_final_review(
                *,
                draft_text: str,
                stage_assessment: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                nonlocal structured_bundle_calls, structured_bundle_successes, cli_fallbacks, retry_successes, retry_exhausted
                nonlocal followup_obligation_review_payload, artifacts_index_path
                if not repo_grounded_required:
                    return {"outputs": [], "entry": None, "payload": {}}
                if not hasattr(session, "run_prompt"):
                    retry_context = "followup_obligation_review execution_failed_cli_unavailable"
                    failure_reason = "followup_obligation_review retry_exhausted execution_failed_cli_unavailable"
                    _append_runtime_degraded_mode(failure_reason)
                    _set_blocking_stage_state(
                        "followup_obligation_review",
                        attempts=1,
                        max_attempts=1,
                        failure_kind="execution_failed",
                        retry_context=retry_context,
                        final_status="retry_exhausted",
                    )
                    followup_obligation_review_payload = {}
                    return {
                        "outputs": [degraded_mode_output(failure_reason)],
                        "entry": None,
                        "payload": {},
                    }
                followup_draft_path = ""
                followup_draft_binding: Dict[str, str] = {"path": "", "sha1": ""}
                for preflight_attempt in (1, 2):
                    try:
                        followup_draft_path = _write_text_artifact(
                            (
                                f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}"
                                "_repo_final_review_followup_draft.md"
                            ),
                            _apply_protected_spec_shell(draft_text, user_query=user_query),
                        )
                        followup_draft_binding = _build_artifact_binding(followup_draft_path)
                        _register_artifact(
                            "draft_followup_review",
                            followup_draft_path,
                            title="Follow-up review draft",
                            meta={"sha1": str(followup_draft_binding.get("sha1") or "").strip()},
                        )
                        artifacts_index_path = _write_artifacts_index()
                    except Exception as e:
                        self._log.warning(
                            "failed to persist follow-up review draft session=%s attempt=%d/2 err=%s",
                            getattr(session, "id", ""),
                            preflight_attempt,
                            e,
                        )
                        reason = "followup_obligation_review execution_failed_draft_persist"
                        if preflight_attempt == 1:
                            _set_blocking_stage_state(
                                "followup_obligation_review",
                                attempts=preflight_attempt,
                                max_attempts=2,
                                failure_kind="execution_failed",
                                retry_context=reason,
                                final_status="retry_requested",
                            )
                            continue
                        _append_runtime_degraded_mode(reason)
                        _set_blocking_stage_state(
                            "followup_obligation_review",
                            attempts=preflight_attempt,
                            max_attempts=2,
                            failure_kind="execution_failed",
                            retry_context=reason,
                            final_status="retry_exhausted",
                        )
                        followup_obligation_review_payload = {}
                        return {
                            "outputs": [degraded_mode_output(reason)],
                            "entry": None,
                            "payload": {},
                        }
                    missing_stage_artifacts = _missing_required_artifacts(
                        draft_override=followup_draft_path,
                        require_obligation_matrix=True,
                    )
                    if not missing_stage_artifacts:
                        break
                    reason = "followup_obligation_review bundle_incomplete missing: " + ", ".join(missing_stage_artifacts)
                    if preflight_attempt == 1:
                        _set_blocking_stage_state(
                            "followup_obligation_review",
                            attempts=preflight_attempt,
                            max_attempts=2,
                            failure_kind="bundle_incomplete",
                            retry_context=reason,
                            final_status="retry_requested",
                        )
                        try:
                            _refresh_stage_preflight_artifacts(
                                draft_text=draft_text,
                                assessment_payload=stage_assessment or {},
                                stage_id="followup_obligation_review_bundle",
                                include_open_gaps=False,
                            )
                        except Exception as exc:
                            self._log.warning(
                                "follow-up obligation review preflight refresh failed session=%s err=%s",
                                getattr(session, "id", ""),
                                exc,
                            )
                        continue
                    _append_runtime_degraded_mode(reason)
                    _set_blocking_stage_state(
                        "followup_obligation_review",
                        attempts=preflight_attempt,
                        max_attempts=2,
                        failure_kind="bundle_incomplete",
                        retry_context=reason,
                        final_status="retry_exhausted",
                    )
                    followup_obligation_review_payload = {}
                    return {
                        "outputs": [degraded_mode_output(reason)],
                        "entry": None,
                        "payload": {},
                    }
                retry_context = ""
                payload: Dict[str, Any] | None = None
                final_failure_kind = "invalid_bundle"
                for semantic_attempt in (1, 2):
                    try:
                        followup_prompt = build_followup_repo_final_review_prompt(
                            repo_root=self._repo_step_root(session),
                            draft_path=followup_draft_path,
                            draft_sha1=str(followup_draft_binding.get("sha1") or "").strip(),
                            fact_pack_path=fact_pack_path,
                            claim_ledger_path=claim_ledger_path,
                            artifacts_index_path=artifacts_index_path,
                            task_contract_path=task_contract_path,
                            obligation_matrix_path=obligation_matrix_path,
                            retry_context=retry_context,
                        )
                        followup_prompt = _prepare_cli_prompt_via_artifact(
                            followup_prompt,
                            filename_hint=f"{session.id}_followup_obligation_review_prompt_attempt_{semantic_attempt}.txt",
                            response_format=CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON,
                        )
                        retry_info = await run_cli_with_retry(lambda: session.run_prompt(followup_prompt), max_attempts=2)
                        structured_bundle_calls += 1
                        structured_bundle_stage_stats["followup_review"]["calls"] += 1
                        raw = str(retry_info.get("output") or "")
                        if retry_info.get("retried"):
                            retry_successes += 1
                            structured_bundle_stage_stats["followup_review"]["retry_successes"] += 1
                            self._log.warning(
                                "follow-up obligation review retried session=%s attempts=%s reason=%s",
                                getattr(session, "id", ""),
                                retry_info.get("attempts"),
                                retry_info.get("retry_reason") or "",
                            )
                        if retry_info.get("retry_exhausted"):
                            retry_exhausted += 1
                            structured_bundle_stage_stats["followup_review"]["retry_exhausted"] += 1
                        sanitized = strip_ansi(raw)
                        payload = parse_bundle_for_response_format(
                            sanitized,
                            CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON,
                        )
                        if payload is not None:
                            structured_bundle_successes += 1
                            structured_bundle_stage_stats["followup_review"]["successes"] += 1
                            payload = dict(payload)
                            expected_sha1 = str(followup_draft_binding.get("sha1") or "").strip()
                            expected_path = str(followup_draft_binding.get("path") or followup_draft_path).strip()
                            actual_path = str(followup_draft_path or "").strip()
                            actual_sha1 = _file_sha1(followup_draft_path)
                            validated_artifact = {
                                "path": expected_path,
                                "actual_path": actual_path,
                                "sha1": expected_sha1,
                                "actual_sha1": actual_sha1,
                                "stale": bool(
                                    (expected_path and actual_path and actual_path != expected_path)
                                    or (expected_sha1 and actual_sha1 and actual_sha1 != expected_sha1)
                                ),
                            }
                            payload["validated_artifact"] = validated_artifact
                            if validated_artifact["stale"]:
                                stale_reason = (
                                    "Verifier validated stale persisted draft artifact: "
                                    f"path={validated_artifact['path']}, "
                                    f"expected_sha1={expected_sha1}, actual_sha1={actual_sha1}."
                                )
                                open_blocking = list(payload.get("open_blocking_obligations") or [])
                                false_closures = list(payload.get("false_closures") or [])
                                required_corrections = [
                                    str(item).strip()
                                    for item in (payload.get("required_corrections") or [])
                                    if str(item).strip()
                                ]
                                degraded_modes = [
                                    str(item).strip()
                                    for item in (payload.get("degraded_modes") or [])
                                    if str(item).strip()
                                ]
                                stale_obligation = {
                                    "obligation_id": "followup_review:artifact_binding",
                                    "statement": stale_reason,
                                    "status": "open",
                                    "blocking": True,
                                    "evidence_refs": [validated_artifact["path"]],
                                }
                                open_blocking.append(dict(stale_obligation))
                                false_closures.append(dict(stale_obligation))
                                if stale_reason not in required_corrections:
                                    required_corrections.append(stale_reason)
                                degraded_marker = "followup_obligation_review stale_artifact_validation"
                                if degraded_marker not in degraded_modes:
                                    degraded_modes.append(degraded_marker)
                                verdict = str(payload.get("verdict") or "").strip()
                                payload["verdict"] = (
                                    f"{stale_reason} {verdict}".strip()
                                    if verdict
                                    else stale_reason
                                )
                                payload["open_blocking_obligations"] = open_blocking
                                payload["false_closures"] = false_closures
                                payload["required_corrections"] = required_corrections
                                payload["degraded_modes"] = degraded_modes
                                _append_runtime_degraded_mode(degraded_marker)
                                _set_blocking_stage_state(
                                    "followup_obligation_review",
                                    attempts=semantic_attempt,
                                    max_attempts=2,
                                    failure_kind="stale_validation",
                                    retry_context=stale_reason,
                                    final_status="retry_exhausted",
                                )
                            else:
                                _set_blocking_stage_state(
                                    "followup_obligation_review",
                                    attempts=semantic_attempt,
                                    max_attempts=2,
                                    final_status="ok",
                                )
                            followup_obligation_review_payload = dict(payload)
                            break
                        final_failure_kind = "invalid_bundle"
                        retry_context = (
                            "Verifier не вернул валидный obligation review bundle. "
                            "Верни поля verdict, closed_blocking_obligations, open_blocking_obligations, "
                            "false_closures, unsupported_assertions, required_corrections, claims, evidence, degraded_modes."
                        )
                    except Exception as e:
                        self._log.warning(
                            "follow-up obligation review failed session=%s attempt=%d/2 err=%s",
                            getattr(session, "id", ""),
                            semantic_attempt,
                            e,
                        )
                        final_failure_kind = "execution_failed"
                        retry_context = (
                            "Предыдущая попытка verifier завершилась execution_failed. "
                            "Повтори шаг и верни только валидный structured bundle."
                        )
                    if semantic_attempt == 1:
                        _set_blocking_stage_state(
                            "followup_obligation_review",
                            attempts=semantic_attempt,
                            max_attempts=2,
                            failure_kind=final_failure_kind,
                            retry_context=retry_context,
                            final_status="retry_requested",
                        )
                if payload is None:
                    cli_fallbacks += 1
                    structured_bundle_stage_stats["followup_review"]["fallbacks"] += 1
                    reason = f"followup_obligation_review retry_exhausted {final_failure_kind}"
                    _append_runtime_degraded_mode(reason)
                    _set_blocking_stage_state(
                        "followup_obligation_review",
                        attempts=2,
                        max_attempts=2,
                        failure_kind=final_failure_kind,
                        retry_context=retry_context,
                        final_status="retry_exhausted",
                    )
                    followup_obligation_review_payload = {}
                    return {
                        "outputs": [degraded_mode_output(reason)],
                        "entry": None,
                        "payload": {},
                    }
                try:
                    payload_path = _write_text_artifact(
                        f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_obligation_review_followup.json",
                        json.dumps(payload, ensure_ascii=False, indent=2),
                    )
                    _register_artifact("obligation_review_followup", payload_path, title="Follow-up obligation review")
                    _write_artifacts_index()
                    outputs = obligation_review_bundle_to_outputs(payload)
                    return {
                        "outputs": outputs,
                        "entry": {
                            "task_id": "followup_obligation_review",
                            "title": "Follow-up obligation review",
                            "status": "ok",
                            "step_type": "use_cli",
                            "summary": str(payload.get("verdict") or "").strip() or "follow-up obligation review",
                            "outputs": outputs,
                            "claims": list(payload.get("claims") or []),
                            "artifacts": [{"type": "json", "path": payload_path}],
                            "orchestrator_artifact": payload_path,
                        },
                        "payload": payload,
                    }
                except Exception as e:
                    self._log.warning("follow-up obligation review persist failed session=%s err=%s", getattr(session, "id", ""), e)
                    retry_context = "follow-up review persist failed after successful payload parse"
                    for persist_attempt in (1, 2):
                        if persist_attempt == 1:
                            _set_blocking_stage_state(
                                "followup_obligation_review",
                                attempts=persist_attempt,
                                max_attempts=2,
                                failure_kind="execution_failed",
                                retry_context=retry_context,
                                final_status="retry_requested",
                            )
                            try:
                                followup_payload_name = (
                                    f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}"
                                    "_obligation_review_followup.json"
                                )
                                payload_path = _write_text_artifact(
                                    followup_payload_name,
                                    json.dumps(payload, ensure_ascii=False, indent=2),
                                )
                                _register_artifact(
                                    "obligation_review_followup",
                                    payload_path,
                                    title="Follow-up obligation review",
                                )
                                _write_artifacts_index()
                                outputs = obligation_review_bundle_to_outputs(payload)
                                _set_blocking_stage_state(
                                    "followup_obligation_review",
                                    attempts=2,
                                    max_attempts=2,
                                    final_status="ok",
                                )
                                return {
                                    "outputs": outputs,
                                    "entry": {
                                        "task_id": "followup_obligation_review",
                                        "title": "Follow-up obligation review",
                                        "status": "ok",
                                        "step_type": "use_cli",
                                        "summary": str(payload.get("verdict") or "").strip() or "follow-up obligation review",
                                        "outputs": outputs,
                                        "claims": list(payload.get("claims") or []),
                                        "artifacts": [{"type": "json", "path": payload_path}],
                                        "orchestrator_artifact": payload_path,
                                    },
                                    "payload": payload,
                                }
                            except Exception as persist_exc:
                                self._log.warning(
                                    "follow-up obligation review persist retry failed session=%s err=%s",
                                    getattr(session, "id", ""),
                                    persist_exc,
                                )
                                continue
                        failure_reason = "followup_obligation_review retry_exhausted execution_failed_persist"
                        _append_runtime_degraded_mode(failure_reason)
                        _set_blocking_stage_state(
                            "followup_obligation_review",
                            attempts=2,
                            max_attempts=2,
                            failure_kind="execution_failed",
                            retry_context=retry_context,
                            final_status="retry_exhausted",
                        )
                        followup_obligation_review_payload = {}
                        return {
                            "outputs": [degraded_mode_output(failure_reason)],
                            "entry": None,
                            "payload": {},
                        }

            async def _assess(text: str) -> Dict[str, Any]:
                def _build_system(*, retry: bool) -> str:
                    template_output_kind = str(active.get("output_kind") or "").strip().lower()
                    execution_handoff_expected = repo_grounded_required and template_output_kind == "spec"
                    system = ""
                    if qa_prompt:
                        system += f"{qa_prompt}\n\n"
                    system += (
                        "Ответь строго JSON-объектом:\n"
                        "{\n"
                        '  \"needs_rework\": true|false,\n'
                        '  \"issues\": [\"...\"],\n'
                        '  \"missing_sections\": [\"...\"],\n'
                        '  \"required_input_gaps\": [\"...\"],\n'
                        '  \"placeholder_gaps\": [\"...\"],\n'
                        '  \"implementation_handoff_gaps\": [\"...\"],\n'
                        '  \"spec_to_plan_gaps\": [\"...\"]\n'
                        "}\n"
                        "Если раздел формально есть, но слишком слабый/неконкретный, считай это недостатком."
                    )
                    if required_inputs:
                        system += (
                            '\n- "required_input_gaps": список элементов из обязательных входов задачи, '
                            "которые документ не закрывает явно; возвращай элементы ровно из списка required_inputs.\n"
                        )
                    system += (
                        "\nДополнительно проверь и верни, если найдешь проблемы:\n"
                        '- "codebase_mismatches": расхождения текста с реальными файлами проекта;\n'
                        '- "unsupported_assumptions": предположения без опоры на код или запрос;\n'
                        '- "unverified_claims": любые surface/integration/capability claims без прямой '
                        "опоры на репозиторий или явный запрос пользователя;\n"
                        '- "evidence_gaps": confirmed claims без привязки к repo/file evidence;\n'
                        '- "config_contract_gaps": выдуманные config keys, fallback layers или '
                        "compatibility wrappers;\n"
                        '- "migration_gaps": пропущенные миграции state/payload/API;\n'
                        '- "doc_sync_gaps": несинхронность README/документации с предложенными '
                        "изменениями;\n"
                        '- "test_gaps": отсутствующие или несоответствующие тесты.\n'
                        "Любое непустое значение в этих полях считается достаточным основанием для доработки.\n"
                        "Для repo-grounded ответа допускаются только два вида статуса:\n"
                        '- подтверждено evidence из репозитория;\n'
                        '- "не подтверждено" / "требует отдельной проверки".\n'
                        "Не оставляй гипотезы и не достраивай выводы по аналогии.\n"
                        "Обязательно проверь:\n"
                        "- не противоречит ли ТЗ текущему коду и реальным файлам проекта;\n"
                        "- не введены ли новые сущности, API, config keys, fallback layers или "
                        "compatibility wrappers без явного основания в коде или запросе;\n"
                        "- не названы ли как факты интеграции, поверхности или capability, "
                        "для которых в репозитории нет прямого подтверждения;\n"
                        "- явно ли описаны impacts для config/docs/tests;\n"
                        "- достаточно ли документ детализирован для low-middle разработчика "
                        "без устных пояснений.\n"
                    )
                    if execution_handoff_expected:
                        system += (
                            "\nДля execution-ready repo-grounded spec дополнительно проверь и верни:\n"
                            '- "placeholder_gaps": плейсхолдеры и недетерминированные формулировки '
                            '(TODO, TBD, "дописать позже", "нужно добавить тесты", '
                            '"обработать edge cases" без конкретики);\n'
                            '- "implementation_handoff_gaps": проблемы раздела "Implementation handoff по компонентам и файлам", '
                            'если там нет конкретных компонентов/файлов, не ясно что меняется, как проверять '
                            'или какие тесты/команды запускать;\n'
                            '- "spec_to_plan_gaps": Must-FR/UC/API/NFR, у которых нет конкретного способа реализации '
                            "или проверки в плане/implementation handoff.\n"
                            "Любое непустое значение в этих полях считается blocking reason.\n"
                        )
                    if is_large_spec:
                        system += (
                            "\nДля больших ТЗ дополнительно проверь и верни:\n"
                            '- "weak_sections": список разделов, которые формально есть, '
                            'но слишком слабые, общие или неполные;\n'
                            '- "missing_counts": список недоборов по количественным требованиям '
                            '(например, сценариев, FR, NFR, API, критериев приемки);\n'
                            '- "traceability_gaps": список пробелов трассируемости '
                            '(например, Must-требования без приемки, FR без теста, '
                            'API без связанного сценария).\n'
                            "Если такие проблемы есть, это должно считаться основанием для доработки.\n"
                            "Для large-спецификации ответь строго JSON-объектом:\n"
                            "{\n"
                            '  "needs_rework": true|false,\n'
                            '  "issues": ["..."],\n'
                            '  "missing_sections": ["..."],\n'
                            '  "required_input_gaps": ["..."],\n'
                            '  "placeholder_gaps": ["..."],\n'
                            '  "implementation_handoff_gaps": ["..."],\n'
                            '  "spec_to_plan_gaps": ["..."],\n'
                            '  "weak_sections": ["..."],\n'
                            '  "missing_counts": ["..."],\n'
                            '  "traceability_gaps": ["..."],\n'
                            '  "codebase_mismatches": ["..."],\n'
                            '  "unsupported_assumptions": ["..."],\n'
                            '  "unverified_claims": ["..."],\n'
                            '  "evidence_gaps": ["..."],\n'
                            '  "config_contract_gaps": ["..."],\n'
                            '  "migration_gaps": ["..."],\n'
                            '  "doc_sync_gaps": ["..."],\n'
                            '  "test_gaps": ["..."]\n'
                            "}\n"
                        )
                    if retry:
                        system += (
                            "\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ. "
                            "Верни только JSON-объект по указанной схеме, без markdown и пояснений."
                        )
                    return system

                def _norm_list(value: Any) -> List[str]:
                    if not isinstance(value, list):
                        return []
                    return [str(x) for x in value if str(x).strip()]

                def _merge_unique_strings(*groups: List[str]) -> List[str]:
                    merged: List[str] = []
                    seen: set[str] = set()
                    for group in groups:
                        for item in group or []:
                            text = str(item or "").strip()
                            if not text or text in seen:
                                continue
                            seen.add(text)
                            merged.append(text)
                    return merged

                assessment_repo_gap_fields = tuple(
                    field_name
                    for field_name in repo_gap_labels
                    if field_name not in {"issues", "weak_sections", "missing_counts", "traceability_gaps", "degraded_modes"}
                )

                def _build_assessment_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
                    def _norm_list(value: Any) -> List[str]:
                        if not isinstance(value, list):
                            return []
                        return [str(x) for x in value if str(x).strip()]

                    def _merge_unique_strings(*groups: List[str]) -> List[str]:
                        merged: List[str] = []
                        seen: set[str] = set()
                        for group in groups:
                            for item in group or []:
                                text_local = str(item or "").strip()
                                if not text_local or text_local in seen:
                                    continue
                                seen.add(text_local)
                                merged.append(text_local)
                        return merged

                    issues_norm = _norm_list(payload.get("issues"))
                    missing_norm = _norm_list(payload.get("missing_sections"))
                    required_input_gaps_norm = _norm_list(payload.get("required_input_gaps"))
                    placeholder_gaps_norm = _norm_list(payload.get("placeholder_gaps"))
                    implementation_handoff_gaps_norm = _norm_list(payload.get("implementation_handoff_gaps"))
                    spec_to_plan_gaps_norm = _norm_list(payload.get("spec_to_plan_gaps"))
                    weak_norm = _norm_list(payload.get("weak_sections"))
                    counts_norm = _norm_list(payload.get("missing_counts"))
                    trace_norm = _norm_list(payload.get("traceability_gaps"))
                    repo_gap_details = {
                        field_name: _norm_list(payload.get(field_name))
                        for field_name in assessment_repo_gap_fields
                    }
                    runtime_repo_gap_details = verify_claim_ledger(
                        claim_ledger,
                        repo_grounded_required=repo_grounded_required,
                    )
                    review_output_types = {
                        CLIOutputType.REPO_REVIEW_MISMATCH,
                        CLIOutputType.REPO_REVIEW_UNVERIFIED_CLAIM,
                        CLIOutputType.REPO_REVIEW_CORRECTION,
                        CLIOutputType.DEGRADED_MODE,
                        "open_gap",
                    }

                    def _is_review_output_group(outputs: Any) -> bool:
                        if not isinstance(outputs, list):
                            return False
                        return any(
                            isinstance(output, dict)
                            and str(output.get("type") or "").strip() in review_output_types
                            for output in outputs
                        )

                    review_output_groups: List[List[Dict[str, Any]]] = []
                    if followup_obligation_review_payload:
                        if isinstance(followup_repo_review_outputs, list):
                            review_output_groups = [list(followup_repo_review_outputs)]
                        else:
                            review_output_groups = [[]]
                    elif _is_review_output_group(followup_repo_review_outputs):
                        review_output_groups = [list(followup_repo_review_outputs)]
                    else:
                        latest_review_outputs: List[Dict[str, Any]] = []
                        for item in step_results:
                            if not isinstance(item, dict):
                                continue
                            task_id = str(item.get("task_id") or "").strip()
                            step_type = str(item.get("step_type") or "").strip()
                            if step_type != "use_cli" and task_id != "use_cli_repo_final_review":
                                continue
                            outputs = item.get("outputs") or []
                            if _is_review_output_group(outputs):
                                latest_review_outputs = list(outputs)
                        if latest_review_outputs:
                            review_output_groups = [latest_review_outputs]
                    review_runtime_gaps = collect_repo_review_runtime_gaps_from_outputs(
                        review_output_groups,
                    )
                    external_reference_runtime_gaps = _collect_external_reference_runtime_gaps(text)
                    template_output_kind = str(active.get("output_kind") or "").strip().lower()
                    execution_handoff_expected = repo_grounded_required and template_output_kind == "spec"
                    placeholder_runtime_gaps = (
                        collect_placeholder_gaps(text)
                        if execution_handoff_expected
                        else []
                    )
                    section_contract_runtime_gaps = _collect_required_top_level_section_contract_gaps(
                        text,
                        user_query=user_query,
                        required_sections=_resolve_required_top_level_sections(
                            user_query=user_query,
                            text=text,
                        ),
                    )
                    implementation_handoff_runtime_gaps = (
                        collect_implementation_handoff_gaps(
                            text,
                            required_sections=_resolve_required_top_level_sections(
                                user_query=user_query,
                                text=text,
                            ),
                        )
                        if execution_handoff_expected
                        else []
                    )
                    for item in runtime_degraded_modes:
                        degraded_text = str(item or "").strip()
                        if degraded_text and degraded_text not in (review_runtime_gaps.get("degraded_modes") or []):
                            review_runtime_gaps.setdefault("degraded_modes", []).append(degraded_text)
                    review_forced_rework = bool(review_runtime_gaps.get("issues"))
                    structural_needs_rework = bool(payload.get("needs_rework"))
                    placeholder_gaps_norm = _merge_unique_strings(
                        placeholder_gaps_norm,
                        placeholder_runtime_gaps,
                    )
                    implementation_handoff_gaps_norm = _merge_unique_strings(
                        implementation_handoff_gaps_norm,
                        implementation_handoff_runtime_gaps,
                    )
                    if (
                        required_input_gaps_norm
                        or placeholder_gaps_norm
                        or implementation_handoff_gaps_norm
                        or spec_to_plan_gaps_norm
                        or (is_large_spec and (weak_norm or counts_norm or trace_norm))
                    ):
                        structural_needs_rework = True
                    issues_norm = _merge_unique_strings(
                        issues_norm,
                        list(review_runtime_gaps.get("issues") or []),
                    )
                    missing_norm = _merge_unique_strings(
                        missing_norm,
                        list(external_reference_runtime_gaps.get("missing_sections") or []),
                    )
                    missing_norm = _merge_unique_strings(
                        missing_norm,
                        list(section_contract_runtime_gaps.get("missing_sections") or []),
                    )
                    section_contract_gaps_norm = _norm_list(
                        section_contract_runtime_gaps.get("section_contract_gaps")
                    )
                    if section_contract_runtime_gaps.get("missing_sections") or section_contract_gaps_norm:
                        structural_needs_rework = True
                    for field_name, items in review_runtime_gaps.items():
                        if field_name in {"issues", "degraded_modes"}:
                            continue
                        repo_gap_details[field_name] = _merge_unique_strings(
                            list(repo_gap_details.get(field_name) or []),
                            list(items or []),
                        )
                    repo_gap_details["external_reference_gaps"] = _merge_unique_strings(
                        list(repo_gap_details.get("external_reference_gaps") or []),
                        list(external_reference_runtime_gaps.get("external_reference_gaps") or []),
                    )
                    for field_name, items in runtime_repo_gap_details.items():
                        repo_gap_details[field_name] = _merge_unique_strings(
                            list(repo_gap_details.get(field_name) or []),
                            list(items or []),
                        )
                    followup_required_corrections = [
                        str(item).strip()
                        for item in (followup_obligation_review_payload.get("required_corrections") or [])
                        if str(item).strip()
                    ]
                    if followup_required_corrections:
                        issues_norm = _merge_unique_strings(issues_norm, followup_required_corrections)
                    followup_false_closure_reasons = [
                        str(item.get("statement") or item.get("text") or "").strip()
                        for item in (followup_obligation_review_payload.get("false_closures") or [])
                        if isinstance(item, dict)
                        and str(item.get("statement") or item.get("text") or "").strip()
                    ]
                    if followup_false_closure_reasons:
                        issues_norm = _merge_unique_strings(issues_norm, followup_false_closure_reasons)
                    followup_unsupported_assertions = [
                        str(item).strip()
                        for item in (followup_obligation_review_payload.get("unsupported_assertions") or [])
                        if str(item).strip()
                    ]
                    if followup_unsupported_assertions:
                        repo_gap_details["unverified_claims"] = _merge_unique_strings(
                            list(repo_gap_details.get("unverified_claims") or []),
                            followup_unsupported_assertions,
                        )
                    merged_degraded_modes = _merge_unique_strings(
                        list(runtime_degraded_modes),
                        list(review_runtime_gaps.get("degraded_modes") or []),
                    )
                    if followup_obligation_review_payload:
                        followup_text_fragments: List[str] = []
                        for field_name in (
                            "required_corrections",
                            "unsupported_assertions",
                            "degraded_modes",
                        ):
                            followup_text_fragments.extend(
                                str(item).strip()
                                for item in (followup_obligation_review_payload.get(field_name) or [])
                                if str(item).strip()
                            )
                        for field_name in ("open_blocking_obligations", "false_closures"):
                            for item in (followup_obligation_review_payload.get(field_name) or []):
                                if not isinstance(item, dict):
                                    followup_text = str(item).strip()
                                else:
                                    followup_text = str(item.get("statement") or item.get("text") or "").strip()
                                if followup_text:
                                    followup_text_fragments.append(followup_text)
                        followup_mentions_invalid_bundle = any(
                            "invalid_bundle_fallback_to_text" in fragment.casefold()
                            for fragment in followup_text_fragments
                        )
                        if not followup_mentions_invalid_bundle:
                            merged_degraded_modes = [
                                item
                                for item in merged_degraded_modes
                                if "invalid_bundle_fallback_to_text" not in str(item or "").casefold()
                            ]
                    all_missing_required_artifacts = _merge_unique_strings(
                        list(missing_required_artifacts),
                        list(stage_missing_required_artifacts),
                    )
                    evidence_needs_rework = bool(merged_degraded_modes) or any(repo_gap_details.values())
                    assessment_payload = {
                        "needs_rework": False,
                        "issues": issues_norm,
                        "missing_sections": missing_norm,
                        "section_contract_gaps": section_contract_gaps_norm,
                        "required_input_gaps": required_input_gaps_norm,
                        "placeholder_gaps": placeholder_gaps_norm,
                        "implementation_handoff_gaps": implementation_handoff_gaps_norm,
                        "spec_to_plan_gaps": spec_to_plan_gaps_norm,
                        "weak_sections": weak_norm,
                        "missing_counts": counts_norm,
                        "traceability_gaps": trace_norm,
                        **repo_gap_details,
                        "missing_required_artifacts": all_missing_required_artifacts,
                        "fix_closed_obligations": list(spec_fix_payload.get("closed_obligations") or []),
                        "fix_remaining_obligations": list(spec_fix_payload.get("remaining_obligations") or []),
                        "degraded_modes": merged_degraded_modes,
                        "followup_closed_blocking_obligations": list(
                            followup_obligation_review_payload.get("closed_blocking_obligations") or []
                        ),
                        "followup_open_blocking_obligations": list(
                            followup_obligation_review_payload.get("open_blocking_obligations") or []
                        ),
                        "followup_false_closures": list(
                            followup_obligation_review_payload.get("false_closures") or []
                        ),
                    }
                    obligations = build_obligation_matrix(
                        task_contract=task_contract_payload,
                        assessment=assessment_payload,
                        required_step_statuses=_required_repo_step_statuses(),
                    )
                    obligation_groups = split_obligations_by_blocking(obligations)
                    open_blocking_obligations = collect_open_blocking_obligations(obligations)
                    followup_forced_rework = bool(followup_required_corrections) or bool(
                        followup_false_closure_reasons
                    )
                    needs_rework = (
                        structural_needs_rework
                        or evidence_needs_rework
                        or review_forced_rework
                        or followup_forced_rework
                    )
                    assessment_payload["needs_rework"] = needs_rework
                    return {
                        "needs_rework": needs_rework,
                        "structural_needs_rework": structural_needs_rework,
                        "evidence_needs_rework": evidence_needs_rework,
                        "review_forced_rework": review_forced_rework,
                        "followup_forced_rework": followup_forced_rework,
                        "issues": issues_norm,
                        "missing_sections": missing_norm,
                        "section_contract_gaps": section_contract_gaps_norm,
                        "required_input_gaps": required_input_gaps_norm,
                        "placeholder_gaps": placeholder_gaps_norm,
                        "implementation_handoff_gaps": implementation_handoff_gaps_norm,
                        "spec_to_plan_gaps": spec_to_plan_gaps_norm,
                        "weak_sections": weak_norm,
                        "missing_counts": counts_norm,
                        "traceability_gaps": trace_norm,
                        **repo_gap_details,
                        "degraded_modes": merged_degraded_modes,
                        "obligation_model_active": True,
                        "task_contract": dict(task_contract_payload),
                        "obligation_matrix": obligations,
                        "blocking_obligations": list(obligation_groups.get("blocking") or []),
                        "open_blocking_obligations": open_blocking_obligations,
                        "non_blocking_obligations": list(obligation_groups.get("non_blocking") or []),
                        "missing_required_artifacts": all_missing_required_artifacts,
                        "fix_closed_obligations": list(spec_fix_payload.get("closed_obligations") or []),
                        "blocking_stage_states": dict(blocking_stage_states),
                        "followup_closed_blocking_obligations": list(
                            followup_obligation_review_payload.get("closed_blocking_obligations") or []
                        ),
                        "followup_open_blocking_obligations": list(
                            followup_obligation_review_payload.get("open_blocking_obligations") or []
                        ),
                        "followup_false_closures": list(
                            followup_obligation_review_payload.get("false_closures") or []
                        ),
                        "qc_layers": {
                            "structural": {
                                "needs_rework": structural_needs_rework,
                                "issues": issues_norm,
                                "missing_sections": missing_norm,
                                "section_contract_gaps": section_contract_gaps_norm,
                                "required_input_gaps": required_input_gaps_norm,
                                "placeholder_gaps": placeholder_gaps_norm,
                                "implementation_handoff_gaps": implementation_handoff_gaps_norm,
                                "spec_to_plan_gaps": spec_to_plan_gaps_norm,
                                "weak_sections": weak_norm,
                                "missing_counts": counts_norm,
                                "traceability_gaps": trace_norm,
                            },
                            "evidence": {
                                "needs_rework": evidence_needs_rework,
                                **repo_gap_details,
                                "degraded_modes": merged_degraded_modes,
                            },
                        },
                        "assessment_error": False,
                    }

                if compose_final_answer_normalize_fallback_used:
                    self._log.info(
                        "final qc assessment: using runtime-only fallback after compose normalize fallback session=%s",
                        getattr(session, "id", ""),
                    )
                    deterministic_payload = {
                        "needs_rework": False,
                        "issues": [],
                        "missing_sections": [],
                        "required_input_gaps": [],
                        "placeholder_gaps": [],
                        "implementation_handoff_gaps": [],
                        "spec_to_plan_gaps": [],
                        "weak_sections": [],
                        "missing_counts": [],
                        "traceability_gaps": [],
                        **{field_name: [] for field_name in assessment_repo_gap_fields},
                    }
                    return _build_assessment_from_payload(deterministic_payload)

                req = "\n".join(f"- {x}" for x in required) if required else "- (не заданы)"
                required_inputs_txt = "\n".join(f"- {x}" for x in required_inputs) if required_inputs else "- (не заданы)"
                repo_evidence_txt = _build_repo_evidence_text()
                external_reference_entries = _collect_external_reference_entries(
                    user_query=user_query,
                    mapping_text=text,
                )
                user_parts = [
                    f"Исходный запрос пользователя:\n{user_query}\n\n",
                    f"Обязательные разделы:\n{req}\n\n",
                    f"Обязательные входы задачи:\n{required_inputs_txt}\n\n",
                    f"Repo evidence из выполненных шагов:\n{repo_evidence_txt}\n\n",
                ]
                if external_reference_entries:
                    user_parts.append(
                        "Внешние референсы и implementation guidance:\n"
                        + _render_external_references_block(external_reference_entries)
                        + "\n\n"
                    )
                if raw_user_clarification_answers:
                    user_parts.append(
                        "Полученные уточнения пользователя:\n"
                        + "\n".join(f"- {answer}" for answer in raw_user_clarification_answers)
                        + "\n\n"
                    )
                if claim_ledger_path:
                    user_parts.append(f"Claim ledger artifact:\n{claim_ledger_path}\n\n")
                if fact_pack_path:
                    user_parts.append(f"Fact pack artifact:\n{fact_pack_path}\n\n")
                if draft_path:
                    user_parts.append(f"Draft artifact:\n{draft_path}\n\n")
                if polished_path:
                    user_parts.append(f"Polished artifact:\n{polished_path}\n\n")
                if open_gaps_path:
                    user_parts.append(f"Open gaps artifact:\n{open_gaps_path}\n\n")
                user_parts.append(f"Текущий текст ТЗ:\n{text}")
                user = "".join(user_parts)
                for attempt in (1, 2):
                    raw = await self._deps.chat_completion(
                        self._config,
                        _build_system(retry=attempt == 2),
                        user,
                        response_format={"type": "json_object"},
                    )
                    if not raw:
                        self._log.warning(
                            "final qc assess empty response attempt=%d/2",
                            attempt,
                        )
                        continue
                    try:
                        payload = parse_normalize_validate(raw, build_assessment_schema(is_large_spec=is_large_spec))
                        return _build_assessment_from_payload(payload)
                    except Exception as e:
                        self._log.warning(
                            "final qc assess parse failed attempt=%d/2: %s",
                            attempt,
                            e,
                        )
                        continue
                return {
                    "needs_rework": False,
                    "structural_needs_rework": False,
                    "evidence_needs_rework": False,
                    "review_forced_rework": False,
                    "followup_forced_rework": False,
                    "issues": ["qc_assessment_parse_error"],
                    "missing_sections": [],
                    "section_contract_gaps": [],
                    "required_input_gaps": [],
                    "placeholder_gaps": [],
                    "implementation_handoff_gaps": [],
                    "spec_to_plan_gaps": [],
                    "weak_sections": [],
                    "missing_counts": [],
                    "traceability_gaps": [],
                    **{field_name: [] for field_name in assessment_repo_gap_fields},
                    "degraded_modes": list(runtime_degraded_modes),
                    "obligation_model_active": True,
                    "task_contract": dict(task_contract_payload),
                    "obligation_matrix": [],
                    "blocking_obligations": [],
                    "open_blocking_obligations": [],
                    "non_blocking_obligations": [],
                    "missing_required_artifacts": _merge_unique_strings(
                        list(missing_required_artifacts),
                        list(stage_missing_required_artifacts),
                    ),
                    "fix_closed_obligations": list(spec_fix_payload.get("closed_obligations") or []),
                    "blocking_stage_states": dict(blocking_stage_states),
                    "followup_closed_blocking_obligations": list(
                        followup_obligation_review_payload.get("closed_blocking_obligations") or []
                    ),
                    "followup_open_blocking_obligations": list(
                        followup_obligation_review_payload.get("open_blocking_obligations") or []
                    ),
                    "followup_false_closures": list(
                        followup_obligation_review_payload.get("false_closures") or []
                    ),
                    "qc_layers": {
                        "structural": {
                            "needs_rework": False,
                            "issues": ["qc_assessment_parse_error"],
                            "missing_sections": [],
                            "section_contract_gaps": [],
                            "required_input_gaps": [],
                            "placeholder_gaps": [],
                            "implementation_handoff_gaps": [],
                            "spec_to_plan_gaps": [],
                            "weak_sections": [],
                            "missing_counts": [],
                            "traceability_gaps": [],
                        },
                        "evidence": {
                            "needs_rework": False,
                            **{field_name: [] for field_name in assessment_repo_gap_fields},
                            "degraded_modes": list(runtime_degraded_modes),
                        },
                    },
                    "assessment_error": True,
                }

            initial_assessment = {}
            repo_rework_exhausted = False
            skip_quality_loop = False
            if repo_grounded_required:
                initial_assessment = await _assess(current)
                if initial_assessment.get("required_input_gaps"):
                    self._log.warning(
                        "final qc initial assessment has required input gaps; "
                        "continuing with corrective rework without late ask_user session=%s gaps=%s",
                        getattr(session, "id", ""),
                        initial_assessment.get("required_input_gaps") or [],
                    )
                    _emit_orch(
                        "final_qc_required_input_gaps",
                        "partial",
                        "Финальная проверка нашла незакрытые входы; запускаю корректировку без нового вопроса",
                    )
                    if not strict_analyst_runtime_context:
                        current = _apply_required_input_assumptions_section(
                            current,
                            user_query=user_query,
                            required_input_gaps=initial_assessment.get("required_input_gaps") or [],
                        )

            async def _rework(
                text: str,
                issues: List[str],
                missing: List[str],
                required_input_gaps: List[str],
                weak_sections: List[str],
                missing_counts: List[str],
                traceability_gaps: List[str],
                repo_gap_details: Dict[str, List[str]],
                stage_assessment: Optional[Dict[str, Any]] = None,
            ) -> str:
                nonlocal draft_path, polished_path, artifacts_index_path, repo_rework_exhausted
                repo_rework_exhausted = False

                def _accept_reworked_candidate(candidate_text: str, *, attempt: int) -> tuple[str, str]:
                    merged_text = _merge_reworked_spec_text(
                        current_text=text,
                        candidate_text=candidate_text,
                        user_query=user_query,
                    )
                    section_contract = _collect_required_top_level_section_contract_gaps(
                        merged_text,
                        user_query=user_query,
                        required_sections=required,
                    )
                    if section_contract["missing_sections"] or section_contract["section_contract_gaps"]:
                        self._log.warning(
                            "final rework violated section contract attempt=%d/2 missing=%s order=%s",
                            attempt,
                            section_contract["missing_sections"],
                            section_contract["section_contract_gaps"],
                        )
                        retry_details: List[str] = []
                        if section_contract["missing_sections"]:
                            retry_details.append(
                                "Missing sections: " + ", ".join(section_contract["missing_sections"])
                            )
                        retry_details.extend(section_contract["section_contract_gaps"])
                        retry_context = (
                            "CLI gap-closure output violated required top-level section contract. "
                            + " ".join(item for item in retry_details if str(item or "").strip())
                        ).strip()
                        return "", retry_context
                    return merged_text, ""

                if repo_grounded_required:
                    if not mandatory_bundle_ready:
                        repo_rework_exhausted = True
                        return ""
                    current_draft_path = ""
                    retry_context = ""
                    for persist_attempt in (1, 2):
                        try:
                            normalized_text = _apply_protected_spec_shell(text, user_query=user_query)
                            current_draft_path = _write_text_artifact(
                                f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_draft.md",
                                normalized_text,
                            )
                            draft_path = current_draft_path
                            _register_artifact("draft", current_draft_path, title="Current draft")
                            artifacts_index_path = _write_artifacts_index()
                            break
                        except Exception as e:
                            self._log.warning(
                                "failed to persist gap-closure draft session=%s attempt=%d/2 err=%s",
                                getattr(session, "id", ""),
                                persist_attempt,
                                e,
                            )
                            retry_context = "gap-closure draft persist failed"
                            if persist_attempt == 1:
                                _set_blocking_stage_state(
                                    "final_cli_gap_closure",
                                    attempts=persist_attempt,
                                    max_attempts=2,
                                    failure_kind="execution_failed",
                                    retry_context=retry_context,
                                    final_status="retry_requested",
                                )
                                continue
                            failure_reason = "final_cli_gap_closure retry_exhausted execution_failed_draft_persist"
                            _append_runtime_degraded_mode(failure_reason)
                            _set_blocking_stage_state(
                                "final_cli_gap_closure",
                                attempts=persist_attempt,
                                max_attempts=2,
                                failure_kind="execution_failed",
                                retry_context=retry_context,
                                final_status="retry_exhausted",
                            )
                            repo_rework_exhausted = True
                            return ""
                    polished = ""
                    polished_bundle: Dict[str, Any] = {}
                    retry_context = ""
                    for polish_attempt in (1, 2):
                        polished_bundle = await _run_cli_gap_closure_polish(
                            draft_path=current_draft_path,
                            fact_pack_path=fact_pack_path,
                            claim_ledger_path=claim_ledger_path,
                            open_gaps_path=open_gaps_path,
                            artifacts_index_path=artifacts_index_path,
                            task_contract_path=task_contract_path,
                            obligation_matrix_path=obligation_matrix_path,
                            refresh_preflight=(
                                lambda: _refresh_stage_preflight_artifacts(
                                    draft_text=text,
                                    assessment_payload=stage_assessment or {},
                                    stage_id="gap_closure_bundle",
                                )
                            )
                            if stage_assessment
                            else None,
                            retry_context_override=retry_context,
                        )
                        polished_candidate = str((polished_bundle or {}).get("final_text") or "").strip()
                        if not polished_candidate:
                            repo_rework_exhausted = True
                            return ""
                        polished, retry_context = _accept_reworked_candidate(
                            polished_candidate,
                            attempt=polish_attempt,
                        )
                        if polished:
                            break
                        if polish_attempt == 1:
                            _set_blocking_stage_state(
                                "final_cli_gap_closure",
                                attempts=polish_attempt,
                                max_attempts=2,
                                failure_kind="invalid_bundle",
                                retry_context=retry_context,
                                final_status="retry_requested",
                            )
                            continue
                        failure_reason = "final_cli_gap_closure invalid_section_contract"
                        _append_runtime_degraded_mode(failure_reason)
                        _set_blocking_stage_state(
                            "final_cli_gap_closure",
                            attempts=polish_attempt,
                            max_attempts=2,
                            failure_kind="invalid_bundle",
                            retry_context=retry_context,
                            final_status="retry_exhausted",
                        )
                        repo_rework_exhausted = True
                        return ""
                    if not polished:
                        repo_rework_exhausted = True
                        return ""
                    retry_context = "gap-closure polished artifact persist failed"
                    for persist_attempt in (1, 2):
                        try:
                            polished_path = _write_text_artifact(
                                f"{_slug(str(getattr(session, 'id', '') or 'session'), fallback='session')}_draft_polished.md",
                                polished,
                            )
                            _register_artifact("draft_polished", polished_path, title="CLI polished draft")
                            for item in (polished_bundle or {}).get("evidence") or []:
                                if not isinstance(item, dict):
                                    continue
                                _register_artifact(
                                    "cli_polish_evidence",
                                    str(item.get("path") or "").strip(),
                                    title="CLI polish evidence",
                                    meta={"preview": str(item.get("preview") or "").strip()},
                                )
                            artifacts_index_path = _write_artifacts_index()
                            self._log.info(
                                "final cli gap closure applied session=%s draft=%s polished=%s",
                                getattr(session, "id", ""),
                                current_draft_path,
                                polished_path,
                            )
                            _set_blocking_stage_state(
                                "final_cli_gap_closure",
                                attempts=persist_attempt,
                                max_attempts=2,
                                final_status="ok",
                            )
                            return polished
                        except Exception as e:
                            self._log.warning(
                                "failed to persist cli-polished draft session=%s attempt=%d/2 err=%s",
                                getattr(session, "id", ""),
                                persist_attempt,
                                e,
                            )
                            if persist_attempt == 1:
                                _set_blocking_stage_state(
                                    "final_cli_gap_closure",
                                    attempts=persist_attempt,
                                    max_attempts=2,
                                    failure_kind="execution_failed",
                                    retry_context=retry_context,
                                    final_status="retry_requested",
                                )
                                continue
                            failure_reason = "final_cli_gap_closure retry_exhausted execution_failed_persist"
                            _append_runtime_degraded_mode(failure_reason)
                            _set_blocking_stage_state(
                                "final_cli_gap_closure",
                                attempts=persist_attempt,
                                max_attempts=2,
                                failure_kind="execution_failed",
                                retry_context=retry_context,
                                final_status="retry_exhausted",
                            )
                            repo_rework_exhausted = True
                            return ""

                system = ""
                if qa_prompt:
                    system += f"Критерии качества:\n{qa_prompt}\n\n"
                system += (
                    "Ты старший системный аналитик. Доработай ТЗ и верни строго JSON-объект "
                    '{"final_text":"..."} без markdown-обёртки. '
                    "Исправь выявленные недостатки, сохраняя исходные факты, устрани противоречия "
                    "текущему коду и сделай документ пригодным для реализации low-middle "
                    "разработчиком без устных пояснений.\n"
                    "Правила:\n"
                    "- Закрой все обязательные разделы.\n"
                    "- Работай в режиме preservation-first patch/merge поверх текущего draft, "
                    "а не rewrite-from-scratch.\n"
                    "- Сохраняй protected spec shell: title, `Исходная задача`, core shell sections, "
                    "`Открытые вопросы и валидационные шаги`.\n"
                    "- Потеря protected spec shell считается preservation regression: такие секции "
                    "нужно сохранить и доработать, а не удалять.\n"
                )
                if strict_analyst_runtime_context:
                    system += (
                        "- Не задавай вопросов пользователю на финальной корректировке: "
                        "run должен завершиться выдачей полноценного ТЗ.\n"
                        "- Если обязательные входы задачи остаются незакрытыми, закрой их в рамках "
                        "имеющегося контекста или оформи как конкретные implementation validation steps "
                        "в профильном разделе ТЗ, без статуса блокировки результата.\n"
                    )
                else:
                    system += (
                        '- Если обязательные входы задачи остаются незакрытыми и runtime не остановил run на clarification,\n'
                        '  вынеси их в раздел "Допущения и незакрытые входы".\n'
                    )
                if required_input_gaps:
                    system += (
                        "\nОБЯЗАТЕЛЬНО ЗАКРОЙ REQUIRED_INPUT_GAPS ИЗ СПИСКА НИЖЕ.\n"
                        "- Не оставляй их неявно: внеси ответ в профильные разделы ТЗ.\n"
                        "- Если evidence недостаточно, оформи это как validation gate или open gap, но не как confirmed fact.\n"
                    )
                    impacted_scope_gap = any(
                        "затронут" in str(item or "").casefold()
                        and (
                            "компон" in str(item or "").casefold()
                            or "модул" in str(item or "").casefold()
                            or "файл" in str(item or "").casefold()
                        )
                        for item in required_input_gaps
                    )
                    if impacted_scope_gap:
                        system += (
                            "- Для gaps про затронутые компоненты явно раздели `точно затронутые` и "
                            "`предположительно затронутые / требуют отдельной проверки` зоны.\n"
                            "- Не перечисляй один и тот же компонент одновременно как confirmed impact и hypothesis.\n"
                            "- Для каждой предположительно затронутой зоны укажи validation signal, который её подтвердит или исключит.\n"
                        )
                system += (
                    "- Пиши конкретно, без воды.\n"
                    '- Не оставляй гипотезы; если доказательств нет, пиши "не подтверждено" '
                    'или "требует отдельной проверки".\n'
                    "- Не придумывай новые сущности, API, config keys, fallback layers или "
                    "compatibility wrappers без основания в коде или запросе.\n"
                    "- Указывай impacts только для реально подтвержденных затронутых зон "
                    "(config/docs/tests и другие артефакты, если они подтверждены репозиторием).\n"
                    "- Не добавляй собственный статус готовности/реализации: runtime добавит его отдельно.\n"
                    "- Не ссылайся на процесс проверки, дай только итоговый ТЗ-текст в поле final_text.\n"
                    "- Не имитируй tool use и не используй псевдо-разметку вроде [TOOL_CALL]."
                )
                req = "\n".join(f"- {x}" for x in required) if required else "- (не заданы)"
                issues_txt = "\n".join(f"- {x}" for x in issues) if issues else "- (не указаны)"
                miss_txt = "\n".join(f"- {x}" for x in missing) if missing else "- (нет)"
                required_inputs_gap_txt = "\n".join(f"- {x}" for x in required_input_gaps) if required_input_gaps else "- (нет)"
                weak_txt = "\n".join(f"- {x}" for x in weak_sections) if weak_sections else "- (нет)"
                counts_txt = "\n".join(f"- {x}" for x in missing_counts) if missing_counts else "- (нет)"
                trace_txt = "\n".join(f"- {x}" for x in traceability_gaps) if traceability_gaps else "- (нет)"
                repo_gap_sections = [
                    (
                        label,
                        "\n".join(f"- {item}" for item in repo_gap_details.get(field_name, []))
                        if repo_gap_details.get(field_name)
                        else "- (нет)",
                    )
                    for field_name, label in repo_gap_labels.items()
                ]
                repo_gap_txt = "\n\n".join(f"{label}:\n{items}" for label, items in repo_gap_sections)
                repo_evidence_txt = _build_repo_evidence_text()
                external_reference_entries = _collect_external_reference_entries(
                    user_query=user_query,
                    mapping_text=text,
                )
                user_parts = [
                    f"Исходный запрос пользователя:\n{user_query}\n\n",
                    f"Обязательные разделы:\n{req}\n\n",
                    f"Repo evidence из выполненных шагов:\n{repo_evidence_txt}\n\n",
                ]
                if external_reference_entries:
                    user_parts.append(
                        "Внешние референсы и implementation guidance:\n"
                        + _render_external_references_block(external_reference_entries)
                        + "\n\n"
                    )
                if raw_user_clarification_answers:
                    user_parts.append(
                        "Полученные уточнения пользователя:\n"
                        + "\n".join(f"- {answer}" for answer in raw_user_clarification_answers)
                        + "\n\n"
                    )
                if claim_ledger_path:
                    user_parts.append(f"Claim ledger artifact:\n{claim_ledger_path}\n\n")
                if fact_pack_path:
                    user_parts.append(f"Fact pack artifact:\n{fact_pack_path}\n\n")
                if draft_path:
                    user_parts.append(f"Draft artifact:\n{draft_path}\n\n")
                if polished_path:
                    user_parts.append(f"Polished artifact:\n{polished_path}\n\n")
                if open_gaps_path:
                    user_parts.append(f"Open gaps artifact:\n{open_gaps_path}\n\n")
                user_parts.extend(
                    [
                        f"Найденные проблемы:\n{issues_txt}\n\n",
                        f"Отсутствующие/слабые разделы:\n{miss_txt}\n\n",
                        f"Незакрытые обязательные входы задачи:\n{required_inputs_gap_txt}\n\n",
                        f"Слабые разделы:\n{weak_txt}\n\n",
                        f"Недобор по количественным требованиям:\n{counts_txt}\n\n",
                        f"Пробелы трассируемости:\n{trace_txt}\n\n",
                        f"{repo_gap_txt}\n\n",
                        f"Текущая версия ТЗ:\n{text}",
                    ]
                )
                user = "".join(user_parts)
                structured_rework = _supports_strict_chat_json_contract(self._deps.chat_completion)
                for attempt in (1, 2):
                    prompt_system = system
                    if attempt == 2:
                        prompt_system += (
                            "\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ. "
                            "Верни только JSON-объект вида {\"final_text\":\"...\"}."
                        )
                    if structured_rework:
                        raw = await self._deps.chat_completion(
                            self._config,
                            prompt_system,
                            user,
                            response_format={"type": "json_object"},
                        )
                    else:
                        raw = await self._deps.chat_completion(self._config, prompt_system, user)
                    if not raw:
                        self._log.warning("final rework empty response attempt=%d/2", attempt)
                        continue
                    try:
                        payload = loads_safe(raw, strict_first=False)
                    except Exception as e:
                        if not structured_rework:
                            fallback_text = str(raw or "").strip()
                            if (
                                fallback_text
                                and not _contains_internal_runtime_markup(fallback_text)
                                and not _looks_like_nonfinal_rework_text(fallback_text)
                            ):
                                accepted_text, _ = _accept_reworked_candidate(fallback_text, attempt=attempt)
                                if accepted_text:
                                    return accepted_text
                        self._log.warning("final rework parse failed attempt=%d/2: %s", attempt, e)
                        continue
                    if not isinstance(payload, dict):
                        if not structured_rework:
                            fallback_text = str(raw or "").strip()
                            if (
                                fallback_text
                                and not _contains_internal_runtime_markup(fallback_text)
                                and not _looks_like_nonfinal_rework_text(fallback_text)
                            ):
                                accepted_text, _ = _accept_reworked_candidate(fallback_text, attempt=attempt)
                                if accepted_text:
                                    return accepted_text
                        self._log.warning("final rework payload is not an object attempt=%d/2", attempt)
                        continue
                    final_text = str(payload.get("final_text") or "").strip()
                    if _contains_internal_runtime_markup(final_text):
                        self._log.warning("final rework returned internal runtime markup attempt=%d/2", attempt)
                        continue
                    if final_text:
                        accepted_text, _ = _accept_reworked_candidate(final_text, attempt=attempt)
                        if accepted_text:
                            return accepted_text
                        continue
                    self._log.warning("final rework payload missing final_text attempt=%d/2", attempt)
                return ""

            if (
                repo_grounded_required
                and hasattr(session, "run_prompt")
                and mandatory_bundle_ready
                and not bool(initial_assessment.get("assessment_error"))
                and (
                    bool(initial_assessment.get("needs_rework"))
                    or bool(initial_assessment.get("review_forced_rework"))
                )
            ):
                _prepare_cli_stage_bundle(initial_assessment, stage_id="gap_closure_bundle")
                revised = await _rework(
                    current,
                    initial_assessment.get("issues") or [],
                    initial_assessment.get("missing_sections") or [],
                    initial_assessment.get("required_input_gaps") or [],
                    initial_assessment.get("weak_sections") or [],
                    initial_assessment.get("missing_counts") or [],
                    initial_assessment.get("traceability_gaps") or [],
                    {field_name: initial_assessment.get(field_name) or [] for field_name in repo_gap_labels},
                    stage_assessment=initial_assessment,
                )
                if revised:
                    current = revised
                    if remaining_quality_passes > 0:
                        remaining_quality_passes -= 1
                    if initial_assessment.get("required_input_gaps") and not analyst_runtime_context:
                        current = _apply_required_input_assumptions_section(
                            revised,
                            user_query=user_query,
                            required_input_gaps=initial_assessment.get("required_input_gaps") or [],
                        )
                    spec_fix_entry = _build_spec_fix_supplemental_entry()
                    supplemental_claim_entries = [spec_fix_entry] if isinstance(spec_fix_entry, dict) else []
                    if supplemental_claim_entries:
                        _refresh_supporting_runtime_artifacts(supplemental_entries=supplemental_claim_entries)
                    followup_review_result = await _run_followup_repo_final_review(
                        draft_text=current,
                        stage_assessment=initial_assessment,
                    )
                    followup_repo_review_outputs = list(followup_review_result.get("outputs") or [])
                    followup_entry = followup_review_result.get("entry")
                    supplemental_claim_entries = [
                        entry
                        for entry in (spec_fix_entry, followup_entry)
                        if isinstance(entry, dict)
                    ]
                    if supplemental_claim_entries:
                        _refresh_supporting_runtime_artifacts(supplemental_entries=supplemental_claim_entries)
                    initial_assessment = await _assess(current)
                    if remaining_quality_passes <= 0:
                        self._log.info(
                            "final qc initial repo rework consumed qc budget session=%s",
                            getattr(session, "id", ""),
                        )
                elif repo_rework_exhausted:
                    skip_quality_loop = True
                    self._log.warning(
                        "final qc initial repo rework exhausted; skipping repeated qc loop session=%s",
                        getattr(session, "id", ""),
                    )

            final_assessment = initial_assessment
            if not skip_quality_loop and remaining_quality_passes > 0:
                for i in range(1, remaining_quality_passes + 1):
                    assessment = await _assess(current)
                    final_assessment = assessment
                    if bool(assessment.get("assessment_error")):
                        self._log.warning("final qc pass %d: assessment degraded, skipping rework loop", i)
                        break
                    if assessment.get("required_input_gaps"):
                        self._log.warning(
                            "final qc pass %d has required input gaps; "
                            "continuing with corrective rework without late ask_user session=%s gaps=%s",
                            i,
                            getattr(session, "id", ""),
                            assessment.get("required_input_gaps") or [],
                        )
                        _emit_orch(
                            "final_qc_required_input_gaps",
                            "partial",
                            "Финальная проверка нашла незакрытые входы; запускаю корректировку без нового вопроса",
                        )
                    if repo_grounded_required:
                        _, obligation_matrix_path = _persist_obligation_matrix(assessment)
                    needs = bool(assessment.get("needs_rework"))
                    if assessment.get("required_input_gaps"):
                        needs = True
                    if i == 1 and bool(assessment.get("review_forced_rework")):
                        needs = True
                    issues = assessment.get("issues") or []
                    missing = assessment.get("missing_sections") or []
                    weak_sections = assessment.get("weak_sections") or []
                    missing_counts = assessment.get("missing_counts") or []
                    traceability_gaps = assessment.get("traceability_gaps") or []
                    repo_gap_details = {
                        field_name: assessment.get(field_name) or []
                        for field_name in repo_gap_labels
                    }
                    if not needs:
                        self._log.info("final qc pass %d: no rework needed", i)
                        break
                    if repo_grounded_required:
                        _prepare_cli_stage_bundle(assessment, stage_id="gap_closure_bundle")
                    self._log.warning(
                        "final qc pass %d: rework required, issues=%d missing=%d weak=%d counts=%d trace=%d repo_gaps=%d",
                        i,
                        len(issues),
                        len(missing),
                        len(weak_sections),
                        len(missing_counts),
                        len(traceability_gaps),
                        sum(len(items) for items in repo_gap_details.values()),
                    )
                    revised = await _rework(
                        current,
                        issues,
                        missing,
                        assessment.get("required_input_gaps") or [],
                        weak_sections,
                        missing_counts,
                        traceability_gaps,
                        repo_gap_details,
                        stage_assessment=assessment,
                    )
                    if not revised:
                        self._log.warning("final qc pass %d: rework returned empty, keeping original", i)
                        if repo_rework_exhausted:
                            final_assessment = assessment
                            self._log.warning(
                                "final qc pass %d: repo rework exhausted, skipping reassessment session=%s",
                                i,
                                getattr(session, "id", ""),
                            )
                        else:
                            final_assessment = await _assess(current)
                        break
                    current = revised
                    if assessment.get("required_input_gaps") and not analyst_runtime_context:
                        current = _apply_required_input_assumptions_section(
                            revised,
                            user_query=user_query,
                            required_input_gaps=assessment.get("required_input_gaps") or [],
                        )
                    if repo_grounded_required and mandatory_bundle_ready:
                        spec_fix_entry = _build_spec_fix_supplemental_entry()
                        supplemental_claim_entries = [spec_fix_entry] if isinstance(spec_fix_entry, dict) else []
                        if supplemental_claim_entries:
                            _refresh_supporting_runtime_artifacts(supplemental_entries=supplemental_claim_entries)
                        followup_review_result = await _run_followup_repo_final_review(
                            draft_text=current,
                            stage_assessment=assessment,
                        )
                        followup_repo_review_outputs = list(followup_review_result.get("outputs") or [])
                        followup_entry = followup_review_result.get("entry")
                        supplemental_claim_entries = [
                            entry
                            for entry in (spec_fix_entry, followup_entry)
                            if isinstance(entry, dict)
                        ]
                        if supplemental_claim_entries:
                            _refresh_supporting_runtime_artifacts(supplemental_entries=supplemental_claim_entries)
                        if followup_repo_review_outputs:
                            self._log.info(
                                "follow-up final repo review applied session=%s outputs=%d",
                                getattr(session, "id", ""),
                                len(followup_repo_review_outputs),
                            )
                            if i == remaining_quality_passes:
                                final_assessment = await _assess(current)
            if final_assessment.get("required_input_gaps"):
                self._log.warning(
                    "final assessment still has required input gaps; "
                    "delivering current final text without late ask_user session=%s gaps=%s",
                    getattr(session, "id", ""),
                    final_assessment.get("required_input_gaps") or [],
                )
                _emit_orch(
                    "final_qc_required_input_gaps",
                    "partial",
                    "Финальная проверка оставила незакрытые входы; выдаю текущую версию ТЗ без нового вопроса",
                )
                if not strict_analyst_runtime_context:
                    current = _apply_required_input_assumptions_section(
                        current,
                        user_query=user_query,
                        required_input_gaps=final_assessment.get("required_input_gaps") or [],
                    )
            last_final_assessment = final_assessment
            last_model_text_before_runtime = current
            return apply_runtime_readiness(
                current,
                final_assessment,
                repo_grounded_required=repo_grounded_required,
                required_step_statuses=_required_repo_step_statuses(),
                repo_gap_labels=repo_gap_labels,
            )

        def _seed_completed_sets(steps: List[Any]) -> tuple[set[str], set[str]]:
            """
            Seed prior terminal-success vs prior non-success state for the current plan.

            Only terminal-success steps are treated as already completed on replan.
            Historical non-success steps are preserved separately so same conceptual
            steps may be retried if planner returns them again.
            """
            ok: set[str] = set()
            non_success: set[str] = set()
            for s in steps:
                prev = prior_step_results.get(s.id)
                if not prev:
                    continue
                st = str(prev.get("status") or "")
                if self._is_terminal_success_status(st):
                    ok.add(s.id)
                else:
                    non_success.add(s.id)
            return ok, non_success

        def _progress_context() -> str:
            # Keep the context bounded: include only the last N step summaries.
            tail = step_results[-25:]
            try:
                payload = json.dumps(tail, ensure_ascii=False)
            except Exception as e:
                self._log.warning("failed to serialize progress context: %s", e)
                payload = ""
            if not payload:
                return ""
            return f"\nstep_results_so_far:\n{payload}"

        while True:
            self._log.info("--- planning (attempt %d) ---", replan_count + 1)
            _emit_orch(
                "planning_start",
                "running",
                f"Планирование: попытка {replan_count + 1}",
                iteration=replan_count + 1,
            )
            nonask_steps_since_plan = 0
            planning_text = user_text
            if clarification_limit_reached and allow_continue_without_clarifications:
                planning_text = (
                    f"{planning_text}\n\n"
                    "Служебное ограничение: лимит уточняющих вопросов пользователю исчерпан. "
                    "Не добавляй ask_user и продолжай с явными допущениями."
                )
            # Provide the planner with stable id hints to reduce id churn across replans.
            prior_hints: str = ""
            if prior_step_results:
                try:
                    items = []
                    for sid, r in list(prior_step_results.items())[-50:]:
                        items.append(
                            {
                                "id": sid,
                                "title": str((r or {}).get("title") or ""),
                                "step_type": str((r or {}).get("step_type") or ""),
                                "status": str((r or {}).get("status") or ""),
                            }
                        )
                    prior_hints = "\nprior_steps:\n" + json.dumps(items, ensure_ascii=False)
                except Exception as e:
                    self._log.warning("failed to serialize prior step hints: %s", e)
                    prior_hints = ""

            steps = await self._deps.plan_steps(
                self._config,
                planning_text,
                ctx_summary + _progress_context() + prior_hints,
            )
            steps = self._order_steps_safely(steps)
            steps = _stabilize_step_ids(steps)
            for planned_step in steps:
                planned_step_id = str(getattr(planned_step, "id", "") or "").strip()
                if planned_step_id:
                    known_step_defs_by_id[planned_step_id] = planned_step
            step_title_by_id = {s.id: s.title for s in steps if s.id}
            self._log.info("plan ready: %d step(s) -> %s",
                           len(steps),
                           ", ".join(f"{s.id}({s.step_type})" for s in steps))
            _emit_orch(
                "plan_ready",
                "running",
                f"План готов: {len(steps)} шаг(ов)",
                iteration=replan_count + 1,
            )
            restart = False
            # Dynamic graph execution:
            # - respects depends_on
            # - only executes dependents if their deps succeeded (ok/partial)
            # - parallelizes only explicitly-marked steps
            completed_ok, historical_non_success = _seed_completed_sets(steps)
            completed_fail: set[str] = set()

            while True:
                batch, skipped = self._next_batch(
                    steps,
                    completed_ok,
                    completed_fail,
                    session_id=session.id,
                    historical_success={
                        sid
                        for sid, result in prior_step_results.items()
                        if self._is_terminal_success_status(str((result or {}).get("status") or ""))
                    },
                    historical_non_success=historical_non_success,
                )
                if skipped:
                    self._log.info("skipped %d step(s): %s", len(skipped),
                                   ", ".join(f"{r.task_id}({r.status})" for r in skipped))
                for r in skipped:
                    entry = {
                        "task_id": r.task_id,
                        "status": r.status,
                        "summary": r.summary,
                        "title": step_title_by_id.get(r.task_id),
                        "outputs": _compact_outputs(
                            r.outputs or [],
                            task_id=str(r.task_id or ""),
                            step_type=str(getattr(next((x for x in steps if x.id == r.task_id), None), "step_type", "") or "task"),
                        ),
                        "claims": list(getattr(r, "claims", []) or []),
                        "tool_calls": r.tool_calls,
                    }
                    _record_step_result(entry)
                if not batch:
                    self._log.info("no more steps to execute, finishing")
                    _emit_orch("plan_drained", "running", "Шагов для выполнения не осталось")
                    break

                self._log.info("executing batch: %s (parallel=%s)",
                               ", ".join(s.id for s in batch), len(batch) > 1)
                _emit_orch(
                    "batch_start",
                    "running",
                    f"Запуск batch: {', '.join(s.id for s in batch)}",
                )
                if len(batch) == 1:
                    step = batch[0]
                    if (
                        clarification_limit_reached
                        and allow_continue_without_clarifications
                        and step.step_type == "ask_user"
                    ):
                        resp = self._deps.ExecutorResponse(
                            task_id=step.id,
                            status="partial",
                            summary=(
                                "ℹ️ Уточняющий вопрос пропущен: лимит вопросов исчерпан, "
                                "продолжаю с допущениями."
                            ),
                            outputs=[
                                {
                                    "type": "text",
                                    "content": (
                                        "Лимит уточнений исчерпан. "
                                        "Шаг ask_user пропущен, работа продолжается."
                                    ),
                                }
                            ],
                            tool_calls=[{"tool": "ask_user", "status": "skipped_due_to_limit"}],
                            next_questions=[],
                        )
                    else:
                        await _prepare_final_repo_review_step(step, plan_steps_local=steps)
                        resp = await self._execute_step(
                            step,
                            session,
                            bot,
                            context,
                            dest,
                            ctx_summary,
                            current_user_text=user_text,
                            constraints=system_prompt_addition,
                        )
                    self._apply_step_result(step, resp, completed_ok, completed_fail)
                    _record_structured_use_cli_step_metrics(step, resp)
                    entry = {
                        "task_id": resp.task_id,
                        "status": resp.status,
                        "summary": resp.summary,
                        "title": step_title_by_id.get(resp.task_id),
                        "step_type": step.step_type,
                        "ask_question": step.ask_question,
                        "ask_options": list(step.ask_options or []),
                        "outputs": _compact_outputs(
                            resp.outputs or [],
                            task_id=str(resp.task_id or ""),
                            step_type=str(step.step_type or ""),
                        ),
                        "claims": list(getattr(resp, "claims", []) or []),
                        "tool_calls": resp.tool_calls,
                    }
                    _record_step_result(entry)

                    if getattr(resp, "status", "") == "needs_input":
                        pause_message = _resolve_needs_input_message(resp)
                        self._log.info("step %s requires user input; pausing orchestrator without finalization", step.id)
                        self._log.debug("trace: %s", build_trace_event(
                            "awaiting_input", mode_id=mode_id, session_id=session.id,
                            step_id=str(step.id), status="needs_input",
                        ))
                        _emit_orch(
                            "awaiting_input",
                            "needs_input",
                            pause_message,
                        )
                        return pause_message

                    if step.step_type == "ask_user" and resp.status == "ok":
                        answer = ""
                        if resp.outputs:
                            answer = str(resp.outputs[0].get("content") or "")
                        if answer:
                            setattr(session, _ANALYST_BLOCKING_CLARIFICATION_TEXT_ATTR, "")
                            selected, explicit_selection = _extract_ask_user_selected(answer)
                            if _is_analyst_runtime_context(session) and not explicit_selection:
                                pause_message = _resolve_needs_input_message()
                                self._log.warning(
                                    "analyst ask_user returned opaque success without explicit user selection; "
                                    "pausing instead of replanning step=%s summary=%r answer=%r",
                                    step.id,
                                    str(getattr(resp, "summary", "") or "")[:200],
                                    answer[:200],
                                )
                                _emit_orch(
                                    "awaiting_input",
                                    "needs_input",
                                    pause_message,
                                )
                                return pause_message
                            if self._requires_blocking_clarification(session):
                                self._log.info(
                                    "blocking clarification answer received, continuing with automatic replan: %r",
                                    selected[:200],
                                )
                            self._log.info("ask_user answer received, will replan: %r", selected[:200])
                            if is_non_semantic_ask_answer(selected):
                                self._log.info(
                                    "ask_user returned control answer, not persisting as clarification: %r",
                                    selected[:200],
                                )
                            else:
                                user_text = f"{user_text}\nОтвет пользователя: {selected}"
                                _refresh_raw_user_clarification_answers()
                            self._persist_recovery_input_bundle(
                                session,
                                clarification_answers=self._extract_clarification_answers(user_text),
                            )
                            replan_count += 1
                            if replan_count > self._max_clarifications:
                                if allow_continue_without_clarifications:
                                    clarification_limit_reached = True
                                    if not clarification_notice_added:
                                        user_text = (
                                            f"{user_text}\n"
                                            "Служебная пометка: лимит уточнений исчерпан, "
                                            "дальше продолжаем с допущениями без новых вопросов."
                                        )
                                        clarification_notice_added = True
                                    self._log.warning(
                                        "too many clarifications (%d), continue without ask_user",
                                        replan_count,
                                    )
                                    _emit_orch(
                                        "clarification_limit",
                                        "partial",
                                        f"Лимит уточнений достигнут ({replan_count}), продолжаем с допущениями",
                                        iteration=replan_count,
                                    )
                                    restart = True
                                    break
                                self._log.warning("too many clarifications (%d), stopping", replan_count)
                                _emit_orch(
                                    "clarification_limit",
                                    "error",
                                    f"Слишком много уточнений ({replan_count}), остановка",
                                    iteration=replan_count,
                                )
                                return "⚠️ Слишком много уточнений. Остановлено."
                            restart = True
                            _emit_orch(
                                "replan",
                                "running",
                                "Запрошен replan после ответа ask_user",
                                iteration=replan_count,
                            )
                            break

                    # Replan on failure, or when LLM says new facts materially change the remaining plan.
                    if getattr(resp, "status", "") in ("error", "blocked"):
                        should_retry = await self._should_retry_via_reactions(
                            session_id=session.id,
                            step_id=str(getattr(resp, "task_id", "") or getattr(step, "id", "")),
                            step_status=str(getattr(resp, "status", "") or ""),
                            summary=str(getattr(resp, "summary", "") or ""),
                            replan_count=replan_count,
                        )
                        if should_retry:
                            replan_count += 1
                            _emit_orch(
                                "replan",
                                "running",
                                "Replan по реакции после failed/blocked шага",
                                iteration=replan_count,
                            )
                            restart = True
                            break
                        self._log.warning("reactions v2 declined retry replan_count=%d", replan_count)
                    else:
                        needs_replan, reason = await _should_replan_after_success(
                            step=step,
                            resp=resp,
                            steps_local=steps,
                            completed_ok_local=completed_ok,
                            completed_fail_local=completed_fail,
                        )
                        if needs_replan:
                            replan_count += 1
                            if replan_count > self._MAX_REPLANS:
                                self._log.warning("too many replans (%d), stopping replanning", replan_count)
                            else:
                                self._log.info("replan requested after success: %s", reason or "(no reason)")
                                _emit_orch(
                                    "replan",
                                    "running",
                                    f"Replan после успешного шага: {reason or '(no reason)'}",
                                    iteration=replan_count,
                                )
                                restart = True
                                break
                        # Periodic forced replan: every N non-ask steps, if there is remaining work.
                        if step.step_type != "ask_user":
                            nonask_steps_since_plan += 1
                            if nonask_steps_since_plan >= self._FORCE_REPLAN_EVERY_N_NONASK_STEPS:
                                remaining2 = [s for s in steps if s.id not in completed_ok and s.id not in completed_fail]
                                if remaining2:
                                    replan_count += 1
                                    if replan_count > self._MAX_REPLANS:
                                        self._log.warning("too many replans (%d), stopping replanning", replan_count)
                                    else:
                                        self._log.info("periodic replan after %d steps", nonask_steps_since_plan)
                                        _emit_orch(
                                            "replan",
                                            "running",
                                            f"Периодический replan после {nonask_steps_since_plan} шагов",
                                            iteration=replan_count,
                                        )
                                        restart = True
                                        break
                                nonask_steps_since_plan = 0
                    continue

                async def _run_one(s: Any):
                    try:
                        await _prepare_final_repo_review_step(s, plan_steps_local=steps)
                        return await self._execute_step(
                            s,
                            session,
                            bot,
                            context,
                            dest,
                            ctx_summary,
                            current_user_text=user_text,
                            constraints=system_prompt_addition,
                        )
                    except Exception as e:
                        return self._deps.ExecutorResponse(
                            task_id=s.id,
                            status="error",
                            summary=f"Ошибка шага {s.id}: {e}",
                            outputs=[],
                            tool_calls=[{"tool": "step", "error": str(e), "corr_id": f"{session.id}:{s.id}"}],
                            next_questions=[],
                        )

                group_results = await asyncio.gather(*[_run_one(s) for s in batch], return_exceptions=False)
                for s, r in zip(batch, group_results):
                    self._apply_step_result(s, r, completed_ok, completed_fail)
                    _record_structured_use_cli_step_metrics(s, r)
                    self._log.info("parallel step %s finished: status=%s", s.id, getattr(r, "status", "?"))
                for r in group_results:
                    entry = {
                        "task_id": r.task_id,
                        "status": r.status,
                        "summary": r.summary,
                        "title": step_title_by_id.get(r.task_id),
                        "step_type": str(getattr(next((x for x in batch if x.id == r.task_id), None), "step_type", "") or "task"),
                        "ask_question": getattr(next((x for x in batch if x.id == r.task_id), None), "ask_question", None),
                        "ask_options": list(
                            getattr(next((x for x in batch if x.id == r.task_id), None), "ask_options", None) or []
                        ),
                        "outputs": _compact_outputs(
                            r.outputs or [],
                            task_id=str(r.task_id or ""),
                            step_type=str(
                                getattr(next((x for x in batch if x.id == r.task_id), None), "step_type", "") or "task"
                            ),
                        ),
                        "claims": list(getattr(r, "claims", []) or []),
                        "tool_calls": r.tool_calls,
                    }
                    _record_step_result(entry)

                if any(getattr(r, "status", "") == "needs_input" for r in group_results):
                    pause_message = _resolve_needs_input_message(
                        next((r for r in group_results if getattr(r, "status", "") == "needs_input"), None)
                    )
                    self._log.info("parallel batch requires user input; pausing orchestrator without finalization")
                    self._log.debug("trace: %s", build_trace_event(
                        "awaiting_input", mode_id=mode_id, session_id=session.id,
                        status="needs_input",
                    ))
                    _emit_orch(
                        "awaiting_input",
                        "needs_input",
                        pause_message,
                    )
                    return pause_message

                # Replan only when needed (any error/blocked in the batch).
                if any(getattr(r, "status", "") in ("error", "blocked") for r in group_results):
                    first_failed = next(
                        (r for r in group_results if getattr(r, "status", "") in ("error", "blocked")),
                        None,
                    )
                    should_retry = await self._should_retry_via_reactions(
                        session_id=session.id,
                        step_id=str(getattr(first_failed, "task_id", "") or ""),
                        step_status=str(getattr(first_failed, "status", "") or ""),
                        summary=str(getattr(first_failed, "summary", "") or ""),
                        replan_count=replan_count,
                    )
                    if should_retry:
                        replan_count += 1
                        _emit_orch(
                            "replan",
                            "running",
                            "Replan после ошибок в parallel batch",
                            iteration=replan_count,
                        )
                        restart = True
                        break
                    self._log.warning("reactions v2 declined retry replan_count=%d", replan_count)
                else:
                    # If any successful step indicates a major plan pivot, replan.
                    for s, r in zip(batch, group_results):
                        needs_replan, reason = await _should_replan_after_success(
                            step=s,
                            resp=r,
                            steps_local=steps,
                            completed_ok_local=completed_ok,
                            completed_fail_local=completed_fail,
                        )
                        if needs_replan:
                            replan_count += 1
                            if replan_count > self._MAX_REPLANS:
                                self._log.warning("too many replans (%d), stopping replanning", replan_count)
                            else:
                                self._log.info("replan requested after parallel success: %s", reason or "(no reason)")
                                _emit_orch(
                                    "replan",
                                    "running",
                                    f"Replan после parallel success: {reason or '(no reason)'}",
                                    iteration=replan_count,
                                )
                                restart = True
                            break
                    if not restart:
                        # Periodic forced replan based on number of non-ask steps completed.
                        nonask_done = sum(1 for s in batch if str(getattr(s, "step_type", "") or "task") != "ask_user")
                        if nonask_done:
                            nonask_steps_since_plan += nonask_done
                            if nonask_steps_since_plan >= self._FORCE_REPLAN_EVERY_N_NONASK_STEPS:
                                remaining2 = [s for s in steps if s.id not in completed_ok and s.id not in completed_fail]
                                if remaining2:
                                    replan_count += 1
                                    if replan_count > self._MAX_REPLANS:
                                        self._log.warning("too many replans (%d), stopping replanning", replan_count)
                                    else:
                                        self._log.info("periodic replan after %d steps", nonask_steps_since_plan)
                                        _emit_orch(
                                            "replan",
                                            "running",
                                            f"Периодический replan после {nonask_steps_since_plan} шагов",
                                            iteration=replan_count,
                                        )
                                        restart = True
                                nonask_steps_since_plan = 0
                            if restart:
                                break

            if restart:
                continue

            if self._requires_blocking_clarification(session) and not self._extract_clarification_answers(user_text):
                pause_message = _resolve_needs_input_message()
                self._log.info(
                    "finalization paused: blocking clarification still unresolved session=%s",
                    getattr(session, "id", ""),
                )
                _emit_orch(
                    "awaiting_input",
                    "needs_input",
                    pause_message,
                )
                return pause_message

            missing_required_repo_steps = self._missing_required_repo_use_cli_step_ids(
                session,
                completed_ok,
                steps=steps,
                historical_steps=known_step_defs_by_id,
                historical_completed_ok={
                    sid
                    for sid, result in prior_step_results.items()
                    if self._is_terminal_success_status(str((result or {}).get("status") or ""))
                },
                template=template,
            )
            if missing_required_repo_steps:
                missing_steps_text = ", ".join(missing_required_repo_steps)
                if not repo_steps_recovery_attempted:
                    repo_steps_recovery_attempted = True
                    self._log.info(
                        "finalization: missing repo steps %s, triggering replan to inject them session=%s",
                        missing_steps_text,
                        session.id,
                    )
                    _emit_orch(
                        "repo_steps_recovery",
                        "running",
                        f"Инжектирую недостающие repo-grounded шаги: {missing_steps_text}",
                    )
                    continue
                self._log.warning(
                    "finalization: missing required repo use_cli steps after recovery session=%s missing=%s; "
                    "продолжаем финализацию, чтобы выдать пользователю итоговый документ",
                    session.id,
                    missing_steps_text,
                )
                _emit_orch(
                    "repo_steps_missing",
                    "partial",
                    (
                        "Финализация продолжается без обязательных repo-grounded шагов: "
                        f"{missing_steps_text}"
                    ),
                )

            try:
                final_response = await _send_final_answer(
                    user_query=raw_user_query,
                    plan_steps_local=steps,
                    step_results_local=step_results,
                )
            except _RestartPlanningAfterClarification:
                continue
            except _AwaitingUserInput as awaiting_input:
                _persist_analyst_quality_metrics(
                    claim_ledger_local=_build_claim_ledger(
                        step_results,
                        supplemental_entries=supplemental_claim_entries,
                    ),
                    assessment_local=last_final_assessment,
                    required_step_statuses={
                        step_id: str((prior_step_results.get(step_id) or {}).get("status") or "")
                        for step_id in self._required_repo_use_cli_step_ids(session, template)
                    },
                    model_text_before_runtime=last_model_text_before_runtime,
                )
                return awaiting_input.message
            _persist_analyst_quality_metrics(
                claim_ledger_local=_build_claim_ledger(
                    step_results,
                    supplemental_entries=supplemental_claim_entries,
                ),
                assessment_local=last_final_assessment,
                required_step_statuses={
                    step_id: str((prior_step_results.get(step_id) or {}).get("status") or "")
                    for step_id in self._required_repo_use_cli_step_ids(session, template)
                },
                model_text_before_runtime=last_model_text_before_runtime,
            )
            self._log.info("=== orchestrator run END session=%s ok=%d fail=%d response_len=%d ===",
                           session.id, len(completed_ok), len(completed_fail), len(final_response))
            _emit_orch(
                "final",
                "ok" if not completed_fail else "partial",
                f"Orchestrator завершен: ok={len(completed_ok)} fail={len(completed_fail)}",
            )
            try:
                date_str = time.strftime("%Y-%m-%d")
                entry = {
                    "date": date_str,
                    "user": user_text,
                    "context": ctx_summary,
                    "steps": [dataclasses.asdict(step) for step in steps],
                    "results": results,
                    "step_results": step_results,
                    "final": final_response,
                }
                path = os.path.join(cwd, "SESSION.json")

                def _append(current: Dict[str, Any]) -> Dict[str, Any]:
                    current.setdefault("orchestrator_by_task", {})
                    current["orchestrator_by_task"].setdefault(task_key, []).append(entry)
                    # Не держим лишнее в памяти — только последние N записей.
                    max_items = 50
                    items = current["orchestrator_by_task"][task_key]
                    while len(items) > max_items:
                        items.pop(0)
                    return current

                self._deps.update_json_locked(path, _append, default={"orchestrator_by_task": {}})
            except Exception as e:
                self._log.warning("failed to append orchestrator session log path=%s err=%s", path, e)
            if strict_analyst_runtime_context:
                self._log.info(
                    "memory: skip update for analyst runtime session=%s",
                    session.id,
                )
            else:
                await self._maybe_update_memory(user_text, final_response, memory_text, cwd)
            return final_response

    def _order_steps_safely(self, steps: List[Any]) -> List[Any]:
        """
        Ensure plan has consistent ids and sane dependency references.
        We keep user-visible order stable; actual execution order is handled by _iter_batches().
        """
        for s in steps:
            deps = []
            seen: set[str] = set()
            for d in (s.depends_on or []):
                dep_id = str(d or "").strip()
                if not dep_id or dep_id == s.id or dep_id in seen:
                    continue
                deps.append(dep_id)
                seen.add(dep_id)
            s.depends_on = deps
        return steps

    def _is_terminal_success_status(self, status: str) -> bool:
        return status in ("ok", "partial")

    def _is_success_status(self, status: str) -> bool:
        return self._is_terminal_success_status(status)

    def _apply_step_result(
        self,
        step: Any,
        resp: Any,
        completed_ok: set[str],
        completed_fail: set[str],
    ) -> None:
        if self._is_terminal_success_status(getattr(resp, "status", "error")):
            completed_ok.add(step.id)
            completed_fail.discard(step.id)
        else:
            completed_fail.add(step.id)
            completed_ok.discard(step.id)

    def _next_batch(
        self,
        steps: List[Any],
        completed_ok: set[str],
        completed_fail: set[str],
        session_id: str,
        historical_success: Optional[set[str]] = None,
        historical_non_success: Optional[set[str]] = None,
    ) -> tuple[List[Any], List[Any]]:
        """
        Pick next executable batch based on dependency success.

        `historical_success` tracks latest successful steps from previous replans and allows
        dependencies on omitted already-completed steps to remain satisfied.
        `completed_fail` tracks only non-success states from the current plan iteration.
        `historical_non_success` tracks latest non-success states from previous replans and
        is used only to block dependents whose failed prerequisite was not restored.
        Returns [] when no runnable steps remain.
        """
        historical_success = historical_success or set()
        historical_non_success = historical_non_success or set()
        remaining = [s for s in steps if s.id not in completed_ok and s.id not in completed_fail]
        if not remaining:
            return [], []

        ids = {s.id for s in steps}
        # Compute ready steps: deps must be completed successfully.
        ready: List[Any] = []
        blocked: List[Any] = []
        for s in remaining:
            deps = [str(d) for d in (s.depends_on or []) if d and d != s.id]
            failed_deps = [d for d in deps if d in completed_fail]
            missing_deps = [d for d in deps if d not in ids and d not in completed_ok and d not in historical_success]
            if failed_deps or missing_deps:
                blocked.append(s)
                continue
            if all(d in completed_ok or (d not in ids and d in historical_success) for d in deps):
                ready.append(s)

        # Mark blocked steps as failed (dependency failed) to avoid infinite loop.
        skipped_responses: List[Any] = []
        for s in blocked:
            completed_fail.add(s.id)
            deps = [str(d) for d in (s.depends_on or []) if d and d != s.id]
            failed_deps = [d for d in deps if d in completed_fail]
            missing_deps = [d for d in deps if d not in ids and d not in completed_ok and d not in historical_success]
            unresolved_deps = failed_deps + [d for d in missing_deps if d not in failed_deps]
            error_code = "dependency_failed" if failed_deps else "dependency_missing_in_plan"
            corr_id = f"{session_id}:{s.id}"
            resp = self._deps.ExecutorResponse(
                task_id=s.id,
                status="blocked",
                summary=(
                    f"⛔ Шаг {s.id} пропущен: зависимость не выполнена "
                    f"({', '.join(unresolved_deps) or 'unknown'})."
                ),
                outputs=[],
                tool_calls=[
                    {
                        "tool": "orchestrator",
                        "error": error_code,
                        "corr_id": corr_id,
                        "depends_on": unresolved_deps,
                        "missing_dependencies": missing_deps,
                        "historical_dependencies": [d for d in missing_deps if d in historical_non_success],
                    }
                ],
                next_questions=[],
            )
            self._deps.validate_response(resp)
            skipped_responses.append(resp)

        if not ready:
            # Cyclic/unsatisfied dependencies: mark remaining as blocked to avoid silent drops.
            for s in remaining:
                if s.id in completed_ok or s.id in completed_fail:
                    continue
                deps = [str(d) for d in (s.depends_on or []) if d and d != s.id]
                corr_id = f"{session_id}:{s.id}"
                resp = self._deps.ExecutorResponse(
                    task_id=s.id,
                    status="blocked",
                    summary=f"⛔ Шаг {s.id} пропущен: не удалось удовлетворить зависимости (возможен цикл): {', '.join(deps) or 'none'}.",
                    outputs=[],
                    tool_calls=[{"tool": "orchestrator", "error": "unsatisfied_dependencies", "corr_id": corr_id, "depends_on": deps}],
                    next_questions=[],
                )
                self._deps.validate_response(resp)
                skipped_responses.append(resp)
                completed_fail.add(s.id)
            return [], skipped_responses

        # Validate parallelizable: require reason and be conservative for file-mutating instructions.
        for s in ready:
            if s.parallelizable:
                reason = (s.parallelizable_reason or "").strip()
                if not reason:
                    s.parallelizable = False
                    continue
                instr = (s.instruction or "").lower()
                risky_keys = ["write_file", "edit_file", "delete_file", "send_file", "git", "commit", "push", "merge", "rebase"]
                risky = any(k in instr for k in risky_keys)
                if risky and "read" not in reason.lower() and "только чтение" not in reason.lower():
                    s.parallelizable = False

        def _build_non_sequential_info(selected_ids: List[str]) -> List[Any]:
            if not selected_ids:
                return []
            order = [s.id for s in steps]
            remaining_ids = {s.id for s in remaining}
            selected_set = set(selected_ids)
            first_remaining = next((sid for sid in order if sid in remaining_ids), None)
            first_selected = next((sid for sid in order if sid in selected_set), None)
            if not first_remaining or not first_selected or first_remaining == first_selected:
                return []
            if first_remaining in selected_set:
                return []

            idx_selected = order.index(first_selected)
            ids_before_selected = [sid for sid in order[:idx_selected] if sid in remaining_ids and sid not in selected_set]
            if not ids_before_selected:
                return []

            by_id = {s.id: s for s in remaining}
            reasons: List[str] = []
            for sid in ids_before_selected:
                step_obj = by_id.get(sid)
                if not step_obj:
                    continue
                deps = [str(d) for d in (step_obj.depends_on or []) if d and d != sid]
                failed_deps = [d for d in deps if d in completed_fail]
                missing_deps = [d for d in deps if d not in ids and d not in completed_ok]
                pending_deps = [
                    d for d in deps
                    if d in ids and d not in completed_ok and d not in completed_fail
                ]
                if failed_deps:
                    reasons.append(f"{sid}: зависимость завершилась ошибкой ({', '.join(failed_deps)})")
                    continue
                if missing_deps:
                    reasons.append(f"{sid}: зависимость отсутствует в текущем плане ({', '.join(missing_deps)})")
                    continue
                if pending_deps:
                    reasons.append(f"{sid}: ожидает зависимости ({', '.join(pending_deps)})")
                    continue
                reasons.append(f"{sid}: пока не готов к запуску")

            if not reasons:
                reasons.append("причина не определена")
            corr_id = f"{session_id}:order:{first_selected}"
            summary = (
                f"ℹ️ Переход не по порядку: выбран шаг {first_selected}. "
                f"Причины: {'; '.join(reasons)}."
            )
            resp = self._deps.ExecutorResponse(
                task_id=f"order_info:{first_selected}",
                status="partial",
                summary=summary,
                outputs=[],
                tool_calls=[
                    {
                        "tool": "orchestrator",
                        "event": "non_sequential_transition",
                        "corr_id": corr_id,
                        "selected_step": first_selected,
                        "selected_ids": selected_ids,
                        "blocked_before": ids_before_selected,
                        "reasons": reasons,
                    }
                ],
                next_questions=[],
            )
            self._deps.validate_response(resp)
            self._log.info(
                "non-sequential transition detected: selected=%s blocked_before=%s reasons=%s",
                first_selected,
                ",".join(ids_before_selected),
                " | ".join(reasons),
            )
            return [resp]

        # Prefer one parallel group if available, else single step in original order.
        order = [s.id for s in steps]
        groups: Dict[str, List[Any]] = {}
        singles: List[Any] = []
        for s in ready:
            if s.parallel_group and s.parallelizable:
                groups.setdefault(s.parallel_group, []).append(s)
            else:
                singles.append(s)
        if groups:
            for sid in order:
                s = next((x for x in ready if x.id == sid), None)
                if not s:
                    continue
                gid = s.parallel_group
                if gid and gid in groups:
                    selected = groups[gid]
                    info_responses = _build_non_sequential_info([x.id for x in selected])
                    return selected, skipped_responses + info_responses
        # fall back to first single in stable order
        singles_set = {s.id for s in singles}
        for sid in order:
            if sid in singles_set:
                selected = [next(x for x in singles if x.id == sid)]
                info_responses = _build_non_sequential_info([selected[0].id])
                return selected, skipped_responses + info_responses
        selected = [singles[0]]
        info_responses = _build_non_sequential_info([selected[0].id])
        return selected, skipped_responses + info_responses

    async def _execute_step(
        self,
        step: Any,
        session: Any,
        bot: Any,
        context: Any,
        dest: Dict[str, Any],
        orchestrator_context: str,
        *,
        current_user_text: str = "",
        constraints: Optional[str] = None,
    ):
        profile = self._dispatcher.get_profile(step, session)
        corr_id = f"{session.id}:{step.id}"
        mode_id = str(get_active_mode(session, "") or "").strip()
        # Analyst mode: force all non-ask_user steps through CLI
        if profile.name == "analyst" and step.step_type == "task":
            self._log.info("_execute_step: forcing step %s from task->use_cli (analyst profile)", step.id)
            step.step_type = "use_cli"
        if step.step_type == "use_cli":
            return await self._execute_use_cli_step(
                step,
                session,
                bot,
                context,
                dest,
                orchestrator_context,
                current_user_text=current_user_text,
                constraints=constraints,
                profile=profile,
                corr_id=corr_id,
            )
        inputs = {}
        if step.step_type == "ask_user":
            inputs = {"question": step.ask_question, "options": step.ask_options}
        req = self._deps.ExecutorRequest(
            task_id=step.id,
            goal=step.instruction,
            context=orchestrator_context or "",
            allowed_tools=profile.allowed_tools,
            profile=profile.name,
            inputs=inputs,
            corr_id=corr_id,
            # For now, constraints is only used as an extra system block for the agent.
            constraints=constraints,
        )
        _step_trace = build_trace_event(
            "step_started", mode_id=mode_id, session_id=session.id,
            step_id=str(step.id), task_id=str(step.id), corr_id=corr_id,
            status="running",
        )
        self._log.debug("trace: %s", _step_trace)
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "orchestrator",
                "phase": "step_start",
                "status": "running",
                "corr_id": corr_id,
                "task_id": str(getattr(step, "id", "") or ""),
                "step_id": str(getattr(step, "id", "") or ""),
                "message": f"Старт шага {step.id} ({step.step_type})",
            },
        )
        self._log.info("step start corr_id=%s step_type=%s profile=%s allowed_tools=%s",
                       corr_id, step.step_type, profile.name,
                       ",".join(profile.allowed_tools[:5]) + ("..." if len(profile.allowed_tools) > 5 else ""))
        self._log.info("step instruction: %s", (step.instruction or "")[:300])
        self._log.debug("trace: %s", build_trace_event(
            "tool_called", mode_id=mode_id, session_id=session.id,
            step_id=str(step.id), corr_id=corr_id, status="running",
        ))
        resp: Any = await self._executor.run(session, req, bot, context, dest, profile)
        self._log.debug("trace: %s", build_trace_event(
            "tool_finished", mode_id=mode_id, session_id=session.id,
            step_id=str(step.id), corr_id=corr_id,
            status=str(getattr(resp, "status", "") or "ok"),
        ))
        self._log.info("step end corr_id=%s status=%s summary=%s", corr_id,
                       getattr(resp, "status", None), (getattr(resp, "summary", "") or "")[:200])
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "orchestrator",
                "phase": "step_end",
                "status": str(getattr(resp, "status", "") or "ok"),
                "corr_id": corr_id,
                "task_id": str(getattr(step, "id", "") or ""),
                "step_id": str(getattr(step, "id", "") or ""),
                "message": str(getattr(resp, "summary", "") or f"Шаг {step.id} завершен")[:240],
            },
        )
        _step_done_trace = build_trace_event(
            "step_finished", mode_id=mode_id, session_id=session.id,
            step_id=str(step.id), task_id=str(step.id), corr_id=corr_id,
            status=str(getattr(resp, "status", "") or "ok"),
        )
        self._log.debug("trace: %s", _step_done_trace)
        if resp.status == "needs_input" and resp.next_questions:
            # Явный запрос пользователю: первая формулировка
            resp.summary = resp.next_questions[0]
        if resp.status == "needs_input" and not resp.next_questions:
            resp.summary = "Нужно уточнение пользователя, но вопрос не сформирован."
        return resp

    async def _execute_use_cli_step(
        self,
        step: Any,
        session: Any,
        bot: Any,
        context: Any,
        dest: Dict[str, Any],
        orchestrator_context: str,
        *,
        current_user_text: str,
        constraints: Optional[str] = None,
        profile: Any,
        corr_id: str,
    ) -> Any:
        """
        Variant B: step_type="use_cli" is a contract, not a hint.
        We execute the tool directly instead of delegating to the ReAct agent.
        """
        state_root = os.path.join(self._config.defaults.workdir, "_sandbox")
        mode_id = str(get_active_mode(session, "") or "").strip()
        session_workspace = os.path.join(state_root, "sessions", session.id)
        try:
            os.makedirs(session_workspace, exist_ok=True)
        except Exception as e:
            self._log.warning("failed to prepare session workspace path=%s err=%s", session_workspace, e)
        project_root = getattr(session, "project_root", None)
        session_workdir = getattr(session, "workdir", None)
        agent_cwd = project_root or session_workdir or session_workspace
        ctx = {
            "cwd": agent_cwd,
            "state_root": state_root,
            "session_id": session.id,
            "chat_id": dest.get("chat_id"),
            "chat_type": dest.get("chat_type"),
            "bot": bot,
            "context": context,
            # Tools may require real session API (use_cli -> session.run_prompt / routing).
            "session": session,
            "allowed_tools": getattr(profile, "allowed_tools", None),
            "corr_id": corr_id,
            "orchestrator_context": orchestrator_context or "",
        }
        if constraints:
            ctx["orchestrator_context"] = f"{orchestrator_context or ''}\n\n{constraints}".strip()
        if str(getattr(profile, "name", "") or "").strip() == "analyst":
            analyst_timeout_sec = int(getattr(self._config.defaults, "analyst_use_cli_timeout_sec", 3600) or 3600) * 2
            if analyst_timeout_sec > 0:
                ctx["tool_timeouts_ms"] = {"use_cli": analyst_timeout_sec * 1000}
        task_text = self._build_use_cli_task_text(step, session, current_user_text)
        args = {"task_text": task_text}
        response_format = str(getattr(step, "_use_cli_response_format", "") or "").strip()
        if response_format:
            args["response_format"] = response_format
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "orchestrator",
                "phase": "step_start",
                "status": "running",
                "corr_id": corr_id,
                "task_id": str(getattr(step, "id", "") or ""),
                "step_id": str(getattr(step, "id", "") or ""),
                "message": f"Старт шага {step.id} (use_cli)",
            },
        )
        self._log.info("step start corr_id=%s step_type=use_cli profile=%s", corr_id, getattr(profile, "name", None))
        self._log.info("use_cli task_text: %s", task_text[:300])
        result = await self._tool_registry.execute("use_cli", args, ctx)
        if not result.get("success"):
            err = str(result.get("error") or "use_cli failed").strip()
            error_outputs: list[Dict[str, Any]] = [{"type": "error_diagnostic", "content": err}]
            resp = self._deps.ExecutorResponse(
                task_id=step.id,
                status="error",
                summary=f"use_cli: {err}",
                outputs=error_outputs,
                tool_calls=[{"tool": "use_cli", "args": args, "success": False, "error": err, "corr_id": corr_id}],
                next_questions=[],
            )
            self._deps.validate_response(resp)
            self._log.info("step end corr_id=%s status=error", corr_id)
            emit_runtime_progress(
                session,
                {
                    "mode_id": mode_id,
                    "source": "orchestrator",
                    "phase": "step_end",
                    "status": "error",
                    "corr_id": corr_id,
                    "task_id": str(getattr(step, "id", "") or ""),
                    "step_id": str(getattr(step, "id", "") or ""),
                    "message": f"use_cli: {err[:200]}",
                },
            )
            return resp
        output = str(result.get("output") or "")
        tool_outputs = result.get("outputs") or []
        if not isinstance(tool_outputs, list):
            tool_outputs = []
        tool_claims = result.get("claims") or []
        if not isinstance(tool_claims, list):
            tool_claims = []
        tool_open_gaps = result.get("open_gaps") or []
        if not isinstance(tool_open_gaps, list):
            tool_open_gaps = []
        for gap in tool_open_gaps:
            gap_text = str(gap or "").strip()
            if gap_text:
                tool_outputs.append({"type": "open_gap", "content": gap_text, "content_preview": gap_text})
        cli_error = self._classify_use_cli_output_error(output)
        if cli_error:
            resp = self._deps.ExecutorResponse(
                task_id=step.id,
                status="error",
                summary=f"use_cli: {cli_error}",
                outputs=list(tool_outputs) or [{"type": "text", "content": output}],
                claims=[],
                tool_calls=[{"tool": "use_cli", "args": args, "success": False, "error": cli_error, "corr_id": corr_id}],
                next_questions=[],
            )
            self._deps.validate_response(resp)
            self._log.info("step end corr_id=%s status=error summary=%s", corr_id, cli_error[:200])
            emit_runtime_progress(
                session,
                {
                    "mode_id": mode_id,
                    "source": "orchestrator",
                    "phase": "step_end",
                    "status": "error",
                    "corr_id": corr_id,
                    "task_id": str(getattr(step, "id", "") or ""),
                    "step_id": str(getattr(step, "id", "") or ""),
                    "message": f"use_cli: {cli_error[:200]}",
                },
            )
            return resp
        summary = output
        resp = self._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary=summary,
            outputs=list(tool_outputs) or [{"type": "text", "content": output}],
            claims=list(tool_claims),
            tool_calls=[{"tool": "use_cli", "args": args, "success": True, "corr_id": corr_id}],
            next_questions=[],
        )
        self._deps.validate_response(resp)
        self._log.info("step end corr_id=%s status=ok summary=%s", corr_id, summary[:200])
        emit_runtime_progress(
            session,
            {
                "mode_id": mode_id,
                "source": "orchestrator",
                "phase": "step_end",
                "status": "ok",
                "corr_id": corr_id,
                "task_id": str(getattr(step, "id", "") or ""),
                "step_id": str(getattr(step, "id", "") or ""),
                "message": summary[:240],
            },
        )
        return resp

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._executor.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._executor.resolve_question(question_id, answer)

    def clear_session_cache(self, session_id: str) -> None:
        self._executor.clear_session_cache(session_id)

    def get_plugin_ui(self, profile: Any) -> Dict[str, Any]:
        return self._executor.get_plugin_ui(profile)

    async def _maybe_update_memory(self, user_text: str, final_response: str, memory_text: str, cwd: str) -> None:
        decision = await self._deps.decide_memory_save(self._config, user_text, final_response, memory_text)
        if not decision:
            self._log.info("memory: no update needed")
            return
        tag = str(decision.get("tag") or "").strip().upper()
        content = str(decision.get("content") or "").strip()
        layer = str(decision.get("layer") or "").strip().lower() or "semantic"
        source = str(decision.get("source") or "").strip().lower() or "agent"
        confidence = decision.get("confidence")
        ttl_days = decision.get("ttl_days")
        verification_status = decision.get("verification_status")
        evidence_type = decision.get("evidence_type")
        evidence_ref = decision.get("evidence_ref")
        self._log.info(
            "memory: saving tag=%s layer=%s source=%s ttl=%s content_len=%d",
            tag, layer, source, ttl_days, len(content),
        )
        saved = self._deps.append_memory_structured(
            cwd,
            tag=tag,
            content=content,
            layer=layer,
            source=source,
            confidence=confidence,
            ttl_days=ttl_days,
            verification_status=verification_status,
            evidence_type=evidence_type,
            evidence_ref=evidence_ref,
        )
        if not saved:
            return
        updated = self._deps.read_memory(cwd)
        cleaned = self._deps.remove_expired_entries(updated)
        if cleaned != updated:
            self._deps.write_memory(cwd, cleaned + ("\n" if cleaned else ""))
            updated = cleaned
        max_bytes = int(self._config.defaults.memory_max_kb) * 1024
        if self._deps.memory_size_bytes(updated) <= max_bytes:
            return
        target_chars = int(self._config.defaults.memory_compact_target_kb) * 1024
        compacted = await self._deps.compress_memory(self._config, updated, target_chars)
        if compacted:
            self._deps.write_memory(cwd, compacted)
            return
        # Обязательная компрессия при лимите: если LLM недоступен, грубо ужимаем
        priority = ["PREF", "DECISION", "CONFIG", "AGREEMENT"]
        compacted_local = self._deps.compact_memory_by_priority(updated, max_bytes, priority)
        self._deps.write_memory(cwd, compacted_local)
