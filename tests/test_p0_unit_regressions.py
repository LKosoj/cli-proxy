import asyncio
import json
import types

import pytest

from modes.webmaster.mode import WebmasterMode
from modes.webmaster.models import ValidationDecision
from sessions.session_run_service import SessionRunService


def test_p0_webmaster_gate_passed_rejects_partial_even_with_evidence() -> None:
    mode = WebmasterMode()
    decision = ValidationDecision(
        status="PASS",
        summary="ok",
        blocking_issues=[],
        checklist_rows=[
            {
                "item": "ARIA",
                "status": "PASS",
                "evidence": "axe ok",
                "fixed": "labels added",
                "why_not_done": "",
            },
            {
                "item": "Keyboard navigation",
                "status": "PARTIAL",
                "evidence": "manual test",
                "fixed": "partially fixed",
                "why_not_done": "one focus trap left",
            },
        ],
        defects=[],
        raw={},
    )
    developer_report = (
        "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| ARIA | PASS | axe ok | labels added | |\n"
        "| Keyboard navigation | PARTIAL | manual test | partially fixed | one focus trap left |\n"
    )

    assert mode._gate_passed(decision, developer_report) is False


@pytest.mark.asyncio
async def test_p0_manager_handoff_stores_full_orchestrator_output_not_plan_summary_only() -> None:
    full_report = json.dumps(
        {
            "status": "ok",
            "summary": "Полный отчёт",
            "_plan_summary": "short summary only",
            "tests": [{"command": "pytest -q", "result": "passed"}],
        },
        ensure_ascii=False,
    )

    class _ManagerMode:
        async def run_pipeline(self, *, session, user_text: str, bot_app, context, dest):
            _ = session, user_text, bot_app, context, dest
            return full_report

        def framework_sends_output(self) -> bool:
            return False

    class _ModeRegistry:
        def __init__(self, mode):
            self._mode = mode

        def get(self, mode_id):
            return self._mode if str(mode_id) == "manager" else None

    session = types.SimpleNamespace(
        id="s1",
        run_lock=asyncio.Lock(),
        queue=[],
        busy=False,
        started_at=0.0,
        last_output_ts=0.0,
        last_tick_ts=None,
        last_tick_value=None,
        tick_seen=0,
        active_mode="manager",
        advanced_orchestrator_enabled=False,
        orchestrator_pending_input=None,
        orchestrator_last_mode_output=None,
        orchestrator_last_mode_id=None,
        modes=types.SimpleNamespace(active_mode="manager", analyst_mode="spec"),
        orchestrator=types.SimpleNamespace(
            enabled=False,
            pending_input=None,
            last_mode_output=None,
            last_mode_id=None,
        ),
        state_summary=None,
        state_updated_at=None,
    )
    bot_app = types.SimpleNamespace(
        mode_registry=_ModeRegistry(_ManagerMode()),
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=1000)),
        send_output=(lambda *_a, **_k: asyncio.sleep(0)),
        _send_message=(lambda *_a, **_k: asyncio.sleep(0)),
    )
    svc = SessionRunService(
        bot_app=bot_app,
        persist_sessions=(lambda: None),
        mode_tasks_list=(lambda **_kwargs: []),
        mode_tasks_create=(lambda **_kwargs: None),
        log_cli_dialog=(lambda *_args, **_kwargs: None),
        reset_session_fields_like_sessions_reset=(lambda *_args, **_kwargs: None),
    )

    await svc.run_mode_pipeline(session, "run", {"chat_id": 1}, context=None, mode_id="manager")

    payload = json.loads(str(session.orchestrator.last_mode_output or ""))
    assert session.orchestrator.last_mode_output == full_report
    assert payload.get("summary") == "Полный отчёт"
    assert payload.get("_plan_summary") == "short summary only"
    assert session.orchestrator.last_mode_output != payload.get("_plan_summary")
