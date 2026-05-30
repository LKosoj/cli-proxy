from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from modes.sdk.runtime.json_normalizer import parse_normalize_validate

from .models import FeedbackDecision, WebmasterContext
from .schemas import WebmasterIntentOutputSchema

if TYPE_CHECKING:
    from .mode import WebmasterMode

logger = logging.getLogger(__name__)


class IntentService:
    """Handles LLM-based intent analysis and feedback classification for WebmasterMode."""

    def __init__(self, mode: "WebmasterMode") -> None:
        self._mode = mode

    # ------------------------------------------------------------------
    # Public API (called via thin delegators on WebmasterMode)
    # ------------------------------------------------------------------

    async def analyze_intent(
        self,
        *,
        bot_app: Any,
        session: Any,
        context: Any,
        dest: Dict[str, Any],
        user_text: str,
        wm_ctx: WebmasterContext,
    ) -> Dict[str, Any]:
        mode = self._mode
        tooling = mode._tooling()
        tool_ctx = mode._tool_ctx(session, context, dest, bot_app)
        resp = await tooling.execute(
            "intent_plugin",
            {
                "user_text": user_text,
                "previous_goal": wm_ctx.goal,
                "previous_actions": wm_ctx.actions,
            },
            tool_ctx,
        )
        if not resp.get("success"):
            raise RuntimeError(f"intent_plugin failed: {resp.get('error')}")
        raw = str(resp.get("output") or "{}").strip()
        if not raw:
            mode._log.warning("webmaster intent_plugin returned empty output; using fallback intent payload")
            return self.fallback_intent_payload(user_text=user_text, wm_ctx=wm_ctx)
        try:
            data = parse_normalize_validate(raw, WebmasterIntentOutputSchema)
        except Exception:
            mode._log.exception("webmaster intent_plugin output normalize/parse failed")
            return self.fallback_intent_payload(user_text=user_text, wm_ctx=wm_ctx)
        return self.normalize_intent_payload(
            payload=data,
            user_text=user_text,
            wm_ctx=wm_ctx,
        )

    def normalize_intent_payload(
        self,
        *,
        payload: Dict[str, Any],
        user_text: str,
        wm_ctx: WebmasterContext,
    ) -> Dict[str, Any]:
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            goal = str(user_text or "").strip()
        if not goal:
            goal = str(wm_ctx.goal or "").strip()
        goal = goal or "Уточнить задачу"
        actions = _normalize_text_list(payload.get("actions"))
        if not actions:
            actions = _normalize_text_list(getattr(wm_ctx, "actions", []))
        if not actions:
            actions = [goal]
        constraints = _normalize_text_list(payload.get("constraints"))
        if not constraints:
            constraints = _normalize_text_list(getattr(wm_ctx, "constraints", []))
        acceptance_criteria = _normalize_text_list(payload.get("acceptance_criteria"))
        if not acceptance_criteria:
            acceptance_criteria = _normalize_text_list(getattr(wm_ctx, "acceptance_criteria", []))
        ambiguities = _normalize_text_list(payload.get("ambiguities"))
        assumptions = _normalize_text_list(payload.get("assumptions"))
        return {
            "goal": goal,
            "actions": actions,
            "constraints": constraints,
            "acceptance_criteria": acceptance_criteria,
            "ambiguities": ambiguities,
            "assumptions": assumptions,
        }

    def fallback_intent_payload(
        self,
        *,
        user_text: str,
        wm_ctx: WebmasterContext,
    ) -> Dict[str, Any]:
        return self.normalize_intent_payload(
            payload={},
            user_text=user_text,
            wm_ctx=wm_ctx,
        )

    async def classify_feedback_llm(
        self,
        bot_app: Any,
        user_text: str,
        wm_ctx: WebmasterContext,
        *,
        session: Any,
    ) -> FeedbackDecision:
        mode = self._mode
        prompts = mode._load_prompts(session=session)
        system = (
            "Определи тип пользовательского сообщения. Верни только JSON: "
            "{\"kind\":\"new_task|continue_task|requirement_change|wrong_execution|unclear\",\"reason\":\"...\"}."
        )
        user = json.dumps(
            {
                "feedback_prompt": prompts["feedback_analysis"],
                "user_text": user_text,
                "context": {
                    "stage": wm_ctx.stage,
                    "goal": wm_ctx.goal,
                    "actions": wm_ctx.actions,
                    "last_cli_report": wm_ctx.last_cli_report[:1500],
                },
            },
            ensure_ascii=False,
        )
        out = await mode._chat_completion(
            bot_app,
            system,
            user,
            response_format={"type": "json_object"},
        )
        parsed = mode._parse_llm_json(out, required_fields=("kind", "reason"))
        kind = str(parsed.get("kind") or "").strip()
        if kind not in ("new_task", "continue_task", "requirement_change", "wrong_execution", "unclear"):
            raise RuntimeError(f"LLM classify_feedback returned invalid kind: {kind}")
        return FeedbackDecision(kind=kind, reason=str(parsed.get("reason") or "").strip())


def _normalize_text_list(values: Any, *, limit: int = 20) -> List[str]:
    """Deduplicate and normalise a list of string values."""
    if not isinstance(values, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        folded = text.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(text)
        if len(out) >= int(limit):
            break
    return out
