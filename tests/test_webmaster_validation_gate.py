import asyncio
import json
import types

import pytest
import yaml

from app.services.project_prompts_service import ensure_project_prompts
from modes.webmaster.mode import WebmasterMode
from modes.webmaster.models import FeedbackDecision, ValidationDecision, WebmasterContext
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.webmaster.schemas import WebmasterValidationReportSchema, validate_webmaster_payload
from modes.webmaster.state_store import build_user_key


def _build_bot_app(max_iterations: int = 2):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                webmaster_use_cli_timeout_sec=42,
                webmaster_validation_max_fix_iterations=max_iterations,
            )
        ),
        _tool_registry=types.SimpleNamespace(),
    )


def test_webmaster_validation_pass_returns_success(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]
        mode._prompts = {"validation_task": "Ты валидатор: не изменяй файлы, только проверяй."}

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        async def _analyze(**_kwargs):
            return {"goal": "g", "actions": ["a"], "constraints": [], "acceptance_criteria": ["ok"]}

        async def _confirm(*_args, **_kwargs):
            return "Подтвердить"

        calls = {"n": 0}

        async def _use_cli(_bot_app, _session, _context, _dest, task_text, *, fresh_run):
            calls["n"] += 1
            if calls["n"] == 1:
                assert fresh_run is True
                return (
                    "Отчет\n\n"
                    "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    "| Семантический HTML | PASS | checked | updated | |\n"
                )
            assert "Ты валидатор: не изменяй файлы, только проверяй." in task_text
            assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.JSON_OBJECT}" in task_text
            return json.dumps(
                {
                    "status": "PASS",
                    "summary": "ok",
                    "blocking_issues": [],
                    "checklist_results": [
                        {
                            "item": "Семантический HTML",
                            "status": "PASS",
                            "evidence": "checked",
                            "fixed": "updated",
                            "why_not_done": "",
                        }
                    ],
                    "defects": [],
                },
                ensure_ascii=False,
            )

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
        mode._analyze_intent = _analyze  # type: ignore[method-assign]
        mode._confirm_intent = _confirm  # type: ignore[method-assign]
        mode._run_use_cli = _use_cli  # type: ignore[method-assign]

        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=_build_bot_app(),
            context=None,
            dest={"chat_id": 1, "user_id": 2, "chat_type": "private"},
        )

        assert out.startswith("✅ Задача выполнена и валидация пройдена.")
        saved = mode._store(session).load(build_user_key(1, 2, "s1"))
        assert saved.stage == "await_feedback"
        assert str(saved.last_validation_json.get("status")) == "PASS"

    asyncio.run(_run())


def test_webmaster_validation_fail_returns_final_failure(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]
        mode._prompts = {
            "validation_task": "Ты валидатор: не изменяй файлы, только проверяй.",
            "fix_task": "Исправь только проблемы из fix-пакета валидатора.",
        }

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        async def _analyze(**_kwargs):
            return {"goal": "g", "actions": ["a"], "constraints": [], "acceptance_criteria": ["ok"]}

        async def _confirm(*_args, **_kwargs):
            return "Подтвердить"

        calls = {"n": 0}
        validation_json = json.dumps(
            {
                "status": "FAIL",
                "summary": "bad",
                "blocking_issues": ["A11y failures"],
                "checklist_results": [
                    {
                        "item": "ARIA/доступность",
                        "status": "FAIL",
                        "evidence": "axe errors",
                        "fixed": "",
                        "why_not_done": "aria-label missing",
                    }
                ],
                "defects": [
                    {
                        "severity": "high",
                        "title": "Missing aria-label",
                        "location": "src/ui/button.tsx",
                        "why": "screen reader cannot announce",
                        "fix_hint": "add aria-label",
                    }
                ],
            },
            ensure_ascii=False,
        )

        async def _use_cli(_bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            calls["n"] += 1
            if calls["n"] % 2 == 1:
                return "dev report"
            return validation_json

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
        mode._analyze_intent = _analyze  # type: ignore[method-assign]
        mode._confirm_intent = _confirm  # type: ignore[method-assign]
        mode._run_use_cli = _use_cli  # type: ignore[method-assign]

        session = types.SimpleNamespace(id="s2", workdir=str(tmp_path))
        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=_build_bot_app(max_iterations=1),
            context=None,
            dest={"chat_id": 3, "user_id": 4, "chat_type": "private"},
        )

        assert out.startswith("❌ Не удалось пройти валидацию")
        saved = mode._store(session).load(build_user_key(3, 4, "s2"))
        assert saved.stage == "failed"
        assert saved.last_feedback_class == "validation_failed"

    asyncio.run(_run())


def test_webmaster_gate_rejects_pass_without_checklist_table(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]
        mode._prompts = {
            "validation_task": "Ты валидатор: не изменяй файлы, только проверяй.",
            "fix_task": "Исправь только проблемы из fix-пакета валидатора.",
        }

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        async def _analyze(**_kwargs):
            return {"goal": "g", "actions": ["a"], "constraints": [], "acceptance_criteria": ["ok"]}

        async def _confirm(*_args, **_kwargs):
            return "Подтвердить"

        validation_json = json.dumps(
            {
                "status": "PASS",
                "summary": "looks good",
                "blocking_issues": [],
                "checklist_results": [
                    {
                        "item": "Семантический HTML",
                        "status": "PASS",
                        "evidence": "checked",
                        "fixed": "",
                        "why_not_done": "",
                    }
                ],
                "defects": [],
            },
            ensure_ascii=False,
        )
        calls = {"n": 0}

        async def _use_cli(_bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            calls["n"] += 1
            if calls["n"] % 2 == 1:
                return "report without checklist table"
            return validation_json

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
        mode._analyze_intent = _analyze  # type: ignore[method-assign]
        mode._confirm_intent = _confirm  # type: ignore[method-assign]
        mode._run_use_cli = _use_cli  # type: ignore[method-assign]

        session = types.SimpleNamespace(id="s3", workdir=str(tmp_path))
        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=_build_bot_app(max_iterations=0),
            context=None,
            dest={"chat_id": 5, "user_id": 6, "chat_type": "private"},
        )

        assert out.startswith("❌ Не удалось пройти валидацию")
        saved = mode._store(session).load(build_user_key(5, 6, "s3"))
        assert saved.stage == "failed"
        assert saved.last_feedback_class == "validation_failed"

    asyncio.run(_run())


def test_webmaster_validation_pass_status_with_partial_row_routes_to_failure(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]
        mode._prompts = {
            "validation_task": "Ты валидатор: не изменяй файлы, только проверяй.",
            "fix_task": "Исправь только проблемы из fix-пакета валидатора.",
        }

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        async def _analyze(**_kwargs):
            return {"goal": "g", "actions": ["a"], "constraints": [], "acceptance_criteria": ["ok"]}

        async def _confirm(*_args, **_kwargs):
            return "Подтвердить"

        validation_payload = {
            "status": "PASS",
            "summary": "looks good",
            "blocking_issues": [],
            "checklist_results": [
                {
                    "item": "ARIA/доступность",
                    "status": "PARTIAL",
                    "evidence": "axe report checked",
                    "fixed": "partially fixed",
                    "why_not_done": "remaining keyboard trap",
                }
            ],
            "defects": [],
        }
        validate_webmaster_payload(
            validation_payload,
            WebmasterValidationReportSchema,
            contract="validation_report",
        )
        validation_json = json.dumps(validation_payload, ensure_ascii=False)
        calls = {"n": 0}

        async def _use_cli(_bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            calls["n"] += 1
            if calls["n"] % 2 == 1:
                return (
                    "Отчет\n\n"
                    "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    "| ARIA/доступность | PARTIAL | axe report checked | partially fixed | remaining keyboard trap |\n"
                )
            return validation_json

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
        mode._analyze_intent = _analyze  # type: ignore[method-assign]
        mode._confirm_intent = _confirm  # type: ignore[method-assign]
        mode._run_use_cli = _use_cli  # type: ignore[method-assign]

        session = types.SimpleNamespace(id="s3_partial", workdir=str(tmp_path))
        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=_build_bot_app(max_iterations=0),
            context=None,
            dest={"chat_id": 15, "user_id": 16, "chat_type": "private"},
        )

        assert out.startswith("❌ Не удалось пройти валидацию")
        saved = mode._store(session).load(build_user_key(15, 16, "s3_partial"))
        assert saved.stage == "failed"
        assert saved.last_feedback_class == "validation_failed"

    asyncio.run(_run())


def test_webmaster_gate_rejects_pass_when_any_checklist_row_is_partial() -> None:
    mode = WebmasterMode()
    decision = ValidationDecision(
        status="PASS",
        summary="ok",
        blocking_issues=[],
        checklist_rows=[
            {
                "item": "Семантический HTML",
                "status": "PARTIAL",
                "evidence": "checked",
                "fixed": "",
                "why_not_done": "не закрыты все критерии",
            }
        ],
        defects=[],
        raw={"status": "PASS"},
    )
    developer_report = (
        "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Семантический HTML | PARTIAL | checked | | не закрыты все критерии |\n"
    )

    assert mode._gate_passed(decision, developer_report) is False


def test_webmaster_fix_task_includes_problematic_checklist_rows(tmp_path) -> None:
    mode = WebmasterMode()
    ensure_project_prompts(str(tmp_path))
    prompts_path = tmp_path / ".cli-proxy" / ".webmaster" / "prompt" / "prompts.yaml"
    payload = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}
    prompts = payload.get("prompts") if isinstance(payload, dict) else {}
    if not isinstance(prompts, dict):
        prompts = {}
    prompts["fix_task"] = "Исправь только проблемы из fix-пакета валидатора."
    prompts_path.write_text(
        yaml.safe_dump({"prompts": prompts}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    session = types.SimpleNamespace(workdir=str(tmp_path))
    wm_ctx = WebmasterContext(key="k", goal="g")
    decision = ValidationDecision(
        status="FAIL",
        summary="bad",
        blocking_issues=[],
        checklist_rows=[
            {
                "item": "ARIA/доступность",
                "status": "FAIL",
                "evidence": "",
                "fixed": "",
                "why_not_done": "aria-label missing",
            }
        ],
        defects=[],
        raw={"status": "FAIL"},
    )

    task = mode._build_fix_task(wm_ctx, decision, iteration=1, max_iterations=2, session=session)

    assert "Проблемные пункты чеклиста:" in task
    assert "item=ARIA/доступность" in task
    assert "status=FAIL" in task
    assert "why_not_done=aria-label missing" in task


def test_webmaster_parse_validation_report_tolerates_control_chars_in_string() -> None:
    mode = WebmasterMode()
    raw = (
        '{'
        '"status":"FAIL",'
        '"summary":"line1\nline2",'
        '"blocking_issues":["A11y"],'
        '"checklist_results":[],'
        '"defects":[]'
        '}'
    )

    decision = mode._parse_validation_report(raw)
    assert decision.status == "FAIL"
    assert decision.summary == "line1\nline2"


def test_webmaster_parse_validation_report_uses_normalizer_schema_defaults() -> None:
    mode = WebmasterMode()
    raw = (
        "report text\n"
        "```json\n"
        '{"status":"PASS","summary":"ok","blocking_issues":[],"checklist_results":[{"item":"html","status":"PASS"}],"defects":[]}'
        "\n```"
    )

    decision = mode._parse_validation_report(raw)

    assert decision.status == "PASS"
    assert decision.summary == "ok"
    assert len(decision.checklist_rows) == 1
    assert decision.checklist_rows[0]["item"] == "html"
    assert decision.checklist_rows[0]["status"] == "PASS"
    assert decision.checklist_rows[0]["evidence"] == ""
    assert decision.checklist_rows[0]["fixed"] == ""
    assert decision.checklist_rows[0]["why_not_done"] == ""


def test_webmaster_parse_validation_report_logs_exception_for_malformed_with_normalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mode = WebmasterMode()
    logged: list[str] = []

    def _fake_exception(msg, *args, **kwargs):  # noqa: ANN001, ARG001
        logged.append(str(msg))

    monkeypatch.setattr(mode._log, "exception", _fake_exception)

    with pytest.raises(RuntimeError, match="validation output JSON parse failed"):
        mode._parse_validation_report("not a json report")

    assert any("webmaster validation report normalize/parse failed" in msg for msg in logged)


def test_webmaster_parse_validation_report_uses_smart_fallback_for_partial_json() -> None:
    mode = WebmasterMode()
    # Missing required schema fields for strict parser in checklist rows,
    # but fallback should keep valid status and salvage rows.
    raw = (
        '{"status":"PARTIAL","summary":"ok","blocking_issues":["x"],'
        '"checklist_results":[{"item":"a11y","status":"partial","why_not":"missing labels"}],'
        '"defects":[]}'
    )

    decision = mode._parse_validation_report(raw)
    assert decision.status == "PARTIAL"
    assert decision.summary == "ok"
    assert decision.blocking_issues == ["x"]
    assert decision.checklist_rows[0]["item"] == "a11y"
    assert decision.checklist_rows[0]["status"] == "PARTIAL"
    assert decision.checklist_rows[0]["why_not_done"] == "missing labels"


def test_webmaster_checkpoint_before_start_and_before_final_response(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]
        mode._prompts = {"validation_task": "Ты валидатор: не изменяй файлы, только проверяй."}

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        async def _analyze(**_kwargs):
            return {"goal": "g", "actions": ["a"], "constraints": [], "acceptance_criteria": ["ok"]}

        async def _confirm(*_args, **_kwargs):
            return "Подтвердить"

        checkpoints: list[str] = []

        async def _checkpoint(_session, label: str):
            checkpoints.append(str(label))
            return True

        calls = {"n": 0}

        async def _use_cli(_bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            calls["n"] += 1
            if calls["n"] == 1:
                assert fresh_run is True
                return (
                    "Отчет\n\n"
                    "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    "| Семантический HTML | PASS | checked | updated | |\n"
                )
            return json.dumps(
                {
                    "status": "PASS",
                    "summary": "ok",
                    "blocking_issues": [],
                    "checklist_results": [
                        {
                            "item": "Семантический HTML",
                            "status": "PASS",
                            "evidence": "checked",
                            "fixed": "updated",
                            "why_not_done": "",
                        }
                    ],
                    "defects": [],
                },
                ensure_ascii=False,
            )

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
        mode._analyze_intent = _analyze  # type: ignore[method-assign]
        mode._confirm_intent = _confirm  # type: ignore[method-assign]
        mode._run_use_cli = _use_cli  # type: ignore[method-assign]
        mode._silent_git_checkpoint = _checkpoint  # type: ignore[method-assign]

        session = types.SimpleNamespace(id="s4", workdir=str(tmp_path))
        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=_build_bot_app(),
            context=None,
            dest={"chat_id": 7, "user_id": 8, "chat_type": "private"},
        )

        assert out.startswith("✅ Задача выполнена и валидация пройдена.")
        assert checkpoints == ["before_start", "before_response_success"]

    asyncio.run(_run())
