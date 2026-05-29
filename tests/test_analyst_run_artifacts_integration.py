import json
import types
from pathlib import Path

import pytest

from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.analyst.mode import AnalystMode
from modes.analyst.run_directory import AnalystRunDirectory, resolve_analyst_runs_root
from modes.analyst.runner_service import AnalystModeRunnerService
from modes.analyst.state_store import AnalystStateStore
from utils import cli_proxy_artifact_path


class _RunnerBackend:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self.clear_calls: list[str] = []
        self.runner = object()

    async def run(self, session, analyst_prompt: str, _bot_app, _context, dest):
        index = min(len(self.calls), len(self.outputs) - 1)
        output = str(self.outputs[index] if self.outputs else "")
        self.calls.append(
            {
                "prompt": str(analyst_prompt or ""),
                "dest": dict(dest or {}),
                "session_id": str(getattr(session, "id", "") or ""),
            }
        )
        return output

    def clear_session_cache(self, session_id: str) -> None:
        self.clear_calls.append(str(session_id or ""))


class _QualityAwareRunnerBackend(_RunnerBackend):
    def __init__(
        self,
        outputs: list[str],
        *,
        runtime_verdict: str,
        blocking_reasons: list[str] | None = None,
        warning_reasons: list[str] | None = None,
    ) -> None:
        super().__init__(outputs)
        self.runtime_verdict = str(runtime_verdict or "")
        self.blocking_reasons = list(blocking_reasons or [])
        self.warning_reasons = list(warning_reasons or [])

    async def run(self, session, analyst_prompt: str, _bot_app, _context, dest):
        output = await super().run(session, analyst_prompt, _bot_app, _context, dest)
        handle = getattr(session, "analyst_run_artifact_handle", None)
        metrics_path = str(getattr(handle, "metrics_path", "") or "").strip()
        if metrics_path:
            Path(metrics_path).write_text(
                json.dumps(
                    {
                        "analyst_quality": {
                            "runtime_verdict": self.runtime_verdict,
                            "blocking_reasons": list(self.blocking_reasons),
                            "warning_reasons": list(self.warning_reasons),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return output


class _HandleAwareRunnerBackend(_RunnerBackend):
    def __init__(self, outputs: list[str]) -> None:
        super().__init__(outputs)
        self.handle_artifacts: list[str] = []

    async def run(self, session, analyst_prompt: str, _bot_app, _context, dest):
        handle = getattr(session, "analyst_run_artifact_handle", None)
        assert isinstance(handle, RunArtifactHandle)
        self.handle_artifacts.append(str(handle.artifacts_dir))
        Path(handle.artifacts_dir).mkdir(parents=True, exist_ok=True)
        (Path(handle.artifacts_dir) / "runtime-check.md").write_text(
            "runtime artifacts are run-scoped",
            encoding="utf-8",
        )
        Path(handle.metrics_path).write_text(
            json.dumps(
                {
                    "analyst_quality": {
                        "runtime_verdict": "Готово к реализации",
                        "blocking_reasons": [],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return await super().run(session, analyst_prompt, _bot_app, _context, dest)


class _BlockedDeliveryRunnerBackend(_RunnerBackend):
    def __init__(self, outputs: list[str], *, followup_payload: dict) -> None:
        super().__init__(outputs)
        self.followup_payload = dict(followup_payload or {})

    async def run(self, session, analyst_prompt: str, _bot_app, _context, dest):
        handle = getattr(session, "analyst_run_artifact_handle", None)
        assert isinstance(handle, RunArtifactHandle)
        Path(handle.artifacts_dir).mkdir(parents=True, exist_ok=True)
        (Path(handle.artifacts_dir) / "s25_obligation_review_followup.json").write_text(
            json.dumps(self.followup_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return await super().run(session, analyst_prompt, _bot_app, _context, dest)


class _PlaceholderAwareRunnerBackend(_RunnerBackend):
    def __init__(self, outputs: list[str]) -> None:
        super().__init__(outputs)
        self.placeholder_snapshots: list[list[dict]] = []

    async def run(self, session, analyst_prompt: str, _bot_app, _context, dest):
        latest_run = AnalystRunDirectory.latest_run(
            resolve_analyst_runs_root(session),
            session_id=str(getattr(session, "id", "") or ""),
        )
        assert latest_run is not None
        self.placeholder_snapshots.append(list(latest_run.load_meta().get("steps") or []))
        return await super().run(session, analyst_prompt, _bot_app, _context, dest)


class _IncrementalSyncRunnerBackend(_RunnerBackend):
    def __init__(self, outputs: list[str], *, step_results: list[dict]) -> None:
        super().__init__(outputs)
        self.step_results = list(step_results)
        self.synced_snapshots: list[list[dict]] = []

    async def run(self, session, analyst_prompt: str, _bot_app, _context, dest):
        latest_run = AnalystRunDirectory.latest_run(
            resolve_analyst_runs_root(session),
            session_id=str(getattr(session, "id", "") or ""),
        )
        assert latest_run is not None
        sync_hook = getattr(session, "analyst_step_results_sync_hook", None)
        assert callable(sync_hook)
        sync_hook(self.step_results)
        self.synced_snapshots.append(list(latest_run.load_meta().get("steps") or []))
        return await super().run(session, analyst_prompt, _bot_app, _context, dest)


def _build_app(tmp_path, *, run_artifacts_enabled: bool = True) -> BotApp:
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
            run_artifacts_enabled=run_artifacts_enabled,
            run_doctor_enabled=True,
            run_boundary_validation_enabled=True,
            run_metrics_enabled=True,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def _write_templates(path: Path) -> None:
    path.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "Default analyst template"
    required_sections: ["DEFAULT_MARK"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  change_spec:
    name: "Change Spec"
    description: "Repo-grounded change request"
    required_sections: ["CHANGE_SPEC_MARK"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
""",
        encoding="utf-8",
    )


def _install_runtime(app: BotApp, outputs: list[str]) -> _RunnerBackend:
    service = AnalystModeRunnerService(app.config)
    backend = _RunnerBackend(outputs)
    service._runtime = backend
    app.mode_runtime_registry["analyst"] = service
    return backend


def _install_runtime_backend(app: BotApp, backend: _RunnerBackend) -> _RunnerBackend:
    service = AnalystModeRunnerService(app.config)
    service._runtime = backend
    app.mode_runtime_registry["analyst"] = service
    return backend


def _prepare_session(app: BotApp, tmp_path):
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = "analyst"
    session.modes.analyst_mode = "spec"
    session.analyst_template_id = "default"
    session.analyst_runtime_template_id = ""
    return session


def _run_store(app: BotApp) -> RunArtifactStore:
    return RunArtifactStore(app.config)


def _analyst_store(tmp_path) -> AnalystStateStore:
    return AnalystStateStore(cli_proxy_artifact_path(str(tmp_path), ".analyst_data"))


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


def test_analyst_save_run_state_deep_merges_execution_context(tmp_path, monkeypatch) -> None:
    templates_path = tmp_path / "analyst_config.yaml"
    _write_templates(templates_path)
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates_path))
    app = _build_app(tmp_path)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("analyst")
    assert mode is not None

    artifact_store = _run_store(app)
    run = artifact_store.start_run(
        session=session,
        mode_id="analyst",
        run_id="run_20260314T120000Z_mergectx001",
        phase="intent",
        source_prompt_hash="sha256:merge-ctx",
        mode_context={
            "execution_context": {
                "dest_kind": "telegram",
                "user_text_preview": "Исходный analyst prompt",
                "routing": {
                    "chat_id": 1,
                    "thread_id": 77,
                },
            }
        },
    )

    mode._save_run_state(  # type: ignore[attr-defined]
        run,
        phase="execute",
        status="running",
        mode_context={
            "execution_context": {
                "analyst_prompt_preview": "Expanded analyst prompt",
                "routing": {
                    "reply_mode": "thread",
                },
            }
        },
    )

    state = artifact_store.load_state(run)
    execution_context = state["mode_context"]["execution_context"]

    assert execution_context["dest_kind"] == "telegram"
    assert execution_context["user_text_preview"] == "Исходный analyst prompt"
    assert execution_context["analyst_prompt_preview"] == "Expanded analyst prompt"
    assert execution_context["routing"]["chat_id"] == 1
    assert execution_context["routing"]["thread_id"] == 77
    assert execution_context["routing"]["reply_mode"] == "thread"


def test_analyst_syncs_step_results_into_run_directory_and_metrics(tmp_path) -> None:
    session = types.SimpleNamespace(id="s1", chat_id=1, workdir=str(tmp_path))
    run_dir = AnalystRunDirectory(cli_proxy_artifact_path(str(tmp_path), ".analyst_runs"), run_id="2026-04-13_sync")
    run_dir.create(
        analysis_profile="codebase",
        document_kind="spec",
        detail_level="standard",
        template_id="change_spec",
        summary="",
        user_request="Сделай ТЗ на доработку",
        session_id="s1",
    )

    session_log_path = Path(tmp_path) / "SESSION.json"
    session_log_path.write_text(
        json.dumps(
            {
                "orchestrator_by_task": {
                    "s1": [
                        {
                            "step_results": [
                                {
                                    "task_id": "research_step",
                                    "status": "partial",
                                    "summary": "Проверил карту кодовой базы",
                                    "title": "Сбор фактов",
                                    "step_type": "use_cli",
                                    "outputs": [
                                        {
                                            "type": "text",
                                            "content_preview": "modes/analyst/mode.py",
                                        }
                                    ],
                                    "claims": [
                                        {
                                            "status": "confirmed",
                                            "text": "В analyst уже есть template_first flow",
                                            "evidence": [
                                                {"path": "modes/analyst/templates/analyst_config.yaml"}
                                            ],
                                        }
                                    ],
                                },
                                {
                                    "task_id": "research_step",
                                    "status": "ok",
                                    "summary": "Уточнил границы sync",
                                    "title": "Сбор фактов",
                                    "step_type": "use_cli",
                                    "outputs": [
                                        {
                                            "type": "text",
                                            "content_preview": "modes/analyst/run_directory.py",
                                        }
                                    ],
                                    "claims": [
                                        {
                                            "status": "confirmed",
                                            "text": "step_results можно синхронизировать в run_dir",
                                            "evidence": [
                                                {"path": "modes/analyst/run_directory.py"}
                                            ],
                                        }
                                    ],
                                },
                                {
                                    "task_id": "design_step",
                                    "status": "ok",
                                    "summary": "Проверил внешний референс",
                                    "title": "Внешнее исследование",
                                    "step_type": "use_cli",
                                    "outputs": [{"type": "text", "content_preview": "tests/..."}],
                                    "claims": [],
                                },
                            ]
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = _build_app(tmp_path)
    store = RunArtifactStore(cfg)
    run_handle = store.start_run(session=session, mode_id="analyst", run_id="run_20260413T120000Z_sync")
    store.save_metrics(
        run_handle,
        {
            "analyst_quality": {
                "runtime_verdict": "Готово к реализации",
                "blocking_reasons": [],
            }
        },
    )

    mode = AnalystMode()
    mode.initialize(config=cfg, services={})
    evidence = mode._sync_analyst_step_artifacts(session=session, run_dir=run_dir, run_handle=run_handle)

    meta = run_dir.load_meta()
    step_entries = meta["steps"]

    assert evidence["step_count"] == 2
    assert evidence["artifact_coverage"] == 1.0
    assert evidence["sources_coverage"] == 0.5
    assert evidence["facts_coverage"] == 1.0
    assert [item["id"] for item in step_entries] == ["research_step", "design_step"]

    research = step_entries[0]
    assert research["attempts"] == 2
    assert len(research["history"]) == 2
    research_artifact = Path(run_dir.step_artifact_path("research_step"))
    content = research_artifact.read_text(encoding="utf-8")
    assert "## Goal" in content
    assert "## Reviewed files/sources" in content
    assert "## Confirmed facts" in content
    assert "## Unconfirmed gaps" in content
    assert "## Attempts / History" in content
    assert "Attempt 1" in content
    assert "Attempt 2" in content

    metrics = store.load_metrics(run_handle)
    assert metrics["analyst_quality"]["runtime_verdict"] == "Готово к реализации"
    assert (
        "Исследовательские CLI-шаги без reviewed files/sources: 1"
        in metrics["analyst_quality"]["warning_reasons"]
    )
    assert (
        "Завершенные исследовательские CLI-шаги без reviewed files/sources: 1"
        in metrics["analyst_quality"]["warning_reasons"]
    )
    assert metrics["analyst_evidence_trail"]["step_count"] == 2
    assert metrics["analyst_evidence_trail"]["artifact_coverage"] == 1.0
    assert metrics["analyst_evidence_trail"]["completed_steps"] == 2
    assert metrics["analyst_evidence_trail"]["completed_steps_with_sources"] == 1
    assert metrics["analyst_evidence_trail"]["completed_evidence_steps"] == 2
    assert metrics["analyst_evidence_trail"]["completed_evidence_steps_with_sources"] == 1


@pytest.mark.asyncio
async def test_analyst_unfinished_cleanup_is_scoped_to_current_session(tmp_path) -> None:
    base_path = cli_proxy_artifact_path(str(tmp_path), ".analyst_runs")
    current_run = AnalystRunDirectory(base_path, run_id="2026-04-13_aaaaaa")
    current_run.create(
        analysis_profile="codebase",
        document_kind="spec",
        detail_level="standard",
        template_id="change_spec",
        summary="",
        user_request="Текущая сессия",
        session_id="current",
    )
    current_run.update_meta(status="running")

    foreign_run = AnalystRunDirectory(base_path, run_id="2026-04-13_zzzzzz")
    foreign_run.create(
        analysis_profile="codebase",
        document_kind="spec",
        detail_level="standard",
        template_id="change_spec",
        summary="",
        user_request="Чужая сессия",
        session_id="foreign",
    )
    foreign_run.update_meta(status="running")

    session = types.SimpleNamespace(id="current", workdir=str(tmp_path), chat_id=1)
    mode = AnalystMode()
    mode.initialize(config=_build_app(tmp_path).config, services={})

    resumed = await mode._maybe_resolve_unfinished_analysis(
        session=session,
        message=None,
        bot_app=None,
        context=None,
        dest={},
        chat_id=1,
        ms=None,
    )

    assert resumed is False
    assert current_run.load_meta()["status"] == "abandoned"
    assert foreign_run.load_meta()["status"] == "running"


@pytest.mark.asyncio
async def test_analyst_run_pipeline_sets_run_handle_before_runtime_and_marks_run_complete(tmp_path, monkeypatch) -> None:
    templates_path = tmp_path / "analyst_config.yaml"
    _write_templates(templates_path)
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates_path))
    _patch_run_ids(monkeypatch, ["run_20260413T180000Z_handle001"])

    app = _build_app(tmp_path)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("analyst")
    assert mode is not None

    backend = _HandleAwareRunnerBackend(["# Итог\n\nГотово."])
    _install_runtime_backend(app, backend)

    async def _fake_classify_intent(*, session, user_text, context, dest, bot_app, clarification_answers=None):
        _ = session, user_text, context, dest, bot_app, clarification_answers
        return {
            "analysis_profile": "spec",
            "document_kind": "spec",
            "detail_level": "standard",
            "summary": "Короткое summary",
            "clarification_questions": [],
        }

    monkeypatch.setattr(mode, "_classify_intent", _fake_classify_intent)
    monkeypatch.setattr(mode, "_resolve_template", lambda *_args, **_kwargs: ("default", {}))
    monkeypatch.setattr("modes.analyst.mode.build_analyst_prompt", lambda *_args, **_kwargs: "PROMPT")

    output = await mode.run_pipeline(
        session=session,
        user_text="Подготовь ТЗ на доработку",
        bot_app=app,
        context=None,
        dest={"kind": "telegram", "chat_id": 1, "chat_type": "private"},
    )

    assert output == "# Итог\n\nГотово."
    assert getattr(session, "analyst_run_artifact_handle", None) is None

    store = _run_store(app)
    latest = store.latest_run(session=session, mode_id="analyst")
    assert latest is not None
    state = store.load_state(latest)

    assert state["status"] == "completed"
    assert state["phase"] == "complete"
    assert state["mode_context"]["execution_context"]["chat_id"] == 1
    assert state["mode_context"]["execution_context"]["source_user_text"] == "Подготовь ТЗ на доработку"
    assert Path(latest.artifacts_dir, "runtime-check.md").read_text(encoding="utf-8") == "runtime artifacts are run-scoped"
    assert backend.handle_artifacts == [latest.artifacts_dir]


@pytest.mark.asyncio
async def test_analyst_quality_gate_warning_still_delivers_final_answer(tmp_path, monkeypatch) -> None:
    templates_path = tmp_path / "analyst_config.yaml"
    _write_templates(templates_path)
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates_path))
    _patch_run_ids(monkeypatch, ["run_20260413T181500Z_block001"])

    app = _build_app(tmp_path)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("analyst")
    assert mode is not None

    backend = _BlockedDeliveryRunnerBackend(
        ["# Черновик\n\nНужна дополнительная валидация."],
        followup_payload={
            "verdict": "PASS по blocking obligations не достигнут",
            "final_text": (
                "VERDICT\n\nPASS по blocking obligations не достигнут.\n\n"
                "OPEN_BLOCKING_OBLIGATIONS\n\n"
                "- Writer integration point не определён.\n"
                "- Совместимость materialized Codex session с codex resume не верифицирована.\n\n"
                "REQUIRED_CORRECTIONS\n\n"
                "- Подтвердить native write path для Codex."
            ),
            "open_blocking_obligations": [
                {
                    "statement": "Writer integration point не определён.",
                },
                {
                    "statement": "Совместимость materialized Codex session с codex resume не верифицирована.",
                },
            ],
            "required_corrections": [
                "Подтвердить native write path для Codex.",
            ],
        },
    )
    _install_runtime_backend(app, backend)

    delivered_outputs: list[dict] = []
    fallback_messages: list[dict] = []

    async def _send_output(_session, _dest, output, _context, **kwargs):
        delivered_outputs.append(
            {
                "output": str(output or ""),
                "kwargs": dict(kwargs or {}),
            }
        )
        return None

    async def _send_message(_context, **kwargs):
        fallback_messages.append(dict(kwargs or {}))
        return True

    monkeypatch.setattr(app, "send_output", _send_output)
    monkeypatch.setattr(app, "_send_message", _send_message)

    async def _fake_classify_intent(*, session, user_text, context, dest, bot_app, clarification_answers=None):
        _ = session, user_text, context, dest, bot_app, clarification_answers
        return {
            "analysis_profile": "codebase",
            "document_kind": "spec",
            "detail_level": "standard",
            "summary": "Короткое summary",
            "clarification_questions": [],
        }

    monkeypatch.setattr(mode, "_classify_intent", _fake_classify_intent)
    monkeypatch.setattr(mode, "_resolve_template", lambda *_args, **_kwargs: ("change_spec", {"repo_grounded_required": True}))
    monkeypatch.setattr("modes.analyst.mode.build_analyst_prompt", lambda *_args, **_kwargs: "PROMPT")

    await app.run_mode_pipeline(
        session,
        "Подготовь ТЗ по переносу сессий в Codex CLI",
        {"kind": "telegram", "chat_id": 1, "chat_type": "private"},
        object(),
        mode_id="analyst",
    )

    assert len(delivered_outputs) == 1
    delivered_text = delivered_outputs[0]["output"]
    assert delivered_outputs[0]["kwargs"] == {"send_header": False}
    assert delivered_text == "# Черновик\n\nНужна дополнительная валидация."
    assert "Анализ завершён, но итог не прошёл quality gate" not in delivered_text
    assert "PASS по blocking obligations не достигнут" not in delivered_text
    assert fallback_messages == []

    store = _run_store(app)
    latest = store.latest_run(session=session, mode_id="analyst")
    assert latest is not None
    state = store.load_state(latest)

    assert state["status"] == "completed"
    assert state["phase"] == "complete"
    assert state["mode_context"]["final_deliverable"] == delivered_text
    assert state["mode_context"]["quality_gate_passed"] is False
    assert state["mode_context"]["quality_gate_artifact_path"].endswith("s25_obligation_review_followup.json")
    assert state["mode_context"]["quality_gate_reasons"] == [
        "Writer integration point не определён.",
        "Совместимость materialized Codex session с codex resume не верифицирована.",
    ]
    assert str(session.orchestrator.last_mode_output or "") == delivered_text
    assert str(session.orchestrator.last_mode_id or "") == "analyst"


@pytest.mark.asyncio
async def test_analyst_writes_placeholder_step_artifact_before_runtime_returns(tmp_path, monkeypatch) -> None:
    templates_path = tmp_path / "analyst_config.yaml"
    _write_templates(templates_path)
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates_path))
    _patch_run_ids(monkeypatch, ["run_20260419T150000Z_placeholder001"])

    app = _build_app(tmp_path)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("analyst")
    assert mode is not None

    backend = _PlaceholderAwareRunnerBackend(["# Итог\n\nГотово."])
    _install_runtime_backend(app, backend)

    async def _fake_classify_intent(*, session, user_text, context, dest, bot_app, clarification_answers=None):
        _ = session, user_text, context, dest, bot_app, clarification_answers
        return {
            "analysis_profile": "spec",
            "document_kind": "spec",
            "detail_level": "standard",
            "summary": "Короткое summary",
            "clarification_questions": [],
        }

    monkeypatch.setattr(mode, "_classify_intent", _fake_classify_intent)
    monkeypatch.setattr(mode, "_resolve_template", lambda *_args, **_kwargs: ("default", {}))
    monkeypatch.setattr("modes.analyst.mode.build_analyst_prompt", lambda *_args, **_kwargs: "PROMPT")

    output = await mode.run_pipeline(
        session=session,
        user_text="Подготовь ТЗ на доработку",
        bot_app=app,
        context=None,
        dest={"kind": "telegram", "chat_id": 1, "chat_type": "private"},
    )

    assert output == "# Итог\n\nГотово."
    assert len(backend.placeholder_snapshots) == 1
    placeholder_steps = backend.placeholder_snapshots[0]
    assert len(placeholder_steps) == 1
    assert placeholder_steps[0]["id"] == "analyst_orchestrator"
    assert placeholder_steps[0]["status"] == "in_progress"

    latest_run = AnalystRunDirectory.latest_run(
        resolve_analyst_runs_root(session),
        session_id=str(getattr(session, "id", "") or ""),
    )
    assert latest_run is not None
    meta = latest_run.load_meta()
    assert meta["steps"][0]["id"] == "analyst_orchestrator"
    assert meta["steps"][0]["status"] == "completed"
    assert Path(latest_run.step_artifact_path("analyst_orchestrator")).exists()


@pytest.mark.asyncio
async def test_analyst_syncs_step_results_into_meta_before_runtime_returns(tmp_path, monkeypatch) -> None:
    templates_path = tmp_path / "analyst_config.yaml"
    _write_templates(templates_path)
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates_path))
    _patch_run_ids(monkeypatch, ["run_20260419T161500Z_incremental001"])

    app = _build_app(tmp_path)
    session = _prepare_session(app, tmp_path)
    mode = app.mode_registry.get("analyst")
    assert mode is not None

    backend = _IncrementalSyncRunnerBackend(
        ["# Итог\n\nГотово."],
        step_results=[
            {
                "task_id": "research_step",
                "status": "ok",
                "summary": "Собрал факты по текущей реализации",
                "title": "Сбор фактов",
                "step_type": "use_cli",
                "outputs": [{"type": "text", "content_preview": "modes/analyst/mode.py"}],
                "claims": [
                    {
                        "status": "confirmed",
                        "text": "meta.json должен обновляться по мере выполнения шагов",
                        "evidence": [{"path": "modes/analyst/run_directory.py"}],
                    }
                ],
            }
        ],
    )
    _install_runtime_backend(app, backend)

    async def _fake_classify_intent(*, session, user_text, context, dest, bot_app, clarification_answers=None):
        _ = session, user_text, context, dest, bot_app, clarification_answers
        return {
            "analysis_profile": "spec",
            "document_kind": "spec",
            "detail_level": "standard",
            "summary": "Короткое summary",
            "clarification_questions": [],
        }

    monkeypatch.setattr(mode, "_classify_intent", _fake_classify_intent)
    monkeypatch.setattr(mode, "_resolve_template", lambda *_args, **_kwargs: ("default", {}))
    monkeypatch.setattr("modes.analyst.mode.build_analyst_prompt", lambda *_args, **_kwargs: "PROMPT")

    output = await mode.run_pipeline(
        session=session,
        user_text="Подготовь ТЗ на доработку",
        bot_app=app,
        context=None,
        dest={"kind": "telegram", "chat_id": 1, "chat_type": "private"},
    )

    assert output == "# Итог\n\nГотово."
    assert len(backend.synced_snapshots) == 1
    synced_steps = backend.synced_snapshots[0]
    assert len(synced_steps) == 1
    assert synced_steps[0]["id"] == "research_step"
    assert synced_steps[0]["status"] == "ok"
    assert getattr(session, "analyst_step_results_sync_hook", None) is None

    latest_run = AnalystRunDirectory.latest_run(
        resolve_analyst_runs_root(session),
        session_id=str(getattr(session, "id", "") or ""),
    )
    assert latest_run is not None
    meta = latest_run.load_meta()
    assert [item["id"] for item in (meta.get("steps") or [])] == ["research_step"]
    assert Path(latest_run.step_artifact_path("research_step")).exists()
