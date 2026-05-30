"""
RunArtifactsMixin — общий миксин жизненного цикла run-artifacts.

Воспроизводит поведенчески идентичные методы, дублированные в режимах
admin, webmaster, analyst, manager, agent.

Предполагает, что класс-хост (BaseMode-подкласс) предоставляет через duck typing:
  - self._optional_run_artifacts() -> Optional[Any]
  - self._optional_run_doctor() -> Optional[Any]
  - self._optional_run_boundary_validation() -> Optional[Any]
  - self.mode_id: str
  - self.config: Any
  - self._log: logging.Logger

Параметризация:
  - merge_execution_context в _save_run_state:
      "shallow" (по умолчанию) — dict(existing).update(incoming) — поведение admin/webmaster/manager/agent
      "deep"    — рекурсивный deep-merge — поведение analyst
  - _RUN_HANDLE_SESSION_ATTR:
      Класс-атрибут строки, задающий имя атрибута сессии для хранения активного RunArtifactHandle.
      Дефолт "_mode_active_run_handle".
      Реальные значения по режимам:
        admin     -> "_admin_run_handle"
        webmaster -> "_webmaster_run_handle"
        analyst   -> "analyst_run_artifact_handle"
        agent     -> "agent_run_artifact_handle"
        manager   -> "_manager_mode_active_run_handle"
"""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

from app.services.run_artifact_store import RunArtifactStore, is_terminal_status

if TYPE_CHECKING:
    from app.services.run_artifact_store import RunArtifactHandle

_DEFAULT_RUN_HANDLE_SESSION_ATTR = "_mode_active_run_handle"

MergeStrategy = Literal["shallow", "deep"]


class RunArtifactsMixin:
    """
    Миксин lifecycle-методов run-artifacts.

    Хост-класс ДОЛЖЕН предоставлять:
      - self._optional_run_artifacts()
      - self._optional_run_doctor()
      - self._optional_run_boundary_validation()
      - self.mode_id (str)
      - self.config (Any)
      - self._log (logging.Logger)

    Хост-класс МОЖЕТ переопределить класс-атрибут _RUN_HANDLE_SESSION_ATTR
    для задания имени session-атрибута хранения активного RunArtifactHandle.
    """

    _RUN_HANDLE_SESSION_ATTR: str = _DEFAULT_RUN_HANDLE_SESSION_ATTR

    # ------------------------------------------------------------------
    # Вспомогательные свойства
    # ------------------------------------------------------------------

    def _artifact_store(self) -> Optional[RunArtifactStore]:
        """Создаёт RunArtifactStore из self.config; возвращает None если config отсутствует."""
        config = getattr(self, "config", None)
        if config is None:
            return None
        return RunArtifactStore(config)

    def _is_run_artifacts_enabled(self) -> bool:
        """Возвращает True если сервис run_artifacts доступен и включён."""
        service = self._optional_run_artifacts()  # type: ignore[attr-defined]
        if service is None:
            return False
        try:
            return bool(service.is_enabled())
        except Exception:
            log = getattr(self, "_log", logging.getLogger(__name__))
            log.exception("run artifacts: failed to resolve enabled flag mode_id=%s", getattr(self, "mode_id", ""))
            return False

    # ------------------------------------------------------------------
    # Статические методы
    # ------------------------------------------------------------------

    @staticmethod
    def _prompt_hash(prompt: str) -> str:
        """sha256-хеш текста промпта; детерминирован для одинакового input."""
        digest = hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _is_terminal_run_status(status: Any) -> bool:
        """True для терминальных статусов: aborted, canceled, completed, failed, ..."""
        return is_terminal_status(status)

    @staticmethod
    def _deep_merge_execution_context(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        """
        Рекурсивный deep-merge двух словарей execution_context.
        Если для одного ключа оба значения — dict, они сливаются рекурсивно.
        В противном случае incoming-значение перекрывает existing.
        Соответствует analyst._deep_merge_execution_context.
        """
        merged = dict(existing)
        for key, value in incoming.items():
            current_value = merged.get(key)
            if isinstance(current_value, dict) and isinstance(value, dict):
                merged[key] = RunArtifactsMixin._deep_merge_execution_context(current_value, value)
                continue
            merged[key] = value
        return merged

    # ------------------------------------------------------------------
    # Последний прогон режима
    # ------------------------------------------------------------------

    def _latest_mode_run(self, session: Any) -> Optional["RunArtifactHandle"]:
        """
        Простая версия: возвращает latest_run из artifact_store без фильтра run_scope.
        Соответствует поведению admin и webmaster.
        Режимы analyst/agent/manager имеют дополнительный фильтр run_scope=="mode_pipeline"
        и переопределяют этот метод самостоятельно.
        """
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None
        return artifact_store.latest_run(session=session, mode_id=getattr(self, "mode_id", ""))

    # ------------------------------------------------------------------
    # Диагностика возобновления
    # ------------------------------------------------------------------

    def _diagnose_resume_boundary(self, run: "RunArtifactHandle") -> Any:
        """
        Вызывает run_doctor для диагностики прерванного run перед повторным запуском.
        Возвращает report или None если doctor недоступен/отключён.
        Ошибки логируются как exception, не пробрасываются.
        """
        log = getattr(self, "_log", logging.getLogger(__name__))
        doctor = self._optional_run_doctor()  # type: ignore[attr-defined]
        if doctor is None or not doctor.is_enabled():
            return None
        artifact_store = self._artifact_store()
        try:
            state = artifact_store.load_state(run) if artifact_store is not None else {}
            phase = str((state or {}).get("phase") or "complete")
            return doctor.diagnose(run, mode_id=getattr(self, "mode_id", ""), phase=phase)
        except Exception:
            log.exception(
                "run artifacts: doctor resume diagnosis failed run_id=%s",
                getattr(run, "run_id", ""),
            )
            return None

    # ------------------------------------------------------------------
    # Сохранение состояния прогона
    # ------------------------------------------------------------------

    def _save_run_state(
        self,
        run: Optional["RunArtifactHandle"],
        *,
        phase: str,
        status: str,
        mode_context: Optional[Dict[str, Any]] = None,
        merge_execution_context: MergeStrategy = "shallow",
    ) -> None:
        """
        Сохраняет phase/status/mode_context в RunArtifactStore.

        merge_execution_context:
          "shallow" (default) — поведение admin/webmaster/manager/agent:
              merged_execution_context = dict(existing); merged.update(incoming)
          "deep" — поведение analyst:
              рекурсивный deep-merge через _deep_merge_execution_context

        Ошибки логируются, не пробрасываются.
        """
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        log = getattr(self, "_log", logging.getLogger(__name__))
        try:
            current = artifact_store.load_state(run)
            merged_mode_context = dict(current.get("mode_context") or {})
            incoming_mode_context = dict(mode_context or {})
            existing_execution_context = merged_mode_context.get("execution_context")
            incoming_execution_context = incoming_mode_context.get("execution_context")
            if isinstance(existing_execution_context, dict) and isinstance(incoming_execution_context, dict):
                if merge_execution_context == "deep":
                    incoming_mode_context["execution_context"] = self._deep_merge_execution_context(
                        existing_execution_context,
                        incoming_execution_context,
                    )
                else:
                    merged_execution_context = dict(existing_execution_context)
                    merged_execution_context.update(incoming_execution_context)
                    incoming_mode_context["execution_context"] = merged_execution_context
            merged_mode_context.update(incoming_mode_context)
            artifact_store.save_state(
                run,
                {
                    "phase": str(phase or current.get("phase") or "complete"),
                    "status": str(status or current.get("status") or "running"),
                    "mode_context": merged_mode_context,
                },
            )
        except Exception:
            log.exception(
                "run artifacts: save_state failed phase=%s run_id=%s",
                phase,
                getattr(run, "run_id", ""),
            )

    # ------------------------------------------------------------------
    # План прогона
    # ------------------------------------------------------------------

    def _save_run_plan(self, run: Optional["RunArtifactHandle"], plan: Dict[str, Any]) -> None:
        """Сохраняет план прогона. Ошибки логируются, не пробрасываются."""
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        log = getattr(self, "_log", logging.getLogger(__name__))
        try:
            artifact_store.save_plan(run, dict(plan or {}))
        except Exception:
            log.exception("run artifacts: save_plan failed run_id=%s", getattr(run, "run_id", ""))

    # ------------------------------------------------------------------
    # Чекпоинты и события
    # ------------------------------------------------------------------

    def _append_checkpoint(self, run: Optional["RunArtifactHandle"], checkpoint: Dict[str, Any]) -> None:
        """Добавляет чекпоинт прогона. Ошибки логируются, не пробрасываются."""
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        log = getattr(self, "_log", logging.getLogger(__name__))
        try:
            artifact_store.append_checkpoint(run, dict(checkpoint or {}))
        except Exception:
            log.exception("run artifacts: append_checkpoint failed run_id=%s", getattr(run, "run_id", ""))

    def _append_run_event(self, run: Optional["RunArtifactHandle"], event: Dict[str, Any]) -> None:
        """Добавляет событие прогона. Ошибки логируются, не пробрасываются."""
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        log = getattr(self, "_log", logging.getLogger(__name__))
        try:
            artifact_store.append_event(run, dict(event or {}))
        except Exception:
            log.exception("run artifacts: append_event failed run_id=%s", getattr(run, "run_id", ""))

    # ------------------------------------------------------------------
    # Граничная валидация
    # ------------------------------------------------------------------

    def _validate_run_boundary(self, run: Optional["RunArtifactHandle"], *, phase: str) -> None:
        """
        Валидирует граничные условия прогона для заданной фазы.
        Выбрасывает RuntimeError если статус не "ok".
        Соответствует admin/manager/agent (без специфики analyst с quality gate).
        """
        if run is None:
            return
        validator = self._optional_run_boundary_validation()  # type: ignore[attr-defined]
        if validator is None or not validator.is_enabled():
            return
        report = validator.validate(run, mode_id=getattr(self, "mode_id", ""), phase=phase)
        if str(report.status or "") == "ok":
            return
        issues = ", ".join(issue.code for issue in report.issues)
        raise RuntimeError(
            f"Run boundary validation failed mode_id={getattr(self, 'mode_id', '')} phase={phase}: {issues}"
        )

    # ------------------------------------------------------------------
    # Завершение прогона
    # ------------------------------------------------------------------

    def _mark_run_finished(
        self, run: Optional["RunArtifactHandle"], *, status: str, phase: str
    ) -> None:
        """Помечает прогон завершённым. Ошибки логируются, не пробрасываются."""
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        log = getattr(self, "_log", logging.getLogger(__name__))
        try:
            artifact_store.mark_finished(run, status=status, phase=phase)
        except Exception:
            log.exception("run artifacts: mark_finished failed run_id=%s", getattr(run, "run_id", ""))

    # ------------------------------------------------------------------
    # Управление активным handle через session-атрибут
    # ------------------------------------------------------------------
    #
    # Имя атрибута сессии задаётся через класс-атрибут _RUN_HANDLE_SESSION_ATTR.
    # Значения по режимам:
    #   admin     -> "_admin_run_handle"
    #   webmaster -> "_webmaster_run_handle"
    #   analyst   -> "analyst_run_artifact_handle"
    #   agent     -> "agent_run_artifact_handle"
    #   manager   -> "_manager_mode_active_run_handle"
    #
    # При переходе режима на RunArtifactsMixin достаточно задать:
    #   _RUN_HANDLE_SESSION_ATTR = "<соответствующее_значение>"

    def _set_active_run_handle(self, session: Any, run: "RunArtifactHandle") -> None:
        """Сохраняет активный RunArtifactHandle в session-атрибут."""
        attr = getattr(self, "_RUN_HANDLE_SESSION_ATTR", _DEFAULT_RUN_HANDLE_SESSION_ATTR)
        setattr(session, attr, run)

    def _active_run_handle(self, session: Any) -> Optional["RunArtifactHandle"]:
        """Возвращает активный RunArtifactHandle из session или None."""
        from app.services.run_artifact_store import RunArtifactHandle as _Handle
        attr = getattr(self, "_RUN_HANDLE_SESSION_ATTR", _DEFAULT_RUN_HANDLE_SESSION_ATTR)
        handle = getattr(session, attr, None)
        return handle if isinstance(handle, _Handle) else None

    def _clear_active_run_handle(self, session: Any) -> None:
        """Обнуляет active run handle в session-атрибуте."""
        attr = getattr(self, "_RUN_HANDLE_SESSION_ATTR", _DEFAULT_RUN_HANDLE_SESSION_ATTR)
        if hasattr(session, attr):
            setattr(session, attr, None)
