import types

from app.services.run_recovery_executor import build_recovery_prompt


def test_build_recovery_prompt_uses_action_specific_sources_for_analyst_and_agent() -> None:
    analyst_state = {
        "mode_context": {
            "source_user_text": "Полный исходный аналитический запрос",
            "input_bundle": {
                "original_user_text": "Полный исходный аналитический запрос",
                "clarification_answers": [],
                "recovery_prompt_text": "Полный исходный аналитический запрос",
            },
            "execution_context": {
                "user_text_preview": "Исходный аналитический запрос",
                "analyst_prompt_preview": "Расширенный analyst prompt",
                "runner_prompt_preview": "Runner analyst prompt",
            }
        }
    }
    agent_state = {
        "mode_context": {
            "source_prompt": "Исходный агентный запрос",
            "execution_context": {
                "runner_prompt_preview": "Runner agent prompt",
                "user_text_preview": "Fallback agent user text",
            },
        }
    }

    session = types.SimpleNamespace(workdir="/tmp/recovery")

    assert (
        build_recovery_prompt(
            session=session,
            mode_id="analyst",
            action="rollback_to_checkpoint",
            state=analyst_state,
        )
        == "Полный исходный аналитический запрос"
    )
    assert (
        build_recovery_prompt(
            session=session,
            mode_id="analyst",
            action="restart_from_phase",
            state=analyst_state,
        )
        == "Полный исходный аналитический запрос"
    )
    assert (
        build_recovery_prompt(
            session=session,
            mode_id="agent",
            action="rollback_to_checkpoint",
            state=agent_state,
        )
        == "Исходный агентный запрос"
    )
    assert (
        build_recovery_prompt(
            session=session,
            mode_id="agent",
            action="restart_from_phase",
            state=agent_state,
        )
        == "Runner agent prompt"
    )


def test_build_recovery_prompt_uses_action_specific_sources_for_webmaster() -> None:
    state = {
        "mode_context": {
            "last_user_text": "Почини форму логина",
            "last_cli_task": "Перезапусти проверку playwright",
            "execution_context": {
                "last_user_text_preview": "Почини форму логина",
                "last_cli_task_preview": "Перезапусти проверку playwright",
            },
            "intent_payload": {
                "goal": "Сделай сайт снова рабочим",
            },
        }
    }
    session = types.SimpleNamespace(workdir="/tmp/recovery")

    assert (
        build_recovery_prompt(
            session=session,
            mode_id="webmaster",
            action="rollback_to_checkpoint",
            state=state,
        )
        == "Почини форму логина"
    )
    assert (
        build_recovery_prompt(
            session=session,
            mode_id="webmaster",
            action="restart_from_phase",
            state=state,
        )
        == "Перезапусти проверку playwright"
    )


def test_build_recovery_prompt_prefers_manager_source_preview_over_expanded_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.run_recovery_executor.load_plan",
        lambda _workdir, **_kwargs: types.SimpleNamespace(status="completed", project_goal="Собери менеджерский план"),
    )
    session = types.SimpleNamespace(workdir="/tmp/recovery")
    state = {
        "mode_context": {
            "source_user_text_preview": "Собери менеджерский план",
            "prompt_preview": (
                "Собери менеджерский план\n\n"
                "<CODEBASE_MAP>\nexpanded map payload\n</CODEBASE_MAP>\n\n"
                "tail"
            ),
        }
    }

    assert (
        build_recovery_prompt(
            session=session,
            mode_id="manager",
            action="restart_from_phase",
            state=state,
        )
        == "Собери менеджерский план"
    )
