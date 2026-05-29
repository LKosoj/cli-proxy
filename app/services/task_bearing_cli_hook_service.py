from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from session import session_runtime_uid
from sessions.session_state_access import get_active_mode

from app.services.memory_event_store import MemoryEventStore
from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from app.services.run_observability_service import RunObservabilityService
from app.services.skill_runtime_service import SkillRuntimeService, SkillSelectionResult


logger = logging.getLogger(__name__)

_HOOK_ATTR = "_shared_task_bearing_cli_hook_service"
_SKILL_RUNTIME_ATTR = "_shared_skill_runtime_selector_service"
_ARTIFACT_STORE_ATTR = "_shared_run_artifact_store"
_RUN_OBSERVABILITY_ATTR = "_shared_run_observability_service"
_MEMORY_EVENT_STORE_ATTR = "_shared_memory_event_store"
_CLI_SUBRUN_SOURCES = {
    "cli_routing",
    "desktop_direct",
    "raw_prompt",
    "telegram_direct",
    "use_cli_plugin",
}

_TECHNICAL_COMMAND_PREFIXES = (
    "git ",
    "pytest",
    ".venv/bin/pytest",
    "python -m pytest",
    "python3 -m pytest",
    "flake8",
    ".venv/bin/flake8",
    "python -m flake8",
    "rg ",
    "grep ",
    "sed ",
    "awk ",
    "cat ",
    "ls",
    "pwd",
    "find ",
    "bash -lc ",
    "sh -lc ",
    "npm test",
    "pnpm test",
    "yarn test",
    "uv run ",
)


def _clean_text(value: Any, *, max_len: int = 280) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _prompt_hash(prompt: str) -> str:
    digest = hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class PreparedCliExecution:
    original_prompt: str
    prompt_for_cli: str
    mode_id: str
    phase: str
    source: str
    task_bearing: bool
    technical_command: bool
    selector_bypassed: bool
    bypass_reason: Optional[str]
    selection_result: Optional[SkillSelectionResult]
    run: Optional[RunArtifactHandle]
    started_at: float
    unit_id: str
    prompt_hash: str
    session_uid: str
    execution_id: str


class TaskBearingCliHookService:
    def __init__(
        self,
        config: Any,
        *,
        skill_runtime: SkillRuntimeService | None = None,
        artifact_store: RunArtifactStore | None = None,
        observability: RunObservabilityService | None = None,
        memory_event_store: MemoryEventStore | None = None,
    ) -> None:
        self.config = config
        self.skill_runtime = skill_runtime or self._ensure_skill_runtime(config)
        self.artifact_store = artifact_store or self._ensure_artifact_store(config)
        self.observability = observability or self._ensure_observability(config, self.artifact_store)
        self.memory_event_store = memory_event_store

    def bind_shared_services(
        self,
        *,
        skill_runtime: SkillRuntimeService | None = None,
        artifact_store: RunArtifactStore | None = None,
        observability: RunObservabilityService | None = None,
        memory_event_store: MemoryEventStore | None = None,
    ) -> None:
        if skill_runtime is not None:
            self.skill_runtime = skill_runtime
        if artifact_store is not None:
            self.artifact_store = artifact_store
        if observability is not None:
            self.observability = observability
        if memory_event_store is not None:
            self.memory_event_store = memory_event_store

    async def prepare_prompt(
        self,
        *,
        session: Any,
        prompt: str,
        source: str,
        mode_id: Optional[str] = None,
        phase: str = "execute",
        task_bearing: bool = True,
        technical_command: Optional[bool] = None,
    ) -> PreparedCliExecution:
        original_prompt = str(prompt or "")
        resolved_source = str(source or "").strip() or "cli"
        resolved_mode_id = self._resolve_mode_id(
            session=session,
            explicit_mode_id=mode_id,
            source=resolved_source,
        )
        resolved_phase = str(phase or "").strip() or "execute"
        started_at = time.time()
        unit_id = f"cli:{resolved_source}"
        prompt_hash = _prompt_hash(original_prompt)
        execution_id = f"exec:{started_at:.6f}:{prompt_hash}"
        run = self._start_run(
            session=session,
            mode_id=resolved_mode_id,
            phase=resolved_phase,
            source=resolved_source,
            prompt=original_prompt,
        )
        bypass_reason = self._selector_bypass_reason(
            prompt=original_prompt,
            task_bearing=task_bearing,
            technical_command=technical_command,
        )
        selector_bypassed = bypass_reason is not None
        prepared_prompt = original_prompt
        selection_result: Optional[SkillSelectionResult] = None
        memory_session_uid = self._memory_session_uid(session=session, run=run)

        self._record_execution_start(
            run=run,
            mode_id=resolved_mode_id,
            phase=resolved_phase,
            source=resolved_source,
            unit_id=unit_id,
            task_bearing=task_bearing,
            selector_bypassed=selector_bypassed,
            bypass_reason=bypass_reason,
            started_at=started_at,
        )
        self._record_memory_event(
            event_type="cli_execution_start",
            session=session,
            run=run,
            mode_id=resolved_mode_id,
            phase=resolved_phase,
            source=resolved_source,
            unit_id=unit_id,
            prompt_hash=prompt_hash,
            session_uid=memory_session_uid,
            execution_id=execution_id,
            payload={
                "task_bearing": bool(task_bearing),
                "technical_command": bool(technical_command) if technical_command is not None else False,
                "selector_bypassed": bool(selector_bypassed),
                "bypass_reason": str(bypass_reason or "") or None,
                "prompt_len": len(original_prompt),
                "started_at": started_at,
            },
            dedupe_suffix="start",
            created_at=started_at,
        )

        if not selector_bypassed:
            selection_result = await self.skill_runtime.resolve_for_task(
                session=session,
                mode_id=resolved_mode_id,
                phase=resolved_phase,
                task_text=original_prompt,
            )
            prepared_prompt = selection_result.composed_task_text
            self._record_selection(
                run=run,
                phase=resolved_phase,
                unit_id=unit_id,
                source=resolved_source,
                result=selection_result,
            )

        return PreparedCliExecution(
            original_prompt=original_prompt,
            prompt_for_cli=prepared_prompt,
            mode_id=resolved_mode_id,
            phase=resolved_phase,
            source=resolved_source,
            task_bearing=bool(task_bearing),
            technical_command=bool(technical_command) if technical_command is not None else False,
            selector_bypassed=selector_bypassed,
            bypass_reason=bypass_reason,
            selection_result=selection_result,
            run=run,
            started_at=started_at,
            unit_id=unit_id,
            prompt_hash=prompt_hash,
            session_uid=memory_session_uid,
            execution_id=execution_id,
        )

    def record_retry(self, prepared: PreparedCliExecution, *, reason: Any) -> None:
        reason_text = _clean_text(reason)
        reason_hash = _prompt_hash(reason_text)
        self._record_memory_event_for_prepared(
            prepared,
            event_type="cli_execution_retry",
            payload={
                "reason_type": type(reason).__name__,
                "reason_len": len(reason_text),
                "reason_hash": reason_hash,
            },
            dedupe_suffix=f"retry:{reason_hash}",
        )
        if prepared.run is None:
            return
        try:
            if self.observability.is_enabled():
                self.observability.record_retry(
                    prepared.run,
                    phase=prepared.phase,
                    unit_id=prepared.unit_id,
                    reason=_clean_text(reason),
                )
            self.artifact_store.append_event(
                prepared.run,
                {
                    "event_type": "cli_execution_retry",
                    "mode_id": prepared.mode_id,
                    "phase": prepared.phase,
                    "source": prepared.source,
                    "unit_id": prepared.unit_id,
                    "reason": _clean_text(reason),
                },
            )
        except Exception:
            logger.exception("task-bearing CLI hook retry record failed source=%s", prepared.source)

    def record_success(self, prepared: PreparedCliExecution, *, output: Any) -> None:
        finished_at = time.time()
        duration_sec = max(0.0, finished_at - float(prepared.started_at or finished_at))
        output_text = str(output or "")
        self._record_memory_event_for_prepared(
            prepared,
            event_type="cli_execution_end",
            payload={
                "status": "ok",
                "selector_bypassed": prepared.selector_bypassed,
                "cache_hit": self._cache_hit(prepared.selection_result),
                "output_len": len(output_text),
                "duration_sec": duration_sec,
                "finished_at": finished_at,
                "selected_skill_ids": self._selected_skill_ids(prepared.selection_result),
            },
            dedupe_suffix="end:ok",
            created_at=finished_at,
        )
        if prepared.run is None:
            return
        try:
            if self.observability.is_enabled():
                self.observability.record_unit_end(
                    prepared.run,
                    unit_id=prepared.unit_id,
                    phase=prepared.phase,
                    status="ok",
                    duration_sec=duration_sec,
                    message=f"{prepared.source}:ok",
                )
            self.artifact_store.save_state(
                prepared.run,
                {
                    "status": "completed",
                    "phase": prepared.phase,
                    "source": prepared.source,
                    "selector_bypassed": prepared.selector_bypassed,
                    "selected_skill_ids": self._selected_skill_ids(prepared.selection_result),
                    "updated_at": finished_at,
                },
            )
            self.artifact_store.append_event(
                prepared.run,
                {
                    "event_type": "cli_execution_end",
                    "mode_id": prepared.mode_id,
                    "phase": prepared.phase,
                    "source": prepared.source,
                    "unit_id": prepared.unit_id,
                    "status": "ok",
                    "selector_bypassed": prepared.selector_bypassed,
                    "cache_hit": self._cache_hit(prepared.selection_result),
                    "output_len": len(output_text),
                    "finished_at": finished_at,
                    "selected_skill_ids": self._selected_skill_ids(prepared.selection_result),
                },
            )
            self.artifact_store.mark_finished(prepared.run, status="completed", phase=prepared.phase)
        except Exception:
            logger.exception("task-bearing CLI hook success record failed source=%s", prepared.source)

    def record_error(self, prepared: PreparedCliExecution, *, error: Any) -> None:
        finished_at = time.time()
        duration_sec = max(0.0, finished_at - float(prepared.started_at or finished_at))
        error_text = _clean_text(error, max_len=512)
        error_type = type(error).__name__
        self._record_memory_event_for_prepared(
            prepared,
            event_type="cli_execution_error",
            payload={
                "status": "error",
                "selector_bypassed": prepared.selector_bypassed,
                "cache_hit": self._cache_hit(prepared.selection_result),
                "error_type": error_type,
                "error_len": len(str(error or "")),
                "error_hash": _prompt_hash(str(error or "")),
                "duration_sec": duration_sec,
                "finished_at": finished_at,
                "selected_skill_ids": self._selected_skill_ids(prepared.selection_result),
            },
            dedupe_suffix="end:error",
            created_at=finished_at,
        )
        if prepared.run is None:
            return
        try:
            if self.observability.is_enabled():
                self.observability.record_unit_end(
                    prepared.run,
                    unit_id=prepared.unit_id,
                    phase=prepared.phase,
                    status="error",
                    duration_sec=duration_sec,
                    message=error_text,
                )
            self.artifact_store.save_state(
                prepared.run,
                {
                    "status": "failed",
                    "phase": prepared.phase,
                    "source": prepared.source,
                    "selector_bypassed": prepared.selector_bypassed,
                    "selected_skill_ids": self._selected_skill_ids(prepared.selection_result),
                    "last_error": error_text,
                    "updated_at": finished_at,
                },
            )
            self.artifact_store.append_event(
                prepared.run,
                {
                    "event_type": "cli_execution_error",
                    "mode_id": prepared.mode_id,
                    "phase": prepared.phase,
                    "source": prepared.source,
                    "unit_id": prepared.unit_id,
                    "status": "error",
                    "selector_bypassed": prepared.selector_bypassed,
                    "cache_hit": self._cache_hit(prepared.selection_result),
                    "error": error_text,
                    "finished_at": finished_at,
                    "selected_skill_ids": self._selected_skill_ids(prepared.selection_result),
                },
            )
            self.artifact_store.mark_finished(prepared.run, status="failed", phase=prepared.phase)
        except Exception:
            logger.exception("task-bearing CLI hook error record failed source=%s", prepared.source)

    def _record_execution_start(
        self,
        *,
        run: Optional[RunArtifactHandle],
        mode_id: str,
        phase: str,
        source: str,
        unit_id: str,
        task_bearing: bool,
        selector_bypassed: bool,
        bypass_reason: Optional[str],
        started_at: float,
    ) -> None:
        if run is None:
            return
        try:
            if self.observability.is_enabled():
                self.observability.record_unit_start(
                    run,
                    unit_id=unit_id,
                    phase=phase,
                    message=f"{source}:start",
                    ts=started_at,
                )
            self.artifact_store.append_event(
                run,
                {
                    "event_type": "cli_execution_start",
                    "mode_id": mode_id,
                    "phase": phase,
                    "source": source,
                    "unit_id": unit_id,
                    "task_bearing": bool(task_bearing),
                    "selector_bypassed": bool(selector_bypassed),
                    "bypass_reason": str(bypass_reason or "") or None,
                    "started_at": started_at,
                },
            )
        except Exception:
            logger.exception("task-bearing CLI hook start record failed source=%s", source)

    def _record_selection(
        self,
        *,
        run: Optional[RunArtifactHandle],
        phase: str,
        unit_id: str,
        source: str,
        result: SkillSelectionResult,
    ) -> None:
        if run is None:
            return
        selected_skill_ids = self._selected_skill_ids(result)
        try:
            if self.observability.is_enabled():
                self.observability.record_skill_selection(
                    run,
                    phase=phase,
                    unit_id=unit_id,
                    selected_skills=selected_skill_ids,
                    reason="cache_hit" if result.cache_hit else "selected",
                )
            self.artifact_store.append_event(
                run,
                {
                    "event_type": "cli_skill_context_applied",
                    "phase": phase,
                    "source": source,
                    "unit_id": unit_id,
                    "cache_hit": bool(result.cache_hit),
                    "model_used": str(result.model_used or ""),
                    "task_hash": str(result.task_hash or ""),
                    "skills_hash": str(result.skills_hash or ""),
                    "selected_skill_ids": selected_skill_ids,
                },
            )
            self.artifact_store.save_state(
                run,
                {
                    "phase": phase,
                    "source": source,
                    "selected_skill_ids": selected_skill_ids,
                    "skill_cache_hit": bool(result.cache_hit),
                    "skill_model_used": str(result.model_used or ""),
                },
            )
        except Exception:
            logger.exception("task-bearing CLI hook selection record failed source=%s", source)

    def _start_run(
        self,
        *,
        session: Any,
        mode_id: str,
        phase: str,
        source: str,
        prompt: str,
    ) -> Optional[RunArtifactHandle]:
        if not self._run_artifacts_enabled():
            return None
        try:
            return self.artifact_store.start_run(
                session=session,
                mode_id=mode_id,
                phase=phase,
                source_prompt_hash=_prompt_hash(prompt),
                mode_context={"source": source, "task_bearing": True},
            )
        except Exception:
            logger.exception("task-bearing CLI hook run start failed mode=%s source=%s", mode_id, source)
            return None

    def _resolve_mode_id(
        self,
        *,
        session: Any,
        explicit_mode_id: Optional[str],
        source: str,
    ) -> str:
        token = str(explicit_mode_id or "").strip()
        if token:
            return token
        if str(source or "").strip().lower() in _CLI_SUBRUN_SOURCES:
            return "cli"
        try:
            token = str(get_active_mode(session, "") or "").strip()
        except Exception:
            logger.exception("task-bearing CLI hook failed to resolve active mode")
            token = ""
        return token or "cli"

    @staticmethod
    def _selected_skill_ids(result: Optional[SkillSelectionResult]) -> list[str]:
        if result is None:
            return []
        return [item.skill_id for item in result.selected_skills]

    @staticmethod
    def _cache_hit(result: Optional[SkillSelectionResult]) -> bool:
        return bool(getattr(result, "cache_hit", False))

    def _selector_bypass_reason(
        self,
        *,
        prompt: str,
        task_bearing: bool,
        technical_command: Optional[bool],
    ) -> Optional[str]:
        if not task_bearing:
            return "non_task_bearing"
        if technical_command is True:
            return "technical_command"
        if technical_command is False:
            return None
        if self._looks_like_technical_utility_prompt(prompt):
            return "technical_utility_prompt"
        return None

    @staticmethod
    def _looks_like_technical_utility_prompt(prompt: str) -> bool:
        cleaned = str(prompt or "").strip().lower()
        if not cleaned or "\n" in cleaned:
            return False
        return any(
            cleaned == prefix.rstrip() or cleaned.startswith(prefix)
            for prefix in _TECHNICAL_COMMAND_PREFIXES
        )

    def _record_memory_event_for_prepared(
        self,
        prepared: PreparedCliExecution,
        *,
        event_type: str,
        payload: dict[str, Any],
        dedupe_suffix: str,
        created_at: float | None = None,
    ) -> None:
        self._record_memory_event(
            event_type=event_type,
            session=None,
            run=prepared.run,
            mode_id=prepared.mode_id,
            phase=prepared.phase,
            source=prepared.source,
            unit_id=prepared.unit_id,
            prompt_hash=prepared.prompt_hash,
            session_uid=prepared.session_uid,
            execution_id=prepared.execution_id,
            payload=payload,
            dedupe_suffix=dedupe_suffix,
            created_at=created_at,
        )

    def _record_memory_event(
        self,
        *,
        event_type: str,
        session: Any,
        run: Optional[RunArtifactHandle],
        mode_id: str,
        phase: str,
        source: str,
        unit_id: str,
        prompt_hash: str,
        session_uid: str,
        execution_id: str,
        payload: dict[str, Any],
        dedupe_suffix: str,
        created_at: float | None = None,
    ) -> None:
        if not self._memory_events_enabled():
            return
        try:
            store = self._ensure_memory_event_store(self.config, self.memory_event_store)
            self.memory_event_store = store
            resolved_session_uid = str(session_uid or "").strip() or self._memory_session_uid(session=session, run=run)
            run_id = str(getattr(run, "run_id", "") or "")
            store.record_event(
                event_type=event_type,
                source=source,
                session_uid=resolved_session_uid,
                run_id=run_id,
                mode_id=mode_id,
                phase=phase,
                unit_id=unit_id,
                prompt_hash=prompt_hash,
                payload=dict(payload or {}),
                dedupe_key=":".join(
                    token
                    for token in (
                        resolved_session_uid,
                        run_id or execution_id,
                        mode_id,
                        phase,
                        unit_id,
                        dedupe_suffix,
                    )
                    if token
                ),
                created_at=created_at,
            )
            self._prune_memory_events(store)
        except Exception:
            logger.exception("task-bearing CLI memory event record failed source=%s event=%s", source, event_type)

    @staticmethod
    def _memory_session_uid(*, session: Any, run: Optional[RunArtifactHandle]) -> str:
        run_uid = str(getattr(run, "session_uid", "") or "").strip()
        if run_uid:
            return run_uid
        if session is None:
            return ""
        try:
            canonical = str(session_runtime_uid(session) or "").strip()
            if canonical:
                return canonical
        except Exception:
            logger.exception("task-bearing CLI hook failed to resolve canonical session uid")
        for attr in ("session_uid", "scoped_key", "id"):
            token = str(getattr(session, attr, "") or "").strip()
            if token:
                return token
        return ""

    def _memory_events_enabled(self) -> bool:
        defaults = getattr(self.config, "defaults", None)
        return bool(getattr(defaults, "memory_events_enabled", False))

    def _prune_memory_events(self, store: MemoryEventStore) -> None:
        defaults = getattr(self.config, "defaults", None)
        retention_days = int(getattr(defaults, "memory_events_retention_days", 30) or 30)
        store.prune_older_than(retention_days=retention_days)

    def _run_artifacts_enabled(self) -> bool:
        defaults = getattr(self.config, "defaults", None)
        return bool(getattr(defaults, "run_artifacts_enabled", True))

    @staticmethod
    def _ensure_memory_event_store(
        config: Any,
        existing: MemoryEventStore | None = None,
    ) -> MemoryEventStore:
        if isinstance(existing, MemoryEventStore):
            return existing
        cached = getattr(config, _MEMORY_EVENT_STORE_ATTR, None)
        if isinstance(cached, MemoryEventStore):
            return cached
        store = MemoryEventStore.from_config(config)
        setattr(config, _MEMORY_EVENT_STORE_ATTR, store)
        return store

    @staticmethod
    def _ensure_skill_runtime(config: Any) -> SkillRuntimeService:
        existing = getattr(config, _SKILL_RUNTIME_ATTR, None)
        if isinstance(existing, SkillRuntimeService):
            return existing
        service = SkillRuntimeService(config)
        setattr(config, _SKILL_RUNTIME_ATTR, service)
        return service

    @staticmethod
    def _ensure_artifact_store(config: Any) -> RunArtifactStore:
        existing = getattr(config, _ARTIFACT_STORE_ATTR, None)
        if isinstance(existing, RunArtifactStore):
            return existing
        service = RunArtifactStore(config)
        setattr(config, _ARTIFACT_STORE_ATTR, service)
        return service

    @staticmethod
    def _ensure_observability(config: Any, artifact_store: RunArtifactStore) -> RunObservabilityService:
        existing = getattr(config, _RUN_OBSERVABILITY_ATTR, None)
        if isinstance(existing, RunObservabilityService):
            return existing
        defaults = getattr(config, "defaults", None)
        service = RunObservabilityService(
            enabled=bool(getattr(defaults, "run_metrics_enabled", True)),
            artifact_store=artifact_store,
        )
        setattr(config, _RUN_OBSERVABILITY_ATTR, service)
        return service


def get_task_bearing_cli_hook_service(
    config: Any,
    *,
    skill_runtime: SkillRuntimeService | None = None,
    artifact_store: RunArtifactStore | None = None,
    observability: RunObservabilityService | None = None,
    memory_event_store: MemoryEventStore | None = None,
) -> TaskBearingCliHookService:
    if config is None:
        raise RuntimeError("config is required for task-bearing CLI hook service")
    existing = getattr(config, _HOOK_ATTR, None)
    if isinstance(existing, TaskBearingCliHookService):
        existing.bind_shared_services(
            skill_runtime=skill_runtime,
            artifact_store=artifact_store,
            observability=observability,
            memory_event_store=memory_event_store,
        )
        return existing
    service = TaskBearingCliHookService(
        config,
        skill_runtime=skill_runtime,
        artifact_store=artifact_store,
        observability=observability,
        memory_event_store=memory_event_store,
    )
    setattr(config, _HOOK_ATTR, service)
    return service


def register_task_bearing_cli_foundation_services(
    config: Any,
    *,
    artifact_store: RunArtifactStore,
    observability: RunObservabilityService,
    skill_runtime: Optional[SkillRuntimeService] = None,
    memory_event_store: Optional[MemoryEventStore] = None,
) -> None:
    setattr(config, _ARTIFACT_STORE_ATTR, artifact_store)
    setattr(config, _RUN_OBSERVABILITY_ATTR, observability)
    if skill_runtime is not None:
        setattr(config, _SKILL_RUNTIME_ATTR, skill_runtime)
    if memory_event_store is not None:
        setattr(config, _MEMORY_EVENT_STORE_ATTR, memory_event_store)
    get_task_bearing_cli_hook_service(
        config,
        skill_runtime=skill_runtime,
        artifact_store=artifact_store,
        observability=observability,
        memory_event_store=memory_event_store,
    )
