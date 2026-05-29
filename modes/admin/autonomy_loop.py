from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Protocol

from modes.sdk.runtime.json_normalizer import loads_safe

from .autonomy_policy import AutonomyPolicy
from .baseline import accept_proposed_baseline, load_proposed_baseline
from .memory import ServerMemory
from .plugin_tools import (
    AdminToolError,
    run_allowlisted_action,
    write_escalation,
)
from .runbooks import Runbook, load_runbooks, match_runbooks
from .snapshot_store import (
    AdminSnapshotStore,
    SEVERITY_ALARM,
    SEVERITY_INFO,
    SEVERITY_NOISE,
    safe_server_id,
)

_log = logging.getLogger(__name__)

META_STABLE_SCANS = "autonomy:consecutive_stable_scans"
META_LAST_ACTION_TS_PREFIX = "autonomy:last_action_ts:"  # + check_id
META_ACTION_WINDOW = "autonomy:recent_action_timestamps"  # JSON list of unix timestamps

# Global observability meta (stored on a synthetic `_autonomy` server store).
AUTONOMY_GLOBAL_SERVER_ID = "_autonomy"
META_TICK_COUNT = "autonomy:tick_count"
META_LAST_TICK_TS = "autonomy:last_tick_ts"
META_ACTIONS_TOTAL = "autonomy:actions_executed_total"
META_ESCALATIONS_TOTAL = "autonomy:escalations_total"
META_IGNORED_TOTAL = "autonomy:ignored_total"
META_DENIED_TOTAL = "autonomy:denied_by_policy_total"
META_BASELINES_AUTO_ACCEPTED_TOTAL = "autonomy:baselines_auto_accepted_total"
META_ACTION_FAILURES_TOTAL = "autonomy:action_failures_total"


@dataclass
class AutonomyDecision:
    """Что агент решил сделать с конкретным drift."""

    action: str  # "execute" | "escalate" | "ignore"
    action_id: Optional[str] = None
    reason: str = ""
    runbook_id: Optional[str] = None
    confidence: float = 0.0
    target_hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "action_id": self.action_id,
            "reason": self.reason,
            "runbook_id": self.runbook_id,
            "confidence": self.confidence,
            "target_hint": self.target_hint,
        }


@dataclass
class DecisionContext:
    server_id: str
    drift: Dict[str, Any]
    runbooks: List[Runbook] = field(default_factory=list)
    dossier: Dict[str, Any] = field(default_factory=dict)


class DecisionMaker(Protocol):
    async def decide(self, ctx: DecisionContext) -> AutonomyDecision: ...


class RuleBasedDecisionMaker:
    """
    Базовый decision maker без LLM:
    - Берёт runbook с самым высоким scoring для тегов drift'а.
    - Если в frontmatter есть `auto_action: {action_id: ..., target_hint: local|ssh}`
      → предлагает execute.
    - Иначе → escalate.

    Не валидирует severity/allowlist — это делает policy-гейт в AdminAutonomyLoop.
    """

    def __init__(self, *, min_confidence_for_execute: float = 0.5) -> None:
        self._min_confidence = float(min_confidence_for_execute)

    async def decide(self, ctx: DecisionContext) -> AutonomyDecision:
        for rb in ctx.runbooks:
            auto = rb.auto_action() if hasattr(rb, "auto_action") else None
            if not isinstance(auto, Mapping):
                continue
            action_id = str(auto.get("action_id") or "").strip()
            if not action_id:
                continue
            target = str(auto.get("target_hint") or auto.get("target") or "").strip().lower() or None
            confidence = float(auto.get("confidence") or 0.7)
            if confidence < self._min_confidence:
                continue
            return AutonomyDecision(
                action="execute",
                action_id=action_id,
                reason=f"runbook {rb.id} auto_action",
                runbook_id=rb.id,
                confidence=confidence,
                target_hint=target,
            )
        severity = str(ctx.drift.get("severity") or "").strip().lower()
        if severity in (SEVERITY_NOISE, SEVERITY_INFO):
            return AutonomyDecision(
                action="ignore",
                reason="no matching runbook auto_action, severity low",
                confidence=0.2,
            )
        return AutonomyDecision(
            action="escalate",
            reason="no matching runbook auto_action",
            confidence=0.3,
        )


_LLM_DECISION_PROMPT = (
    "Ты — SRE-агент, принимающий решение по дрейфу конфигурации сервера.\n"
    "Тебе даны:\n"
    "  - server_id\n"
    "  - drift (check_id, severity, new/prev values, details)\n"
    "  - список кандидатов-runbook'ов (id, title, tags, auto_action)\n\n"
    "Верни СТРОГО JSON-объект без комментариев:\n"
    "{{\n"
    '  "action": "execute" | "escalate" | "ignore",\n'
    '  "action_id": "<id действия, если execute>",\n'
    '  "target_hint": "local" | "ssh" | null,\n'
    '  "runbook_id": "<id>" | null,\n'
    '  "reason": "<короткое объяснение>",\n'
    '  "confidence": 0.0..1.0\n'
    "}}\n\n"
    "Правила:\n"
    "  - severity=alarm → action всегда escalate.\n"
    "  - если нет подходящего runbook с auto_action — execute запрещён.\n"
    "  - при сомнениях выбирай escalate.\n\n"
    "INPUT:\n{payload}\n"
)


class LLMDecisionMaker:
    """
    Decision maker, использующий внешний LLM.

    - Принимает async-колбэк `llm_caller(prompt) -> str` — ответственность за сам
      LLM-call (модель, retry-политика, auth) лежит на callsite.
    - Жёсткий таймаут; любая ошибка/невалидный JSON → fallback на RuleBasedDecisionMaker.
    - Никакой политики severity/allowlist тут нет — это работа AdminAutonomyLoop.
    """

    def __init__(
        self,
        llm_caller: Callable[[str], Awaitable[str]],
        *,
        timeout_sec: float = 15.0,
        fallback: Optional[DecisionMaker] = None,
        prompt_template: str = _LLM_DECISION_PROMPT,
    ) -> None:
        if llm_caller is None:
            raise ValueError("llm_caller is required")
        self._caller = llm_caller
        self._timeout = float(timeout_sec)
        self._fallback = fallback or RuleBasedDecisionMaker()
        self._prompt_template = str(prompt_template)

    async def decide(self, ctx: DecisionContext) -> AutonomyDecision:
        try:
            payload = self._build_payload(ctx)
            prompt = self._prompt_template.format(payload=json.dumps(payload, ensure_ascii=False))
            raw = await asyncio.wait_for(self._caller(prompt), timeout=self._timeout)
            parsed = self._parse_response(raw)
            if parsed is None:
                raise ValueError("LLM returned non-JSON response")
            return self._to_decision(parsed)
        except asyncio.TimeoutError:
            _log.warning("LLMDecisionMaker: timeout, falling back to rule-based")
        except Exception:
            _log.exception("LLMDecisionMaker: failed, falling back to rule-based")
        return await self._fallback.decide(ctx)

    @staticmethod
    def _build_payload(ctx: DecisionContext) -> Dict[str, Any]:
        runbooks_brief: List[Dict[str, Any]] = []
        for rb in ctx.runbooks:
            auto = rb.auto_action() if hasattr(rb, "auto_action") else None
            runbooks_brief.append({
                "id": getattr(rb, "id", ""),
                "title": getattr(rb, "title", ""),
                "tags": list(getattr(rb, "tags", []) or []),
                "auto_action": dict(auto) if isinstance(auto, Mapping) else None,
            })
        return {
            "server_id": ctx.server_id,
            "drift": dict(ctx.drift),
            "runbooks": runbooks_brief,
        }

    @staticmethod
    def _parse_response(raw: Any) -> Optional[Mapping[str, Any]]:
        if raw is None:
            return None
        if isinstance(raw, Mapping):
            return raw
        text = str(raw).strip()
        if not text:
            return None
        # некоторые модели оборачивают в ```json ... ```
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            if text.lower().startswith("json"):
                text = text.split("\n", 1)[-1].strip()
        parsed = loads_safe(text)
        return parsed if isinstance(parsed, Mapping) else None

    @staticmethod
    def _to_decision(data: Mapping[str, Any]) -> AutonomyDecision:
        action = str(data.get("action") or "").strip().lower()
        if action not in ("execute", "escalate", "ignore"):
            action = "escalate"
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        target_hint_raw = data.get("target_hint")
        target_hint = (
            str(target_hint_raw).strip().lower() if isinstance(target_hint_raw, str) and target_hint_raw else None
        )
        action_id = str(data.get("action_id") or "").strip() or None
        reason = str(data.get("reason") or "").strip() or "llm decision"
        # Defense-in-depth: execute без action_id бессмысленно — вырождаем в escalate.
        if action == "execute" and not action_id:
            action = "escalate"
            reason = f"{reason} | coerced: execute without action_id"
        return AutonomyDecision(
            action=action,
            action_id=action_id,
            reason=reason,
            runbook_id=str(data.get("runbook_id") or "").strip() or None,
            confidence=max(0.0, min(1.0, confidence)),
            target_hint=target_hint,
        )


class AdminAutonomyLoop:
    """
    Замыкает цикл: drifts → decision → policy gate → execute/escalate → audit note.
    Plus baseline auto-accept после N стабильных сканов подряд.
    """

    def __init__(
        self,
        workdir: str,
        policy: AutonomyPolicy,
        *,
        decision_maker: Optional[DecisionMaker] = None,
        state_store: Any = None,
        action_runner: Optional[
            Callable[..., Awaitable[Any]]
        ] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.workdir = str(workdir or "")
        if not self.workdir:
            raise ValueError("workdir is required")
        self.policy = policy
        self.decision_maker: DecisionMaker = decision_maker or RuleBasedDecisionMaker()
        self.state_store = state_store
        self._action_runner = action_runner or run_allowlisted_action
        self._clock = clock or time.time

    async def process_server_drifts(
        self,
        server_id: str,
        drifts: List[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        sid = safe_server_id(server_id)
        policy = self.policy.for_server(sid)
        if not policy.enabled:
            return []
        results: List[Dict[str, Any]] = []
        all_runbooks_cached: Optional[List[Runbook]] = None

        for drift in drifts:
            if not isinstance(drift, Mapping):
                continue
            if drift.get("acknowledged_at"):
                continue
            sev = str(drift.get("severity") or "").strip().lower()
            tags = _drift_tags(drift)

            if all_runbooks_cached is None:
                all_runbooks_cached = load_runbooks(self.workdir, server_ids=[sid])
            # match per-drift: разные drifts могут иметь разные tags.
            matched = match_runbooks(all_runbooks_cached, server_id=sid, tags=tags)
            ctx = DecisionContext(server_id=sid, drift=dict(drift), runbooks=matched)
            try:
                decision = await self.decision_maker.decide(ctx)
            except Exception as exc:
                _log.exception("autonomy decision failed server=%s drift=%s", sid, drift.get("id"))
                decision = AutonomyDecision(
                    action="escalate",
                    reason=f"decision maker error: {exc}",
                    confidence=0.0,
                )

            applied = False
            gated_reason: Optional[str] = None
            action_result: Optional[Dict[str, Any]] = None

            runtime_failure = False
            if decision.action == "execute":
                if sev == SEVERITY_ALARM:
                    gated_reason = "alarm severity never auto-applied"
                elif not policy.permits_severity(sev):
                    gated_reason = f"severity {sev} not in auto_apply_severities"
                elif not policy.permits_action(decision.action_id or ""):
                    gated_reason = f"action {decision.action_id} not in auto_exec_actions"
                elif self._cooldown_active(sid, str(drift.get("check_id") or ""), policy):
                    gated_reason = "cooldown active"
                elif self._rate_limited(sid, policy):
                    gated_reason = "rate-limited (max_actions_per_hour)"
                else:
                    action_result, applied = await self._run_action(
                        sid=sid, decision=decision, policy=policy,
                    )
                    if not applied:
                        gated_reason = "action run failed"
                        runtime_failure = True

            if gated_reason:
                # переход execute → escalate: гейт сработал или runtime-фейл
                self._escalate(sid, drift, decision, extra_reason=gated_reason)
                decision = AutonomyDecision(
                    action="escalate",
                    action_id=decision.action_id,
                    reason=f"gated: {gated_reason}",
                    runbook_id=decision.runbook_id,
                    confidence=decision.confidence,
                    target_hint=decision.target_hint,
                )
                if runtime_failure:
                    self._bump_counter(META_ACTION_FAILURES_TOTAL, 1)
                else:
                    self._bump_counter(META_DENIED_TOTAL, 1)
                self._bump_counter(META_ESCALATIONS_TOTAL, 1)
            elif decision.action == "escalate":
                self._escalate(sid, drift, decision)
                self._bump_counter(META_ESCALATIONS_TOTAL, 1)
            elif decision.action == "ignore":
                self._note(
                    sid,
                    f"IGNORE drift#{drift.get('id')} check={drift.get('check_id')} "
                    f"reason={decision.reason}",
                    tags=["autonomy", "ignore"],
                )
                self._bump_counter(META_IGNORED_TOTAL, 1)
            elif decision.action == "execute" and applied:
                self._bump_counter(META_ACTIONS_TOTAL, 1)

            entry = {
                "server_id": sid,
                "drift_id": drift.get("id"),
                "check_id": drift.get("check_id"),
                "severity": sev,
                "decision": decision.to_dict(),
                "applied": applied,
                "gated_reason": gated_reason,
                "action_result": action_result,
            }
            results.append(entry)
        return results

    async def _run_action(
        self,
        *,
        sid: str,
        decision: AutonomyDecision,
        policy: AutonomyPolicy,
    ) -> tuple[Optional[Dict[str, Any]], bool]:
        if not decision.action_id:
            return None, False

        # 1) dry-run превью. Всегда выполняем первым, логируем.
        try:
            dry = await self._action_runner(
                workdir=self.workdir,
                action_id=decision.action_id,
                server_id=sid,
                target_hint=decision.target_hint,
                dry_run=True,
            )
        except AdminToolError as exc:
            self._note(
                sid,
                f"AUTO-EXEC DENIED action={decision.action_id} reason=dry_run error: {exc}",
                tags=["autonomy", "denied"],
            )
            return None, False
        except Exception as exc:
            _log.exception("autonomy dry_run failed sid=%s action=%s", sid, decision.action_id)
            self._note(
                sid,
                f"AUTO-EXEC DENIED action={decision.action_id} reason=dry_run crash: {exc}",
                tags=["autonomy", "denied"],
            )
            return None, False

        dry_dict = dry.to_dict() if hasattr(dry, "to_dict") else dict(dry or {})
        if policy.require_dry_run_success and not bool(dry_dict.get("success")):
            self._note(
                sid,
                f"AUTO-EXEC DENIED action={decision.action_id} reason=dry_run_unsuccessful",
                tags=["autonomy", "denied"],
            )
            return dry_dict, False

        # 2) Real run
        try:
            real = await self._action_runner(
                workdir=self.workdir,
                action_id=decision.action_id,
                server_id=sid,
                target_hint=decision.target_hint,
                dry_run=False,
            )
        except Exception as exc:
            _log.exception("autonomy real_run failed sid=%s action=%s", sid, decision.action_id)
            self._note(
                sid,
                f"AUTO-EXEC FAILED action={decision.action_id} error={exc}",
                tags=["autonomy", "failed"],
            )
            return dry_dict, False

        real_dict = real.to_dict() if hasattr(real, "to_dict") else dict(real or {})
        success = bool(real_dict.get("success"))
        self._record_action(sid, check_id=str(decision.action_id))
        self._note(
            sid,
            (
                f"AUTO-EXEC {'OK' if success else 'FAIL'} action={decision.action_id} "
                f"runbook={decision.runbook_id or '-'} rc={real_dict.get('exit_code')}"
            ),
            tags=["autonomy", "exec"],
        )
        return real_dict, success

    def _escalate(
        self,
        sid: str,
        drift: Mapping[str, Any],
        decision: AutonomyDecision,
        *,
        extra_reason: Optional[str] = None,
    ) -> None:
        urgency = "high" if str(drift.get("severity") or "").lower() == SEVERITY_ALARM else "medium"
        reason = decision.reason or "autonomy escalation"
        if extra_reason:
            reason = f"{reason} | {extra_reason}"
        try:
            write_escalation(
                workdir=self.workdir,
                state_store=self.state_store,
                server_id=sid,
                reason=reason,
                urgency=urgency,
                context={
                    "drift_id": drift.get("id"),
                    "check_id": drift.get("check_id"),
                    "severity": drift.get("severity"),
                    "decision": decision.to_dict(),
                },
            )
        except AdminToolError:
            _log.exception("autonomy: write_escalation failed sid=%s", sid)

    def _note(self, sid: str, text: str, *, tags: Optional[List[str]] = None) -> None:
        try:
            ServerMemory(self.workdir, sid).append_note(text, source="autonomy", tags=tags or [])
        except Exception:
            _log.exception("autonomy: failed to write memory note sid=%s", sid)

    # --- rate-limit / cooldown ---

    def _cooldown_active(self, sid: str, check_id: str, policy: AutonomyPolicy) -> bool:
        if not check_id or policy.cooldown_sec <= 0:
            return False
        store = self._store(sid)
        key = META_LAST_ACTION_TS_PREFIX + check_id
        raw = store.get_meta(key)
        try:
            last_ts = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            last_ts = 0.0
        return (self._clock() - last_ts) < float(policy.cooldown_sec)

    def _rate_limited(self, sid: str, policy: AutonomyPolicy) -> bool:
        if policy.max_actions_per_hour <= 0:
            return False
        store = self._store(sid)
        raw = store.get_meta(META_ACTION_WINDOW) or ""
        timestamps: List[float] = []
        if raw:
            try:
                parsed = loads_safe(raw)
                if isinstance(parsed, list):
                    timestamps = [float(x) for x in parsed if isinstance(x, (int, float))]
            except Exception:
                timestamps = []
        now = self._clock()
        horizon = now - 3600.0
        recent = [t for t in timestamps if t >= horizon]
        return len(recent) >= int(policy.max_actions_per_hour)

    def _record_action(self, sid: str, *, check_id: str) -> None:
        store = self._store(sid)
        now = float(self._clock())
        if check_id:
            store.set_meta(META_LAST_ACTION_TS_PREFIX + check_id, str(now))
        raw = store.get_meta(META_ACTION_WINDOW) or ""
        timestamps: List[float] = []
        if raw:
            try:
                parsed = loads_safe(raw)
                if isinstance(parsed, list):
                    timestamps = [float(x) for x in parsed if isinstance(x, (int, float))]
            except Exception:
                timestamps = []
        timestamps.append(now)
        horizon = now - 3600.0
        timestamps = [t for t in timestamps if t >= horizon]
        store.set_meta(META_ACTION_WINDOW, json.dumps(timestamps))

    def _store(self, sid: str) -> AdminSnapshotStore:
        return AdminSnapshotStore.for_server(self.workdir, sid)

    def _global_store(self) -> AdminSnapshotStore:
        return AdminSnapshotStore.for_server(self.workdir, AUTONOMY_GLOBAL_SERVER_ID)

    def _bump_counter(self, key: str, delta: int = 1) -> None:
        try:
            self._global_store().bump_meta(key, int(delta))
        except Exception:
            _log.exception("autonomy: bump counter failed key=%s", key)

    def record_tick_start(self) -> None:
        try:
            store = self._global_store()
            store.set_meta(META_LAST_TICK_TS, str(int(self._clock())))
        except Exception:
            _log.exception("autonomy: failed to record tick start")
        self._bump_counter(META_TICK_COUNT, 1)

    # --- baseline auto-accept ---

    def maybe_auto_accept_baseline(
        self,
        server_id: str,
        *,
        drifts_this_tick: int,
    ) -> Optional[str]:
        """
        Возвращает имя сервера, у которого baseline был авто-принят, либо None.
        Логика: при каждом «чистом» скане (0 drifts) увеличиваем счётчик.
        Как только счётчик достигает threshold И есть proposed baseline — принимаем.
        """
        sid = safe_server_id(server_id)
        policy = self.policy.for_server(sid)
        if not policy.enabled or not policy.baseline_auto_accept_enabled:
            return None
        store = self._store(sid)
        if int(drifts_this_tick) > 0:
            store.set_meta(META_STABLE_SCANS, "0")
            return None
        raw = store.get_meta(META_STABLE_SCANS) or "0"
        try:
            current = int(raw)
        except ValueError:
            current = 0
        current += 1
        store.set_meta(META_STABLE_SCANS, str(current))
        if current < int(policy.baseline_auto_accept_after_stable_scans):
            return None
        proposed = load_proposed_baseline(self.workdir, sid)
        if proposed is None:
            return None
        try:
            accept_proposed_baseline(self.workdir, sid)
        except Exception:
            _log.exception("autonomy: auto-accept baseline failed sid=%s", sid)
            return None
        store.set_meta(META_STABLE_SCANS, "0")
        self._note(
            sid,
            f"BASELINE AUTO-ACCEPTED after {current} stable scans",
            tags=["autonomy", "baseline"],
        )
        self._bump_counter(META_BASELINES_AUTO_ACCEPTED_TOTAL, 1)
        return sid


def _drift_tags(drift: Mapping[str, Any]) -> List[str]:
    tags: List[str] = []
    check_id = str(drift.get("check_id") or "").strip()
    if check_id:
        tags.append(check_id)
        head = check_id.split(".", 1)[0]
        if head and head != check_id:
            tags.append(head)
    details = drift.get("details") if isinstance(drift.get("details"), Mapping) else None
    if details:
        kind = str(details.get("kind") or "").strip()
        if kind:
            tags.append(f"kind:{kind}")
    sev = str(drift.get("severity") or "").strip().lower()
    if sev:
        tags.append(f"severity:{sev}")
    return tags


__all__ = [
    "AUTONOMY_GLOBAL_SERVER_ID",
    "AdminAutonomyLoop",
    "AutonomyDecision",
    "DecisionContext",
    "DecisionMaker",
    "LLMDecisionMaker",
    "META_ACTIONS_TOTAL",
    "META_ACTION_FAILURES_TOTAL",
    "META_BASELINES_AUTO_ACCEPTED_TOTAL",
    "META_DENIED_TOTAL",
    "META_ESCALATIONS_TOTAL",
    "META_IGNORED_TOTAL",
    "META_LAST_TICK_TS",
    "META_TICK_COUNT",
    "RuleBasedDecisionMaker",
]
