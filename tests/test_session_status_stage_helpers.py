from __future__ import annotations

from sessions.session_status import build_common_mode_stage, build_manager_mode_stage


def test_common_stage_disabled() -> None:
    assert build_common_mode_stage(enabled=False, running=False, busy=False, queue_len=0) == "выключен"


def test_common_stage_running_busy() -> None:
    assert build_common_mode_stage(enabled=True, running=True, busy=True, queue_len=0) == "обрабатывает задачу"


def test_common_stage_queue_when_not_busy() -> None:
    assert build_common_mode_stage(enabled=True, running=False, busy=False, queue_len=2) == "ждет задачи в очереди"


def test_common_stage_draining() -> None:
    assert (
        build_common_mode_stage(
            enabled=True,
            running=True,
            busy=False,
            queue_len=0,
            running_stage="выполняет",
            draining_stage="завершает обработку",
        )
        == "завершает обработку"
    )


def test_common_stage_idle() -> None:
    assert build_common_mode_stage(enabled=True, running=False, busy=False, queue_len=0) == "ожидает новый запрос"


def test_manager_stage_disabled_is_idle_even_with_plan_status() -> None:
    assert (
        build_manager_mode_stage(
            enabled=False,
            running=False,
            busy=False,
            queue_len=0,
            plan_status="paused",
        )
        == "idle"
    )
