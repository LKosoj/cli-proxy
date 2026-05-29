import json
from pathlib import Path

import pytest

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.webmaster.models import FeedbackDecision
from modes.webmaster.state_store import build_user_key


def _build_app(tmp_path, *, max_iterations: int = 2) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            openai_api_key="k",
            openai_model="m",
            openai_big_model="m-big",
            run_artifacts_enabled=True,
            run_doctor_enabled=True,
            run_boundary_validation_enabled=True,
            run_metrics_enabled=True,
            webmaster_validation_max_fix_iterations=max_iterations,
            webmaster_use_cli_timeout_sec=42,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def _prepare_session(app: BotApp, tmp_path):
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = "webmaster"
    return session


def _run_store(app: BotApp) -> RunArtifactStore:
    return RunArtifactStore(app.config)


def _patch_run_ids(monkeypatch: pytest.MonkeyPatch, run_ids: list[str]) -> None:
    iterator = iter(run_ids)
    last = run_ids[-1]

    def _next_run_id() -> str:
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            return last
        return last

    monkeypatch.setattr("app.services.run_artifact_store._sortable_run_id", _next_run_id)


def _install_webmaster_stubs(mode, outputs: list[str]) -> None:
    async def _classify(*_args, **_kwargs):
        return FeedbackDecision(kind="new_task", reason="ok")

    async def _analyze(**_kwargs):
        return {
            "goal": "Обновить сайт",
            "actions": ["Обновить hero", "Проверить адаптив"],
            "constraints": ["Не ломать остальной UI"],
            "acceptance_criteria": ["Hero обновлён", "Чеклист валидатора PASS"],
            "ambiguities": [],
            "assumptions": [],
        }

    async def _confirm(*_args, **_kwargs):
        return "Подтвердить"

    queue = list(outputs)

    async def _use_cli(_bot_app, _session, _context, _dest, _task_text, *, fresh_run):
        assert queue, "unexpected extra use_cli call"
        return str(queue.pop(0))

    async def _silent_git_checkpoint(_session, _label):
        return False

    def _build_cli_task(_wm_ctx, *, session=None):
        _ = session
        return "webmaster-dev-task"

    def _build_validation_task(_wm_ctx, _developer_report, *, session=None):
        _ = session
        return "webmaster-validation-task"

    def _build_fix_task(_wm_ctx, _decision, iteration, _max_iterations, *, session=None):
        _ = session
        return f"webmaster-fix-task-{iteration}"

    mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
    mode._analyze_intent = _analyze  # type: ignore[method-assign]
    mode._confirm_intent = _confirm  # type: ignore[method-assign]
    mode._run_use_cli = _use_cli  # type: ignore[method-assign]
    mode._silent_git_checkpoint = _silent_git_checkpoint  # type: ignore[method-assign]
    mode._build_cli_task = _build_cli_task  # type: ignore[method-assign]
    mode._build_validation_task = _build_validation_task  # type: ignore[method-assign]
    mode._build_fix_task = _build_fix_task  # type: ignore[method-assign]


def _dev_report(label: str) -> str:
    return (
        f"Отчет {label}\n\n"
        "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| Семантический HTML | PASS | checked {label} | updated {label} | |\n"
    )


def _validation_payload(*, status: str, row_status: str, evidence: str) -> str:
    return json.dumps(
        {
            "status": status,
            "summary": "ok" if status == "PASS" else "needs fix",
            "blocking_issues": [],
            "checklist_results": [
                {
                    "item": "Семантический HTML",
                    "status": row_status,
                    "evidence": evidence,
                    "fixed": "",
                    "why_not_done": "" if row_status == "PASS" else "needs more work",
                }
            ],
            "defects": [],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_webmaster_checkpoint_tracking_advances_with_loop_progression(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T231100Z_webmaster_success001"])
    app = _build_app(tmp_path, max_iterations=2)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("webmaster")
    assert mode is not None
    _install_webmaster_stubs(
        mode,
        [
            _dev_report("iter1"),
            _validation_payload(status="PASS", row_status="PARTIAL", evidence="blocked iter1"),
            _dev_report("iter2"),
            _validation_payload(status="PASS", row_status="PASS", evidence="checked iter2"),
        ],
    )

    result = await mode.run_pipeline(
        session=session,
        user_text="Обнови landing page и проверь адаптив",
        bot_app=app,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1, "user_id": 2, "chat_type": "private"},
    )

    assert result.startswith("✅ Задача выполнена и валидация пройдена.")
    store = _run_store(app)
    run = store.latest_run(session=session, mode_id="webmaster")
    assert run is not None

    assert Path(run.state_path).exists()
    assert Path(run.plan_path).exists()
    assert Path(run.checkpoints_path).exists()
    assert Path(run.recovery_path).exists()
    assert Path(run.metrics_path).exists()
    assert Path(run.events_path).exists()

    state = store.load_state(run)
    assert state["status"] == "completed"
    assert state["phase"] == "complete"
    assert state["mode_context"]["webmaster_user_key"] == build_user_key(1, 2, session.id)
    assert state["mode_context"]["structured_report"]["status"] == "PASS"
    assert state["mode_context"]["validation_report"]["gate"]["passed"] is True

    checkpoints = json.loads(Path(run.checkpoints_path).read_text(encoding="utf-8"))
    assert [(item["phase"], item["status"]) for item in checkpoints["items"]] == [
        ("dev", "started"),
        ("dev", "ok"),
        ("validation", "started"),
        ("validation", "failed"),
        ("dev", "started"),
        ("dev", "ok"),
        ("validation", "started"),
        ("validation", "ok"),
    ]
    assert [item["iteration"] for item in checkpoints["items"]] == [0, 0, 0, 0, 1, 1, 1, 1]

    validator = RunBoundaryValidationService(enabled=True)
    report = validator.validate(run, mode_id="webmaster", phase="complete")
    assert report.status == "ok"


@pytest.mark.asyncio
async def test_webmaster_failed_gate_translates_into_boundary_rejection(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T231200Z_webmaster_failed001"])
    app = _build_app(tmp_path, max_iterations=1)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("webmaster")
    assert mode is not None
    _install_webmaster_stubs(
        mode,
        [
            _dev_report("single"),
            _validation_payload(status="PASS", row_status="PARTIAL", evidence="needs manual fix"),
            _dev_report("retry"),
            _validation_payload(status="PASS", row_status="PARTIAL", evidence="still failing"),
        ],
    )

    result = await mode.run_pipeline(
        session=session,
        user_text="Сделай задачу и пройди валидацию",
        bot_app=app,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1, "user_id": 2, "chat_type": "private"},
    )

    assert result.startswith("❌ Не удалось пройти валидацию")
    store = _run_store(app)
    run = store.latest_run(session=session, mode_id="webmaster")
    assert run is not None
    state = store.load_state(run)
    assert state["status"] == "failed"
    assert state["phase"] == "validation"
    assert state["mode_context"]["validation_report"]["gate"]["passed"] is False

    validator = RunBoundaryValidationService(enabled=True)
    report = validator.validate(run, mode_id="webmaster", phase="validation")
    assert report.status == "error"
    issue_codes = {issue.code for issue in report.issues}
    assert "webmaster_gate_failed" in issue_codes


@pytest.mark.asyncio
async def test_webmaster_recover_run_replay_finalize_keeps_live_store_isolated(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T231250Z_webmaster_recover003"])
    app = _build_app(tmp_path, max_iterations=1)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("webmaster")
    assert mode is not None
    store = mode._store(session)  # type: ignore[attr-defined]
    user_key = build_user_key(1, 2, session.id)
    wm_ctx = store.load(user_key)
    wm_ctx.task_kind = "continue_task"
    wm_ctx.stage = "await_intent_update"
    wm_ctx.goal = "Живой user config должен остаться без изменений"
    wm_ctx.last_user_text = "Живой user config должен остаться без изменений"
    wm_ctx.last_cli_task = "live-webmaster-dev-task"
    wm_ctx.last_cli_report = _dev_report("live")
    wm_ctx.last_feedback_class = "live_feedback"
    wm_ctx.last_validation_json = {
        "status": "FAIL",
        "summary": "live state",
        "gate": {"passed": False},
    }
    wm_ctx.last_validation_report = json.dumps(wm_ctx.last_validation_json, ensure_ascii=False)
    store.save(wm_ctx)

    run_store = _run_store(app)
    previous = run_store.start_run(
        session=session,
        mode_id="webmaster",
        run_id="run_20260312T231240Z_webmaster_broken002",
        phase="validation",
        source_prompt_hash="sha256:webmaster-recover",
    )
    validation_report = json.loads(_validation_payload(status="PASS", row_status="PASS", evidence="checked replay"))
    validation_report["gate"] = {
        "passed": True,
        "checklist_table_present": True,
        "invalid_rows": [],
        "non_pass_rows": [],
        "missing_evidence_rows": [],
        "blocking_issue_count": 0,
    }
    run_store.save_plan(
        previous,
        {
            "kind": "webmaster_pipeline",
            "goal": "Исторический snapshot для replay finalize",
            "task_kind": "new_task",
            "units": [
                {"id": "webmaster:dev", "step_type": "dev"},
                {"id": "webmaster:validation", "step_type": "validation"},
            ],
        },
    )
    run_store.save_state(
        previous,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "webmaster_user_key": user_key,
                "task_kind": "new_task",
                "goal": "Исторический snapshot для replay finalize",
                "last_user_text": "Исторический snapshot для replay finalize",
                "last_cli_task": "historical-webmaster-dev-task",
                "developer_report": _dev_report("replay"),
                "validation_report": dict(validation_report),
                "execution_context": {
                    "dest_kind": "telegram",
                    "chat_id": 1,
                    "user_id": 2,
                    "chat_type": "private",
                },
            },
        },
    )
    monkeypatch.setattr(session, "is_active_by_tick", lambda: False)

    result = await app.mode_run_operations.recover_run(
        session=session,
        mode_id="webmaster",
        run_id=previous.run_id,
    )

    previous_recovery = json.loads(Path(previous.recovery_path).read_text(encoding="utf-8"))
    latest = run_store.latest_run(session=session, mode_id="webmaster")
    assert latest is not None
    latest_state = run_store.load_state(latest)
    live_ctx_after = store.load(user_key)

    assert result.status == "ok"
    assert result.recommended_action == "replay_finalize"
    assert latest.run_id == "run_20260312T231250Z_webmaster_recover003"
    assert latest_state["status"] == "completed"
    assert latest_state["phase"] == "complete"
    assert latest_state["mode_context"]["goal"] == "Исторический snapshot для replay finalize"
    assert latest_state["mode_context"]["last_user_text"] == "Исторический snapshot для replay finalize"
    assert latest_state["mode_context"]["last_cli_task"] == "historical-webmaster-dev-task"
    assert latest_state["mode_context"]["structured_report"]["status"] == "PASS"
    assert latest_state["mode_context"]["validation_report"]["gate"]["passed"] is True
    assert live_ctx_after.goal == "Живой user config должен остаться без изменений"
    assert live_ctx_after.last_user_text == "Живой user config должен остаться без изменений"
    assert live_ctx_after.last_cli_task == "live-webmaster-dev-task"
    assert live_ctx_after.last_feedback_class == "live_feedback"
    assert live_ctx_after.last_validation_json["gate"]["passed"] is False
    previous_state = run_store.load_state(previous)
    assert previous_state["status"] == "superseded"
    assert previous_recovery["last_requested_operation"]["executed_operation"] == "replay_finalize"
    assert previous_recovery["last_requested_operation"]["executed_via"] == "webmaster_replay_finalize"
    assert previous_recovery["last_requested_operation"]["spawned_run_id"] == latest.run_id


@pytest.mark.asyncio
async def test_webmaster_run_artifacts_isolate_sequential_runs_with_different_prompts(tmp_path, monkeypatch) -> None:
    _patch_run_ids(
        monkeypatch,
        [
            "run_20260312T231300Z_webmaster_first001",
            "run_20260312T231301Z_webmaster_second002",
        ],
    )
    app = _build_app(tmp_path, max_iterations=0)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("webmaster")
    assert mode is not None
    _install_webmaster_stubs(
        mode,
        [
            _dev_report("first"),
            _validation_payload(status="PASS", row_status="PASS", evidence="checked first"),
            _dev_report("second"),
            _validation_payload(status="PASS", row_status="PASS", evidence="checked second"),
        ],
    )
    store = _run_store(app)

    await mode.run_pipeline(
        session=session,
        user_text="Сначала обнови hero",
        bot_app=app,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1, "user_id": 2, "chat_type": "private"},
    )
    first = store.latest_run(session=session, mode_id="webmaster")
    assert first is not None
    first_state = store.load_state(first)

    await mode.run_pipeline(
        session=session,
        user_text="Потом проверь футер и адаптив",
        bot_app=app,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1, "user_id": 2, "chat_type": "private"},
    )
    second = store.latest_run(session=session, mode_id="webmaster")
    assert second is not None
    second_state = store.load_state(second)

    assert first.run_id != second.run_id
    assert first_state["source_prompt_hash"] != second_state["source_prompt_hash"]
    assert "checked first" in first_state["mode_context"]["validation_report"]["checklist_rows"][0]["evidence"]
    assert "checked second" in second_state["mode_context"]["validation_report"]["checklist_rows"][0]["evidence"]
    assert "first" in first_state["mode_context"]["structured_report"]["developer_report"]
    assert "second" in second_state["mode_context"]["structured_report"]["developer_report"]
