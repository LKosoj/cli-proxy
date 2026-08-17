from __future__ import annotations

from pathlib import Path

from modes.sdk.services import ErrorMessageService


REPO_ROOT = Path(__file__).resolve().parents[1]
MODE_FILES = (
    REPO_ROOT / "modes" / "agent" / "mode.py",
)


def test_error_message_service_has_stable_messages() -> None:
    assert ErrorMessageService.manager_archive_failed() == "Не удалось перенести план в архив."
    assert ErrorMessageService.manager_runtime_unavailable() == "Manager runtime недоступен."
    assert ErrorMessageService.manager_resume_failed() == "Не удалось возобновить план."
    assert ErrorMessageService.stale_choice_resend_task() == "Выбор устарел. Пришлите задачу заново."


def test_modes_use_mode_status_service_layer() -> None:
    for path in MODE_FILES:
        source = path.read_text(encoding="utf-8")
        assert "from sessions.session_status" not in source
        assert "ModeStatusService" in source
