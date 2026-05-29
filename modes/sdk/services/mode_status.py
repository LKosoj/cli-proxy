from __future__ import annotations

from typing import Any, Optional

from sessions.session_status import (
    build_common_mode_stage as _build_common_mode_stage,
    build_manager_mode_stage as _build_manager_mode_stage,
    build_mode_status_text as _build_mode_status_text,
    build_webmaster_mode_stage as _build_webmaster_mode_stage,
    get_session_queue_len as _get_session_queue_len,
)


class ModeStatusService:
    """Unified SDK facade over mode/session status text helpers."""

    @staticmethod
    def get_session_queue_len(session: Any) -> int:
        return _get_session_queue_len(session)

    @staticmethod
    def build_common_mode_stage(
        *,
        enabled: bool,
        running: bool,
        busy: bool,
        queue_len: int,
        disabled_stage: str = "выключен",
        running_stage: str = "обрабатывает задачу",
        queued_stage: str = "ждет задачи в очереди",
        idle_stage: str = "ожидает новый запрос",
        draining_stage: Optional[str] = None,
    ) -> str:
        return _build_common_mode_stage(
            enabled=enabled,
            running=running,
            busy=busy,
            queue_len=queue_len,
            disabled_stage=disabled_stage,
            running_stage=running_stage,
            queued_stage=queued_stage,
            idle_stage=idle_stage,
            draining_stage=draining_stage,
        )

    @staticmethod
    def build_manager_mode_stage(
        *,
        enabled: bool,
        running: bool,
        busy: bool,
        queue_len: int,
        plan_status: str,
    ) -> str:
        return _build_manager_mode_stage(
            enabled=enabled,
            running=running,
            busy=busy,
            queue_len=queue_len,
            plan_status=plan_status,
        )

    @staticmethod
    def build_webmaster_mode_stage(
        *,
        enabled: bool,
        running: bool,
        busy: bool,
        queue_len: int,
        wm_stage: str,
    ) -> str:
        return _build_webmaster_mode_stage(
            enabled=enabled,
            running=running,
            busy=busy,
            queue_len=queue_len,
            wm_stage=wm_stage,
        )

    @staticmethod
    def build_mode_status_text(
        session: Any,
        *,
        title: str,
        stage: str,
        enabled: bool,
        queue_suffix: Optional[str] = None,
        task_suffix: Optional[str] = None,
        extra_sections: Optional[list[tuple[str, str]]] = None,
    ) -> str:
        return _build_mode_status_text(
            session,
            title=title,
            stage=stage,
            enabled=enabled,
            queue_suffix=queue_suffix,
            task_suffix=task_suffix,
            extra_sections=extra_sections,
        )
