from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from modes.sdk.runtime.json_normalizer import loads_safe
from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_last_mode_output,
    set_active_mode,
)


DIRECT_CLI_MODE_ID = "direct_cli"
ORCHESTRATOR_MODE_ID = "orchestrator"
_LLM_MIN_CONFIDENCE = 0.6
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorProposal:
    target_mode_id: str
    target_label: str
    reason: str
    confidence: float
    source: str = "deterministic"


@dataclass(frozen=True)
class ModePolicy:
    mode_id: str
    label: str
    capabilities: Tuple[str, ...]
    keywords: Tuple[str, ...]
    deterministic_only: bool
    can_auto_enter: bool


class AdvancedOrchestratorService:
    """
    Session-level intelligent mode router with deterministic guardrails.

    - Never routes to agent mode.
    - Treats direct CLI as virtual mode `direct_cli`.
    - Supports dynamic modes (fallback generic policy).
    """

    _NON_ROUTABLE_MODE_IDS = {"agent", "codebase_mapper"}

    _DEFAULT_POLICIES: Dict[str, ModePolicy] = {
        DIRECT_CLI_MODE_ID: ModePolicy(
            mode_id=DIRECT_CLI_MODE_ID,
            label="Прямой CLI",
            capabilities=("cli_execution", "terminal_commands", "ad_hoc_run"),
            keywords=("cli", "терминал", "shell", "bash", "команда", "run", "exec"),
            deterministic_only=False,
            can_auto_enter=True,
        ),
        "analyst": ModePolicy(
            mode_id="analyst",
            label="Analyst",
            capabilities=("analysis", "audit", "requirements", "spec_generation"),
            keywords=("анализ", "audit", "аудит", "тз", "spec", "review", "исслед"),
            deterministic_only=False,
            can_auto_enter=True,
        ),
        "manager": ModePolicy(
            mode_id="manager",
            label="Manager",
            capabilities=("planning", "orchestration", "task_decomposition", "execution_control"),
            keywords=("план", "декомп", "оркестр", "этап", "шаг", "менедж", "коорди"),
            deterministic_only=False,
            can_auto_enter=True,
        ),
        "webmaster": ModePolicy(
            mode_id="webmaster",
            label="Webmaster",
            capabilities=("web_build", "frontend_flow", "ux_iteration", "web_debug"),
            keywords=("сайт", "страниц", "frontend", "верст", "css", "html", "ui", "ux"),
            deterministic_only=False,
            can_auto_enter=True,
        ),
        "codebase_mapper": ModePolicy(
            mode_id="codebase_mapper",
            label="Codebase Mapper",
            capabilities=("repo_map", "architecture_map", "code_index", "context_gathering"),
            keywords=("карта", "мап", "структур", "индекс", "архитектур", "codebase"),
            deterministic_only=True,
            can_auto_enter=False,
        ),
        "agent": ModePolicy(
            mode_id="agent",
            label="Agent",
            capabilities=("general_autonomy",),
            keywords=(),
            deterministic_only=True,
            can_auto_enter=False,
        ),
    }

    def _canonical_mode_id(self, mode_id: Optional[str]) -> str:
        mid = str(mode_id or "").strip()
        return mid or DIRECT_CLI_MODE_ID

    def _available_modes(self, mode_registry: Any) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = [(DIRECT_CLI_MODE_ID, "Прямой CLI")]
        if mode_registry is None or not hasattr(mode_registry, "list_modes"):
            return out
        try:
            modes = list(mode_registry.list_modes() or [])
        except Exception:
            _LOG.exception("failed to list modes for orchestrator; fallback to direct_cli only")
            return out
        for mode_id, label in modes:
            mid = str(mode_id or "").strip()
            if not mid:
                continue
            out.append((mid, str(label or mid).strip() or mid))
        return out

    def _policy_for(self, mode_id: str, label: str) -> ModePolicy:
        mid = str(mode_id or "").strip()
        known = self._DEFAULT_POLICIES.get(mid)
        if known is not None:
            return known
        # Dynamic mode fallback: no hand-written keywords required.
        return ModePolicy(
            mode_id=mid,
            label=str(label or mid).strip() or mid,
            capabilities=("mode_specific",),
            keywords=tuple(x for x in re.split(r"[^a-zA-Zа-яА-Я0-9]+", f"{mid} {label}") if x),
            deterministic_only=False,
            can_auto_enter=True,
        )

    @staticmethod
    def _normalize_text(text: str) -> str:
        return str(text or "").strip().lower()

    @staticmethod
    def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
        for token in needles:
            t = str(token or "").strip().lower()
            if t and t in haystack:
                return True
        return False

    def _score_mode(self, *, text: str, mode_id: str, label: str, current_mode_id: str) -> float:
        policy = self._policy_for(mode_id, label)
        if mode_id in self._NON_ROUTABLE_MODE_IDS or not policy.can_auto_enter:
            return -1.0

        score = 0.0

        # Prefer staying in current mode unless there is a meaningful signal.
        if mode_id == current_mode_id:
            score += 0.4

        # Explicit mode mention is strongest and supports dynamic modes.
        if self._contains_any(text, [mode_id, label]):
            score += 3.0

        # Capability/keyword-driven intent matching.
        for kw in policy.keywords:
            if kw and kw in text:
                score += 1.2

        if mode_id == DIRECT_CLI_MODE_ID and self._contains_any(text, ["прямой cli", "без режима", "только cli"]):
            score += 2.0

        return score

    def _allowed_transition(self, source_mode_id: str, target_mode_id: str, available_mode_ids: set[str]) -> bool:
        if target_mode_id in self._NON_ROUTABLE_MODE_IDS:
            return False
        if source_mode_id in self._NON_ROUTABLE_MODE_IDS:
            # Non-routable modes intentionally excluded from orchestration chain.
            return False
        if target_mode_id not in available_mode_ids:
            return False
        return True

    def propose_transition(self, *, session: Any, text: str, mode_registry: Any) -> Optional[OrchestratorProposal]:
        content = self._normalize_text(text)
        if not content:
            return None

        current_mode_id = self._canonical_mode_id(get_active_mode(session, None))
        available = self._available_modes(mode_registry)
        available_ids = {mid for mid, _ in available}

        ranked: List[Tuple[float, str, str]] = []
        for mode_id, label in available:
            score = self._score_mode(text=content, mode_id=mode_id, label=label, current_mode_id=current_mode_id)
            ranked.append((score, mode_id, label))

        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return None

        top_score, top_mode, top_label = ranked[0]
        if top_score < 1.6:
            return None

        if top_mode == current_mode_id:
            return None

        if not self._allowed_transition(current_mode_id, top_mode, available_ids):
            return None

        # If confidence is marginal and second candidate is too close, avoid noisy prompts.
        if len(ranked) > 1:
            second_score = ranked[1][0]
            if (top_score - second_score) < 0.35:
                return None

        reason = f"Обнаружен более подходящий режим: {top_label}."
        confidence = max(0.0, min(1.0, top_score / 4.0))
        return OrchestratorProposal(
            target_mode_id=top_mode,
            target_label=top_label,
            reason=reason,
            confidence=confidence,
            source="deterministic",
        )

    async def propose_transition_hybrid(
        self,
        *,
        session: Any,
        text: str,
        mode_registry: Any,
        app_config: Any = None,
        llm_router_fn=None,
    ) -> Optional[OrchestratorProposal]:
        """
        Hybrid routing:
        1) LLM suggests candidate + reason + confidence.
        2) Policy/guardrails validate.
        3) On low confidence/invalid/unavailable -> deterministic fallback.
        """
        fallback = self.propose_transition(session=session, text=text, mode_registry=mode_registry)
        llm = llm_router_fn
        if llm is None or app_config is None:
            return fallback
        llm_candidate = await self._propose_transition_llm(
            session=session,
            text=text,
            mode_registry=mode_registry,
            app_config=app_config,
            llm_router_fn=llm,
        )
        if llm_candidate is None:
            return fallback
        return llm_candidate

    async def _propose_transition_llm(
        self,
        *,
        session: Any,
        text: str,
        mode_registry: Any,
        app_config: Any,
        llm_router_fn,
    ) -> Optional[OrchestratorProposal]:
        available = self._available_modes(mode_registry)
        available_map = {mid: label for mid, label in available}
        available_ids = set(available_map.keys())
        current_mode_id = self._canonical_mode_id(get_active_mode(session, None))
        if current_mode_id in self._NON_ROUTABLE_MODE_IDS:
            return None
        system = (
            "Ты маршрутизатор режимов. Верни JSON объект с полями: "
            "mode_id (string), reason (string), confidence (number 0..1). "
            "Если переход не нужен, верни mode_id='stay'. "
            "Никогда не выбирай mode_id='agent' и mode_id='codebase_mapper'."
        )
        modes_text = "\n".join(f"- {mid}: {label}" for mid, label in available)
        user = (
            f"Текущий режим: {current_mode_id}\n"
            f"Доступные режимы:\n{modes_text}\n\n"
            f"Запрос пользователя:\n{text}\n"
        )
        try:
            raw = await llm_router_fn(
                app_config,
                system,
                user,
                response_format={"type": "json_object"},
            )
            payload = loads_safe(str(raw or "").strip(), strict_first=False)
        except Exception:
            _LOG.exception("orchestrator llm routing failed")
            return None
        mode_id = str(payload.get("mode_id") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        try:
            confidence = float(payload.get("confidence"))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if not mode_id or mode_id == "stay":
            return None
        if confidence < _LLM_MIN_CONFIDENCE:
            return None
        if not self._allowed_transition(current_mode_id, mode_id, available_ids):
            return None
        if mode_id == current_mode_id:
            return None
        label = available_map.get(mode_id, mode_id)
        if not reason:
            reason = f"LLM выбрал более подходящий режим: {label}."
        return OrchestratorProposal(
            target_mode_id=mode_id,
            target_label=label,
            reason=reason,
            confidence=confidence,
            source="llm",
        )

    def build_handoff_input(self, *, session: Any, original_user_text: str) -> str:
        """
        For accepted transition:
        - If there is a full previous mode result, pass it unchanged.
        - Otherwise pass the original user request unchanged.
        """
        prev = get_orchestrator_last_mode_output(session, None)
        if isinstance(prev, str) and prev.strip():
            return prev
        return str(original_user_text or "")

    def apply_mode(self, *, session: Any, target_mode_id: str) -> None:
        prev = self._canonical_mode_id(get_active_mode(session, None))
        mid = self._canonical_mode_id(target_mode_id)
        try:
            # Runtime-only loop guard marker: remembers previous step in orchestration chain.
            setattr(session, "_orchestrator_prev_mode_id", prev)
        except Exception:
            _LOG.exception("failed to store orchestrator previous mode marker")
        set_active_mode(session, None if mid == DIRECT_CLI_MODE_ID else mid)

    def build_confirm_text(self, *, current_mode_label: str, proposal: OrchestratorProposal) -> str:
        confidence_pct = int(round(float(proposal.confidence) * 100))
        return (
            "Продвинутый оркестратор предлагает переход.\n"
            f"Текущий режим: {current_mode_label}.\n"
            f"Предлагаемый режим: {proposal.target_label}.\n"
            f"Причина: {proposal.reason}\n"
            f"Уверенность: {confidence_pct}%\n\n"
            "Выполнить переход перед обработкой сообщения?"
        )

    def current_mode_label(self, *, session: Any, mode_registry: Any) -> str:
        current = self._canonical_mode_id(get_active_mode(session, None))
        if current == DIRECT_CLI_MODE_ID:
            return "Прямой CLI"
        if mode_registry is not None and hasattr(mode_registry, "list_modes"):
            try:
                for mode_id, label in list(mode_registry.list_modes() or []):
                    if str(mode_id or "").strip() == current:
                        return str(label or current)
            except Exception:
                pass
        return current

    def mode_policies(self, *, mode_registry: Any = None) -> List[ModePolicy]:
        modes = self._available_modes(mode_registry)
        return [
            self._policy_for(mode_id, label)
            for mode_id, label in modes
            if mode_id not in self._NON_ROUTABLE_MODE_IDS
        ]
