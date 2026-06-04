import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PySide6.QtWidgets import QPushButton

from desktop.widgets.run_operations_panel import RunOperationsPanelWidget
from i18n import t


def _btn_label(key: str) -> str:
    # Panel falls back to "ru" for MagicMock facade.ui_language.
    return t(key, "ru")


@pytest.fixture
def mock_facade():
    facade = MagicMock()
    facade.subscribe.side_effect = lambda callback: (lambda: None)
    facade.list_runs.return_value = [
        {
            "session_uid": "desktop:s1",
            "mode_id": "analyst",
            "run_id": "run_20260312T120500Z_d4c3b2a1",
            "status": "running",
            "phase": "execute",
            "active": True,
            "recommended_action": "rollback_to_checkpoint",
            "can_recover": True,
            "can_resume": False,
            "issue_codes": ["missing_plan"],
            "skill_log": ["Injected: playwright-cli, xlsx"],
            "project_local_skill_ids": ["playwright-cli"],
            "cli_work_type": None,
            "executor_profile": None,
        }
    ]
    facade.doctor_run = AsyncMock(return_value={"message": "Doctor выполнен."})
    facade.recover_run = AsyncMock(return_value={"message": "Recover подготовлен."})
    facade.resume_run = AsyncMock(return_value={"message": "Resume подготовлен."})
    facade.promote_run_skills = AsyncMock(return_value={"message": "Skills promoted to global: playwright-cli"})
    return facade


@pytest.mark.asyncio
async def test_run_operations_panel_renders_runs_and_invokes_facade_methods(qtbot, mock_facade):
    panel = RunOperationsPanelWidget(mock_facade, session_uid="desktop:s1")
    qtbot.addWidget(panel)

    assert "1" in panel.summary_label.text()
    assert mock_facade.list_runs.call_args_list[0].args == ("desktop:s1",)

    labels = [label.text() for label in panel.findChildren(type(panel.summary_label))]
    assert any("Injected: playwright-cli, xlsx" in text for text in labels)

    def _ensure_async(coro, parent=None):
        task = asyncio.get_running_loop().create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda current: parent._background_tasks.discard(current))
        return task

    with patch("desktop.widgets.run_operations_panel.ensure_async", side_effect=_ensure_async):
        buttons = panel.findChildren(QPushButton)
        doctor_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_doctor"))
        doctor_button.click()
        await asyncio.sleep(0)

        buttons = panel.findChildren(QPushButton)
        recover_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_recover"))
        recover_button.click()
        await asyncio.sleep(0)

        buttons = panel.findChildren(QPushButton)
        resume_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_resume"))
        assert resume_button.isEnabled() is False

        buttons = panel.findChildren(QPushButton)
        promote_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_promote_skills"))
        promote_button.click()
        await asyncio.sleep(0)

    mock_facade.doctor_run.assert_awaited_once_with(
        "desktop:s1",
        mode_id="analyst",
        run_id="run_20260312T120500Z_d4c3b2a1",
    )
    mock_facade.recover_run.assert_awaited_once_with(
        "desktop:s1",
        mode_id="analyst",
        run_id="run_20260312T120500Z_d4c3b2a1",
    )
    mock_facade.resume_run.assert_not_awaited()
    mock_facade.promote_run_skills.assert_awaited_once_with(
        "desktop:s1",
        mode_id="analyst",
        run_id="run_20260312T120500Z_d4c3b2a1",
    )
    assert panel.last_action_label.text() in {
        "Doctor выполнен.",
        "Recover подготовлен.",
        "Resume подготовлен.",
        "Skills promoted to global: playwright-cli",
    }


@pytest.mark.asyncio
async def test_run_operations_panel_renders_apply_recommendation_for_codebase_mapper(qtbot):
    facade = MagicMock()
    facade.subscribe.side_effect = lambda callback: (lambda: None)
    facade.list_runs.return_value = [
        {
            "session_uid": "desktop:s1",
            "mode_id": "codebase_mapper",
            "run_id": "run_20260312T120500Z_mapper",
            "status": "failed",
            "phase": "operation",
            "active": False,
            "recommended_action": "run_validate",
            "can_recover": False,
            "can_resume": False,
            "can_apply_recommendation": True,
            "issue_codes": ["boundary_contract_failed"],
            "skill_log": [],
            "project_local_skill_ids": [],
            "cli_work_type": None,
            "executor_profile": None,
        }
    ]
    facade.doctor_run = AsyncMock(return_value={"message": "Doctor выполнен."})
    facade.recover_run = AsyncMock(return_value={"message": "Recover подготовлен."})
    facade.resume_run = AsyncMock(return_value={"message": "Resume подготовлен."})
    facade.apply_recommendation_run = AsyncMock(return_value={"message": "Validate operation executed."})
    facade.promote_run_skills = AsyncMock(return_value={"message": "Skills promoted."})

    panel = RunOperationsPanelWidget(facade, session_uid="desktop:s1")
    qtbot.addWidget(panel)

    def _ensure_async(coro, parent=None):
        task = asyncio.get_running_loop().create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda current: parent._background_tasks.discard(current))
        return task

    with patch("desktop.widgets.run_operations_panel.ensure_async", side_effect=_ensure_async):
        buttons = panel.findChildren(QPushButton)
        recover_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_recover"))
        resume_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_resume"))
        apply_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_validate"))
        assert recover_button.isEnabled() is False
        assert resume_button.isEnabled() is False
        assert apply_button.isEnabled() is True
        apply_button.click()
        await asyncio.sleep(0)

    facade.apply_recommendation_run.assert_awaited_once_with(
        "desktop:s1",
        mode_id="codebase_mapper",
        run_id="run_20260312T120500Z_mapper",
    )
    assert panel.last_action_label.text() == "Validate operation executed."


def test_run_operations_panel_applies_policy_metadata_to_buttons(qtbot):
    facade = MagicMock()
    facade.subscribe.side_effect = lambda callback: (lambda: None)
    facade.list_runs.return_value = [
        {
            "session_uid": "desktop:s1",
            "mode_id": "codebase_mapper",
            "run_id": "run_20260312T120500Z_policy",
            "status": "failed",
            "phase": "operation",
            "active": False,
            "recommended_action": "run_validate",
            "can_recover": True,
            "can_resume": True,
            "can_apply_recommendation": True,
            "issue_codes": ["boundary_contract_failed"],
            "skill_log": [],
            "project_local_skill_ids": ["playwright-cli"],
            "cli_work_type": None,
            "executor_profile": None,
            "run_operations_policy": {
                "doctor": {"allowed": True, "reason": "owner_allowed", "visibility": "show"},
                "recover": {"allowed": False, "reason": "admin_required", "visibility": "hide"},
                "resume": {"allowed": False, "reason": "admin_required", "visibility": "disable"},
                "apply_recommendation": {"allowed": False, "reason": "admin_required", "visibility": "hide"},
                "promote_skills": {"allowed": False, "reason": "admin_required", "visibility": "hide"},
            },
        }
    ]

    panel = RunOperationsPanelWidget(facade, session_uid="desktop:s1")
    qtbot.addWidget(panel)

    buttons_by_text = {button.text(): button for button in panel.findChildren(QPushButton)}

    assert buttons_by_text[_btn_label("desktop.runops.btn_doctor")].isEnabled() is True
    assert _btn_label("desktop.runops.btn_recover") not in buttons_by_text
    assert buttons_by_text[_btn_label("desktop.runops.btn_resume")].isEnabled() is False
    assert _btn_label("desktop.runops.btn_validate") not in buttons_by_text
    assert _btn_label("desktop.runops.btn_promote_skills") not in buttons_by_text


@pytest.mark.asyncio
async def test_run_operations_panel_disables_resume_and_recover_for_superseded_run(qtbot):
    facade = MagicMock()
    facade.subscribe.side_effect = lambda callback: (lambda: None)
    facade.list_runs.return_value = [
        {
            "session_uid": "desktop:s1",
            "mode_id": "manager",
            "run_id": "run_20260312T120500Z_superseded",
            "status": "superseded",
            "phase": "complete",
            "active": False,
            "terminal_status": True,
            "terminal_actions_blocked": True,
            "recommended_action": "replay_finalize",
            "can_recover": False,
            "can_resume": False,
            "issue_codes": ["boundary_contract_failed"],
            "skill_log": [],
            "project_local_skill_ids": [],
            "cli_work_type": None,
            "executor_profile": None,
        }
    ]
    facade.doctor_run = AsyncMock(return_value={"message": "Doctor выполнен."})
    facade.recover_run = AsyncMock(return_value={"message": "Recover подготовлен."})
    facade.resume_run = AsyncMock(return_value={"message": "Resume подготовлен."})
    facade.promote_run_skills = AsyncMock(return_value={"message": "Skills promoted."})

    panel = RunOperationsPanelWidget(facade, session_uid="desktop:s1")
    qtbot.addWidget(panel)

    def _ensure_async(coro, parent=None):
        task = asyncio.get_running_loop().create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda current: parent._background_tasks.discard(current))
        return task

    with patch("desktop.widgets.run_operations_panel.ensure_async", side_effect=_ensure_async):
        buttons = panel.findChildren(QPushButton)
        recover_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_recover"))
        resume_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_resume"))

        assert recover_button.isEnabled() is False
        assert resume_button.isEnabled() is False

        recover_button.click()
        resume_button.click()
        await asyncio.sleep(0)

    facade.recover_run.assert_not_awaited()
    facade.resume_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_operations_panel_blocks_superseded_run_even_without_backend_terminal_flag(qtbot):
    facade = MagicMock()
    facade.subscribe.side_effect = lambda callback: (lambda: None)
    facade.list_runs.return_value = [
        {
            "session_uid": "desktop:s1",
            "mode_id": "manager",
            "run_id": "run_20260312T120501Z_superseded_fallback",
            "status": "superseded",
            "phase": "complete",
            "active": False,
            "terminal_status": True,
            "terminal_actions_blocked": False,
            "recommended_action": "replay_finalize",
            "can_recover": True,
            "can_resume": True,
            "issue_codes": ["boundary_contract_failed"],
            "skill_log": [],
            "project_local_skill_ids": [],
            "cli_work_type": None,
            "executor_profile": None,
        }
    ]
    facade.doctor_run = AsyncMock(return_value={"message": "Doctor выполнен."})
    facade.recover_run = AsyncMock(return_value={"message": "Recover подготовлен."})
    facade.resume_run = AsyncMock(return_value={"message": "Resume подготовлен."})
    facade.promote_run_skills = AsyncMock(return_value={"message": "Skills promoted."})

    panel = RunOperationsPanelWidget(facade, session_uid="desktop:s1")
    qtbot.addWidget(panel)

    buttons = panel.findChildren(QPushButton)
    recover_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_recover"))
    resume_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_resume"))

    assert recover_button.isEnabled() is False
    assert resume_button.isEnabled() is False


@pytest.mark.asyncio
async def test_run_operations_panel_keeps_failed_run_resume_and_recover_semantics(qtbot):
    facade = MagicMock()
    facade.subscribe.side_effect = lambda callback: (lambda: None)
    facade.list_runs.return_value = [
        {
            "session_uid": "desktop:s1",
            "mode_id": "manager",
            "run_id": "run_20260312T120500Z_failed",
            "status": "failed",
            "phase": "complete",
            "active": False,
            "terminal_status": True,
            "terminal_actions_blocked": False,
            "recommended_action": "restart_from_phase",
            "can_recover": True,
            "can_resume": True,
            "issue_codes": ["boundary_contract_failed"],
            "skill_log": [],
            "project_local_skill_ids": [],
            "cli_work_type": None,
            "executor_profile": None,
        }
    ]
    facade.doctor_run = AsyncMock(return_value={"message": "Doctor выполнен."})
    facade.recover_run = AsyncMock(return_value={"message": "Recover подготовлен."})
    facade.resume_run = AsyncMock(return_value={"message": "Resume подготовлен."})
    facade.promote_run_skills = AsyncMock(return_value={"message": "Skills promoted."})

    panel = RunOperationsPanelWidget(facade, session_uid="desktop:s1")
    qtbot.addWidget(panel)

    def _ensure_async(coro, parent=None):
        task = asyncio.get_running_loop().create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda current: parent._background_tasks.discard(current))
        return task

    with patch("desktop.widgets.run_operations_panel.ensure_async", side_effect=_ensure_async):
        buttons = panel.findChildren(QPushButton)
        recover_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_recover"))
        resume_button = next(btn for btn in buttons if btn.text() == _btn_label("desktop.runops.btn_resume"))

        assert recover_button.isEnabled() is True
        assert resume_button.isEnabled() is True

        recover_button.click()
        await asyncio.sleep(0)
        resume_button.click()
        await asyncio.sleep(0)

    facade.recover_run.assert_awaited_once_with(
        "desktop:s1",
        mode_id="manager",
        run_id="run_20260312T120500Z_failed",
    )
    facade.resume_run.assert_awaited_once_with(
        "desktop:s1",
        mode_id="manager",
        run_id="run_20260312T120500Z_failed",
    )
