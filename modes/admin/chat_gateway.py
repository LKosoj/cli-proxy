from __future__ import annotations

import importlib.util
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

import yaml

from i18n import t
from modes.sdk.runtime.json_normalizer import loads_safe

try:
    from .chat_memory import ChatMemory, ChatMessage
except ImportError:  # pragma: no cover - fallback for direct module loading in tests
    _CHAT_MEMORY_PATH = os.path.join(os.path.dirname(__file__), "chat_memory.py")
    _SPEC = importlib.util.spec_from_file_location(
        "modes_admin_chat_memory_direct", _CHAT_MEMORY_PATH
    )
    if _SPEC is None or _SPEC.loader is None:
        raise
    _MOD = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(_MOD)
    ChatMemory = _MOD.ChatMemory  # type: ignore[assignment]
    ChatMessage = _MOD.ChatMessage  # type: ignore[assignment]

try:
    from .chat_schemas import (
        Intent,
        IntentValidationError,
        ValidationContext,
        build_validation_context,
        parse_intent,
        revalidate_intent,
    )
except ImportError:  # pragma: no cover - fallback for direct module loading in tests
    _CHAT_SCHEMAS_PATH = os.path.join(os.path.dirname(__file__), "chat_schemas.py")
    _SPEC2 = importlib.util.spec_from_file_location(
        "modes_admin_chat_schemas_direct", _CHAT_SCHEMAS_PATH
    )
    if _SPEC2 is None or _SPEC2.loader is None:
        raise
    _MOD2 = importlib.util.module_from_spec(_SPEC2)
    _SPEC2.loader.exec_module(_MOD2)
    Intent = _MOD2.Intent  # type: ignore[assignment]
    IntentValidationError = _MOD2.IntentValidationError  # type: ignore[assignment]
    ValidationContext = _MOD2.ValidationContext  # type: ignore[assignment]
    build_validation_context = _MOD2.build_validation_context  # type: ignore[assignment]
    parse_intent = _MOD2.parse_intent  # type: ignore[assignment]
    revalidate_intent = _MOD2.revalidate_intent  # type: ignore[assignment]

_LOG = logging.getLogger(__name__)

LlmChatProvider = Callable[[str, str], Awaitable[str]]
"""async callable that takes (system_prompt, user_prompt) and returns raw JSON text."""

_DEFAULT_PROMPTS_PATH = os.path.join(os.path.dirname(__file__), "prompts.yaml")
_HISTORY_TAIL = 20
_DEFAULT_TIMEOUT_SEC = 45.0


@dataclass
class ChatDecision:
    """Result of a single chat-gateway turn."""

    reply_text: str = ""
    intent: Optional[Intent] = None
    pending_action_id: Optional[str] = None
    raw_llm_output: str = ""
    error: Optional[str] = None
    executed: bool = False
    clarification_options: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "reply_text": self.reply_text,
            "executed": self.executed,
        }
        if self.intent is not None:
            payload["intent"] = self.intent.as_dict()
        if self.pending_action_id:
            payload["pending_action_id"] = self.pending_action_id
        if self.error:
            payload["error"] = self.error
        if self.clarification_options:
            payload["clarification_options"] = list(self.clarification_options)
        return payload


@dataclass
class PendingApproval:
    """Serialized pending-approval record stored alongside chat history."""

    approval_id: str
    session_id: str
    intent: Dict[str, Any]
    created_at: float
    user_text: str
    pinned_cli: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "intent": dict(self.intent or {}),
            "created_at": float(self.created_at or 0.0),
            "user_text": self.user_text,
            "pinned_cli": self.pinned_cli,
        }


class AdminChatGatewayError(RuntimeError):
    pass


class AdminChatGateway:
    """Turn free-form admin text into a validated intent + reply."""

    def __init__(
        self,
        *,
        workdir: str,
        llm_provider: LlmChatProvider,
        admin_config: Mapping[str, Any],
        known_aliases: Sequence[str],
        pinned_cli: str = "",
        session_id: str = "",
        prompts_path: Optional[str] = None,
        memory: Optional[ChatMemory] = None,
        now_ts: Optional[Callable[[], float]] = None,
    ) -> None:
        self.workdir = str(workdir or "")
        if not self.workdir:
            raise AdminChatGatewayError("workdir is required")
        self._llm_provider = llm_provider
        self._admin_config = dict(admin_config or {})
        self._known_aliases = list(known_aliases or [])
        self._pinned_cli = str(pinned_cli or "")
        self._session_id = str(session_id or "")
        self._prompts_path = str(prompts_path or _DEFAULT_PROMPTS_PATH)
        self._memory = memory or ChatMemory(self.workdir)
        self._now_ts = now_ts or time.time
        self._prompts: Optional[Dict[str, str]] = None

    @property
    def memory(self) -> ChatMemory:
        return self._memory

    def load_prompts(self) -> Dict[str, str]:
        if self._prompts is not None:
            return self._prompts
        try:
            with open(self._prompts_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            data = yaml.safe_load(raw) or {}
        except FileNotFoundError as exc:
            raise AdminChatGatewayError(f"prompts.yaml not found: {self._prompts_path}") from exc
        except yaml.YAMLError as exc:
            raise AdminChatGatewayError(f"invalid YAML in prompts.yaml: {exc}") from exc
        prompts = data.get("prompts") if isinstance(data, Mapping) else None
        if not isinstance(prompts, Mapping):
            raise AdminChatGatewayError("prompts.yaml missing `prompts` mapping")
        self._prompts = {str(k): str(v) for k, v in prompts.items()}
        return self._prompts

    def build_system_prompt(self) -> str:
        prompts = self.load_prompts()
        text = prompts.get("chat_system", "")
        if not text:
            raise AdminChatGatewayError("chat_system prompt is missing")
        return text

    def build_user_prompt(self, user_text: str) -> str:
        prompts = self.load_prompts()
        template = prompts.get("chat_user_template", "")
        if not template:
            raise AdminChatGatewayError("chat_user_template prompt is missing")
        memory_md = self._memory.read_memory_md()
        history = self._format_history(self._memory.tail(_HISTORY_TAIL))
        actions_cfg = self._admin_config.get("actions") or {}
        return template.format(
            workdir=self.workdir,
            pinned_cli=self._pinned_cli or "(not set)",
            aliases=", ".join(sorted(self._known_aliases)) or "(none)",
            actions_local=_format_actions(actions_cfg.get("local")),
            actions_ssh=_format_actions(actions_cfg.get("ssh")),
            memory_md=memory_md or "(empty)",
            history=history or "(empty)",
            user_text=str(user_text or "").strip(),
        )

    async def handle(self, user_text: str, lang: str = 'ru') -> ChatDecision:
        text = str(user_text or "").strip()
        if not text:
            return ChatDecision(
                reply_text=t('admin.chat.empty_input', lang),
                error="empty_input",
            )

        self._memory.append(role="user", text=text)

        try:
            system_prompt = self.build_system_prompt()
            user_prompt = self.build_user_prompt(text)
        except AdminChatGatewayError as exc:
            _LOG.exception("admin chat: prompt build failed")
            reply = t('admin.chat.config_error', lang, exc=exc)
            self._memory.append(role="system", text=reply, intent_type="error")
            return ChatDecision(reply_text=reply, error=str(exc))

        try:
            raw = await self._llm_provider(system_prompt, user_prompt)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("admin chat: llm provider failed")
            reply = t('admin.chat.llm_unavailable', lang, exc=exc)
            self._memory.append(role="system", text=reply, intent_type="error")
            return ChatDecision(reply_text=reply, error="llm_unavailable", raw_llm_output="")

        raw_text = str(raw or "").strip()
        if not raw_text:
            reply = t('admin.chat.llm_empty_response', lang)
            self._memory.append(role="system", text=reply, intent_type="error")
            return ChatDecision(reply_text=reply, error="llm_empty_response", raw_llm_output="")

        try:
            payload = _parse_json(raw_text)
        except ValueError as exc:
            _LOG.warning("admin chat: llm returned non-json output: %s", exc)
            reply = t('admin.chat.llm_invalid_json', lang, exc=exc)
            self._memory.append(
                role="system", text=reply, intent_type="error",
                meta={"raw_llm_output": raw_text},
            )
            return ChatDecision(
                reply_text=reply, error="invalid_json", raw_llm_output=raw_text,
            )

        try:
            intent = parse_intent(payload)
        except IntentValidationError as exc:
            _LOG.warning("admin chat: intent schema invalid: %s", exc)
            reply = t('admin.chat.llm_invalid_intent', lang, exc=exc)
            self._memory.append(
                role="system", text=reply, intent_type="error",
                meta={"raw_llm_output": raw_text},
            )
            return ChatDecision(
                reply_text=reply, error=f"schema:{exc}", raw_llm_output=raw_text,
            )

        ctx = build_validation_context(
            admin_config=self._admin_config,
            known_aliases=self._known_aliases,
        )
        try:
            intent = revalidate_intent(intent, ctx=ctx)
        except IntentValidationError as exc:
            _LOG.warning("admin chat: intent revalidation failed: %s", exc)
            reply = t('admin.chat.action_not_allowed', lang, exc=exc)
            self._memory.append(
                role="system", text=reply, intent_type="error",
                meta={"raw_llm_output": raw_text, "intent": intent.as_dict()},
            )
            return ChatDecision(
                reply_text=reply, error=f"revalidate:{exc}",
                intent=intent, raw_llm_output=raw_text,
            )

        decision = ChatDecision(intent=intent, raw_llm_output=raw_text)

        if intent.type == "answer":
            decision.reply_text = intent.text
            self._memory.append(
                role="assistant", text=intent.text, intent_type="answer",
            )
            return decision

        if intent.type == "ask_clarification":
            decision.reply_text = intent.text
            decision.clarification_options = list(intent.clarification_options)
            self._memory.append(
                role="assistant", text=intent.text, intent_type="ask_clarification",
                meta={"options": list(intent.clarification_options)} if intent.clarification_options else None,
            )
            return decision

        if intent.type == "update_memory":
            appended = self._memory.append_memory_md(intent.memory_append, source="chat")
            reply_text = intent.text or t('admin.chat.memory_saved', lang)
            decision.reply_text = reply_text
            self._memory.append(
                role="assistant", text=reply_text, intent_type="update_memory",
                meta={"memory_diff": appended},
            )
            return decision

        if intent.type == "run_readonly":
            decision.reply_text = (
                intent.text or t('admin.chat.pending_run_readonly', lang, action_id=intent.action_id, target=intent.target)
            )
            self._memory.append(
                role="assistant", text=decision.reply_text,
                intent_type="run_readonly",
                meta={"action_id": intent.action_id, "target": intent.target},
            )
            decision.executed = False  # выполнение делает вызывающий код (handle_input)
            return decision

        # propose_action / propose_new_action / propose_plan — требуют approval.
        approval_id = _new_approval_id()
        decision.pending_action_id = approval_id
        decision.reply_text = _render_pending_text(intent, lang=lang)
        self._memory.append(
            role="assistant", text=decision.reply_text,
            intent_type=f"pending:{intent.type}",
            meta={
                "approval_id": approval_id,
                "intent": intent.as_dict(),
            },
        )
        return decision

    def _format_history(self, messages: List[ChatMessage]) -> str:
        if not messages:
            return ""
        lines: List[str] = []
        for m in messages:
            tag = m.role
            if m.intent_type:
                tag = f"{m.role}:{m.intent_type}"
            lines.append(f"[{m.ts}] {tag}: {m.text}")
        return "\n".join(lines)


def _parse_json(raw: str) -> Mapping[str, Any]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty response")
    # strip common markdown fencing
    if text.startswith("```"):
        parts = text.split("\n", 1)
        text = parts[1] if len(parts) == 2 else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        data = loads_safe(text, strict_first=False)
    except json.JSONDecodeError as exc:
        raise ValueError(f"json decode error: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError("top-level JSON must be an object")
    return data


def _format_actions(raw: Any) -> str:
    if not isinstance(raw, Mapping) or not raw:
        return "(none)"
    rows: List[str] = []
    for action_id, payload in raw.items():
        if not isinstance(payload, Mapping):
            continue
        argv = payload.get("argv")
        argv_text = " ".join(str(a) for a in argv) if isinstance(argv, list) else ""
        risk = str(payload.get("risk_level") or "").strip().lower() or "?"
        read_only = bool(payload.get("read_only"))
        desc = str(payload.get("description") or "").strip()
        suffix = f"[{risk}{' · read_only' if read_only else ''}]"
        line = f"- {action_id} {suffix}: {argv_text}"
        if desc:
            line += f"  # {desc}"
        rows.append(line)
    if not rows:
        return "(none)"
    return "\n".join(rows)


def _render_pending_text(intent: Intent, lang: str = 'ru') -> str:
    if intent.type == "propose_action":
        argv_text = (
            " ".join(intent.argv or []) if intent.argv
            else t('admin.chat.pending_argv_empty', lang)
        )
        base = intent.text or t(
            'admin.chat.pending_propose_action', lang,
            action_id=intent.action_id, target=intent.target,
        )
        return (
            f"{base}\n"
            f"{t('admin.chat.pending_command_line', lang, command=argv_text)}\n"
            f"{t('admin.chat.pending_confirm_suffix', lang, risk_level=intent.risk_level or '?')}"
        )
    if intent.type == "propose_new_action":
        argv_text = " ".join(intent.argv or [])
        base = intent.text or t('admin.chat.pending_propose_new_action', lang)
        save_hint = ""
        if intent.suggest_save and intent.suggested_action_id:
            save_hint = t(
                'admin.chat.pending_save_as_action', lang,
                suggested_action_id=intent.suggested_action_id,
            )
        return (
            f"{base}\n"
            f"{t('admin.chat.pending_target_line', lang, target=intent.target)}\n"
            f"{t('admin.chat.pending_command_line', lang, command=argv_text)}\n"
            f"{t('admin.chat.pending_confirm_suffix', lang, risk_level=intent.risk_level)}{save_hint}"
        )
    if intent.type == "propose_plan":
        steps = intent.steps or []
        header = intent.text or t('admin.chat.pending_plan_header', lang, count=len(steps))
        lines: List[str] = [header]
        for idx, step in enumerate(steps, 1):
            argv_text = " ".join(step.argv or []) if step.argv else ""
            action_part = f"`{step.action_id}`" if step.action_id else ""
            suffix = f" (risk={step.risk_level})" if step.risk_level else ""
            lines.append(
                f"{idx}. [{step.target}] {action_part} {argv_text}{suffix}".strip()
            )
        if intent.stop_on_error:
            lines.append("stop_on_error=True")
        if intent.suggest_save_as_runbook and intent.suggested_runbook_id:
            lines.append(
                t('admin.chat.pending_save_as_runbook', lang, runbook_id=intent.suggested_runbook_id)
            )
        lines.append(t('admin.chat.pending_confirm', lang))
        return "\n".join(lines)
    return intent.text or t('admin.chat.pending_confirm', lang)


def _new_approval_id() -> str:
    return f"chat-{uuid.uuid4().hex[:12]}"


__all__ = [
    "AdminChatGateway",
    "AdminChatGatewayError",
    "ChatDecision",
    "LlmChatProvider",
    "PendingApproval",
]
