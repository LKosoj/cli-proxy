from __future__ import annotations

from typing import Any, Dict, Optional

from modes.sdk.planning import MANAGER_CONTINUE_TOKEN, load_plan
from session import session_scoped_key


def _clean_text(value: Any, *, max_len: int = 4000) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _mode_context(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = state.get("mode_context") if isinstance(state, dict) else {}
    return payload if isinstance(payload, dict) else {}


def _execution_context(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = _mode_context(state).get("execution_context")
    return payload if isinstance(payload, dict) else {}


def build_recovery_dest(*, default_kind: str, session: Any, state: Dict[str, Any]) -> Dict[str, Any]:
    execution_context = _execution_context(state)
    mode_context = _mode_context(state)
    dest_kind = (
        _clean_text(execution_context.get("dest_kind"), max_len=32)
        or _clean_text(mode_context.get("dest_kind"), max_len=32)
        or _clean_text(default_kind, max_len=32)
        or "telegram"
    )
    dest: Dict[str, Any] = {"kind": dest_kind}
    chat_id = execution_context.get("chat_id")
    if chat_id in (None, ""):
        chat_id = getattr(session, "chat_id", None)
    if chat_id not in (None, ""):
        dest["chat_id"] = chat_id
    user_id = execution_context.get("user_id")
    if user_id not in (None, ""):
        dest["user_id"] = user_id
    return dest


def build_recovery_prompt(
    *,
    session: Any,
    mode_id: str,
    action: str,
    state: Dict[str, Any],
) -> Optional[str]:
    mode_context = _mode_context(state)
    execution_context = _execution_context(state)
    resolved_mode = _clean_text(mode_id, max_len=64)
    resolved_action = _clean_text(action, max_len=64)

    if resolved_mode == "analyst":
        input_bundle = mode_context.get("input_bundle")
        input_bundle = input_bundle if isinstance(input_bundle, dict) else {}
        prompt = _clean_text(
            input_bundle.get("recovery_prompt_text")
            or input_bundle.get("original_user_text")
            or mode_context.get("source_user_text")
            or execution_context.get("source_user_text")
            or execution_context.get("user_text_preview"),
            max_len=4000,
        )
        if not prompt and resolved_action == "restart_from_phase":
            prompt = _clean_text(
                execution_context.get("analyst_prompt_preview") or execution_context.get("runner_prompt_preview"),
                max_len=4000,
            )
        if not prompt:
            prompt = _clean_text(execution_context.get("analyst_prompt_preview"), max_len=4000)
        if not prompt:
            prompt = _clean_text(execution_context.get("runner_prompt_preview"), max_len=4000)
        return prompt or None

    if resolved_mode == "agent":
        if resolved_action == "restart_from_phase":
            prompt = _clean_text(
                execution_context.get("runner_prompt_preview")
                or mode_context.get("source_prompt"),
                max_len=4000,
            )
        else:
            prompt = _clean_text(mode_context.get("source_prompt"), max_len=4000)
        if not prompt:
            prompt = _clean_text(execution_context.get("user_text_preview"), max_len=4000)
        if not prompt:
            prompt = _clean_text(execution_context.get("runner_prompt_preview"), max_len=4000)
        return prompt or None

    if resolved_mode == "webmaster":
        if resolved_action == "restart_from_phase":
            prompt = _clean_text(mode_context.get("last_cli_task"), max_len=4000)
            if not prompt:
                prompt = _clean_text(execution_context.get("last_cli_task_preview"), max_len=4000)
        else:
            prompt = _clean_text(mode_context.get("last_user_text"), max_len=4000)
        if not prompt:
            prompt = _clean_text(execution_context.get("last_user_text_preview"), max_len=4000)
        if not prompt:
            prompt = _clean_text(mode_context.get("last_user_text_preview"), max_len=4000)
        if not prompt:
            prompt = _clean_text(mode_context.get("last_cli_task"), max_len=4000)
        if not prompt:
            prompt = _clean_text(execution_context.get("last_cli_task_preview"), max_len=4000)
        if not prompt:
            intent_payload = mode_context.get("intent_payload")
            if isinstance(intent_payload, dict):
                prompt = _clean_text(intent_payload.get("goal"), max_len=4000)
        return prompt or None

    if resolved_mode == "manager":
        legacy_plan = load_plan(
            str(getattr(session, "workdir", "") or ""),
            scoped_key=session_scoped_key(session),
        )
        legacy_status = _clean_text(getattr(legacy_plan, "status", ""), max_len=32).lower()
        if resolved_action in {"rollback_to_checkpoint", "restart_from_phase"} and legacy_plan is not None:
            if legacy_status in {"active", "paused", "failed"}:
                return MANAGER_CONTINUE_TOKEN
        prompt = _clean_text(mode_context.get("source_user_text_preview"), max_len=4000)
        if not prompt and legacy_plan is not None:
            prompt = _clean_text(getattr(legacy_plan, "project_goal", ""), max_len=4000)
        if not prompt:
            prompt = _clean_text(mode_context.get("prompt_preview"), max_len=4000)
        return prompt or None

    return None
