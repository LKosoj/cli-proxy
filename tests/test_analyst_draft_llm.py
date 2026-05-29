import json
import os
import types

from app.services.run_artifact_store import RunArtifactStore
from modes.analyst.run_directory import AnalystRunDirectory
from modes.sdk.runtime.memory_store import ensure_chat_workspace
from tg.callbacks import CallbackHandler
from utils.paths import cli_proxy_artifact_path


def test_analyst_draft_uses_llm_when_final_missing(tmp_path, monkeypatch):
    # Arrange: create SESSION.json with step_results but empty final.
    workdir = str(tmp_path)
    chat_id = 100
    sb = ensure_chat_workspace(workdir, chat_id)
    os.makedirs(sb, exist_ok=True)
    session_id = "s1"
    path = os.path.join(sb, "SESSION.json")
    data = {
        "orchestrator_by_task": {
            session_id: [
                {
                    "user": "goal",
                    "step_results": [
                        {"task_id": "a", "summary": "sum-a", "outputs": [{"type": "text", "content_preview": "x"}]},
                    ],
                    "final": "",
                }
            ]
        }
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    # Fake bot app with OpenAI configured.
    session = types.SimpleNamespace(id=session_id, state_summary="", chat_id=chat_id)
    bot_app = types.SimpleNamespace(
        manager=types.SimpleNamespace(active=lambda: session),
        get_mode_runtime=lambda _mode_id: None,
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="k",
                openai_model="m",
                workdir=workdir,
            )
        ),
    )
    handler = CallbackHandler(bot_app)

    # Ensure required_sections come from mode runtime template provider.
    monkeypatch.setattr(
        "tg.callbacks.CallbackHandler._resolve_analyst_template_from_mode",
        lambda _self, _session: {"required_sections": ["SEC1"]},
    )

    captured = {}

    async def _fake_chat_completion(_cfg, system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return "# Draft\n\n- SEC1\n\n[В процессе]"

    monkeypatch.setattr("tg.callbacks.chat_completion", _fake_chat_completion)

    # Act
    out = __import__("asyncio").run(handler._build_analyst_draft_text(session))

    # Assert: output uses LLM result and prompt contains required sections.
    assert "# Draft" in out
    assert "SEC1" in captured.get("user", "")


def test_analyst_draft_llm_uses_audit_wrapper_from_session_document_kind(tmp_path, monkeypatch):
    workdir = str(tmp_path)
    chat_id = 100
    sb = ensure_chat_workspace(workdir, chat_id)
    os.makedirs(sb, exist_ok=True)
    session_id = "s1"
    path = os.path.join(sb, "SESSION.json")
    with open(
        path,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            {
                "orchestrator_by_task": {
                    session_id: [
                        {
                            "user": "Проведи аудит",
                            "step_results": [{"task_id": "a", "summary": "sum-a"}],
                            "final": "",
                        }
                    ]
                }
            },
            f,
            ensure_ascii=False,
        )

    session = types.SimpleNamespace(
        id=session_id,
        state_summary="",
        chat_id=chat_id,
        analyst_intent_flags={"document_kind": "audit"},
    )
    bot_app = types.SimpleNamespace(
        manager=types.SimpleNamespace(active=lambda: session),
        get_mode_runtime=lambda _mode_id: None,
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="k",
                openai_model="m",
                workdir=workdir,
            )
        ),
    )
    handler = CallbackHandler(bot_app)

    monkeypatch.setattr(
        "tg.callbacks.CallbackHandler._resolve_analyst_template_from_mode",
        lambda _self, _session: {"required_sections": ["SEC1"]},
    )

    async def _fake_chat_completion(_cfg, _system, _user, **_kwargs):
        return "## Observation\n\n- finding"

    monkeypatch.setattr("tg.callbacks.chat_completion", _fake_chat_completion)

    out = __import__("asyncio").run(handler._build_analyst_draft_text(session))

    assert "# Черновик отчета по аудиту" in out
    assert "Тип документа: отчет по аудиту" in out
    assert "## Observation" in out


def test_analyst_draft_prefers_fact_pack_and_avoids_raw_truncation(tmp_path, monkeypatch):
    workdir = str(tmp_path)
    chat_id = 100
    sb = ensure_chat_workspace(workdir, chat_id)
    os.makedirs(sb, exist_ok=True)
    session_id = "s1"
    orchestrator_dir = os.path.join(sb, "_orchestrator")
    os.makedirs(orchestrator_dir, exist_ok=True)
    with open(os.path.join(orchestrator_dir, f"{session_id}_fact_pack.md"), "w", encoding="utf-8") as f:
        f.write("# Fact Pack\n\n- confirmed fact\n")

    path = os.path.join(sb, "SESSION.json")
    data = {
        "orchestrator_by_task": {
            session_id: [
                {
                    "user": "goal",
                    "step_results": [
                        {
                            "task_id": "a",
                            "summary": "sum-a",
                            "orchestrator_artifact": os.path.join(orchestrator_dir, "a.md"),
                            "outputs": [{"type": "text", "content_preview": "x", "path": os.path.join(orchestrator_dir, "spill.md")}],
                        },
                    ],
                    "final": "",
                }
            ]
        }
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    session = types.SimpleNamespace(id=session_id, state_summary="", chat_id=chat_id)
    bot_app = types.SimpleNamespace(
        manager=types.SimpleNamespace(active=lambda: session),
        get_mode_runtime=lambda _mode_id: None,
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="k",
                openai_model="m",
                workdir=workdir,
            )
        ),
    )
    handler = CallbackHandler(bot_app)

    monkeypatch.setattr(
        "tg.callbacks.CallbackHandler._resolve_analyst_template_from_mode",
        lambda _self, _session: {"required_sections": ["SEC1"]},
    )

    captured = {}

    async def _fake_chat_completion(_cfg, system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return "# Draft\n\n- SEC1\n\n[В процессе]"

    monkeypatch.setattr("tg.callbacks.chat_completion", _fake_chat_completion)

    out = __import__("asyncio").run(handler._build_analyst_draft_text(session))

    assert "# Draft" in out
    assert "Fact Pack" in captured.get("user", "")
    assert "...(truncated)" not in captured.get("user", "")


def test_analyst_draft_reads_fact_pack_from_latest_run_artifacts_dir(tmp_path, monkeypatch):
    workdir = str(tmp_path)
    chat_id = 100
    sb = ensure_chat_workspace(workdir, chat_id)
    os.makedirs(sb, exist_ok=True)
    session_id = "s1"

    path = os.path.join(sb, "SESSION.json")
    data = {
        "orchestrator_by_task": {
            session_id: [
                {
                    "user": "goal",
                    "step_results": [
                        {
                            "task_id": "a",
                            "summary": "sum-a",
                            "outputs": [{"type": "text", "content_preview": "x"}],
                        },
                    ],
                    "final": "",
                }
            ]
        }
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    session = types.SimpleNamespace(id=session_id, state_summary="", chat_id=chat_id)
    bot_app = types.SimpleNamespace(
        manager=types.SimpleNamespace(active=lambda: session),
        get_mode_runtime=lambda _mode_id: None,
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="k",
                openai_model="m",
                workdir=workdir,
            )
        ),
    )
    run_store = RunArtifactStore(bot_app.config)
    run_handle = run_store.start_run(
        session=types.SimpleNamespace(id=session_id, chat_id=chat_id, workdir=workdir),
        mode_id="analyst",
        run_id="run_20260410T122000Z_draftbundle",
    )
    with open(os.path.join(run_handle.artifacts_dir, f"{session_id}_fact_pack.md"), "w", encoding="utf-8") as f:
        f.write("# Fact Pack\n\n- latest run fact\n")

    handler = CallbackHandler(bot_app)

    monkeypatch.setattr(
        "tg.callbacks.CallbackHandler._resolve_analyst_template_from_mode",
        lambda _self, _session: {"required_sections": ["SEC1"]},
    )

    captured = {}

    async def _fake_chat_completion(_cfg, system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return "# Draft\n\n- SEC1\n\n[В процессе]"

    monkeypatch.setattr("tg.callbacks.chat_completion", _fake_chat_completion)

    out = __import__("asyncio").run(handler._build_analyst_draft_text(session))

    assert "# Draft" in out
    assert "latest run fact" in captured.get("user", "")


def test_analyst_draft_prefers_run_dir_step_artifacts_when_session_steps_are_missing(tmp_path, monkeypatch):
    workdir = str(tmp_path)
    chat_id = 100
    sb = ensure_chat_workspace(workdir, chat_id)
    os.makedirs(sb, exist_ok=True)
    session_id = "s1"

    path = os.path.join(sb, "SESSION.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "orchestrator_by_task": {
                    session_id: [
                        {
                            "user": "goal",
                            "step_results": [],
                            "final": "",
                        }
                    ]
                }
            },
            f,
            ensure_ascii=False,
        )
    orchestrator_dir = os.path.join(sb, "_orchestrator")
    os.makedirs(orchestrator_dir, exist_ok=True)
    with open(os.path.join(orchestrator_dir, f"{session_id}_fact_pack.md"), "w", encoding="utf-8") as f:
        f.write("# Fact Pack\n\n- session evidence\n")

    session = types.SimpleNamespace(id=session_id, state_summary="", chat_id=chat_id, workdir=workdir)
    runs_root = cli_proxy_artifact_path(workdir, ".analyst_runs")
    run_dir = AnalystRunDirectory(runs_root, run_id="2026-04-13_draft")
    run_dir.create(
        analysis_profile="codebase",
        document_kind="spec",
        detail_level="standard",
        template_id="change_spec",
        summary="",
        user_request="Сделай ТЗ",
        session_id=session_id,
    )
    run_dir.add_step("repo_scan", status="ok")
    run_dir.update_meta(
        steps=[
            {
                "id": "repo_scan",
                "title": "Сбор подтвержденных фактов",
                "status": "ok",
                "attempts": 2,
                "artifact": "steps/repo_scan.md",
                "summary": "Подтвердил текущую реализацию analyst pipeline",
                "reviewed_sources": ["modes/analyst/mode.py", "modes/analyst/run_directory.py"],
                "confirmed_facts": ["pipeline сам синхронизирует meta.steps"],
                "unconfirmed_gaps": ["не зафиксированы"],
                "history": [
                    {
                        "attempt": 1,
                        "status": "partial",
                        "summary": "Собрал первичные факты",
                        "reviewed_sources": ["modes/analyst/mode.py"],
                        "confirmed_facts": ["есть post-runtime sync"],
                        "unconfirmed_gaps": ["нужно проверить draft bridge"],
                    },
                    {
                        "attempt": 2,
                        "status": "ok",
                        "summary": "Подтвердил текущую реализацию analyst pipeline",
                        "reviewed_sources": ["modes/analyst/mode.py", "modes/analyst/run_directory.py"],
                        "confirmed_facts": ["pipeline сам синхронизирует meta.steps"],
                        "unconfirmed_gaps": ["не зафиксированы"],
                    },
                ],
            }
        ],
        evidence_trail={
            "step_count": 1,
            "completed_steps": 1,
            "completed_steps_with_sources": 1,
            "warning_reasons": [],
        },
    )
    with open(run_dir.step_artifact_path("repo_scan"), "w", encoding="utf-8") as f:
        f.write(
            "# Шаг repo_scan\n\n"
            "## Goal\n\n"
            "Сбор подтвержденных фактов\n\n"
            "## Reviewed files/sources\n\n"
            "- modes/analyst/mode.py\n"
            "- modes/analyst/run_directory.py\n\n"
            "## Confirmed facts\n\n"
            "- pipeline сам синхронизирует meta.steps\n\n"
            "## Unconfirmed gaps\n\n"
            "- не зафиксированы\n"
        )
    foreign_run = AnalystRunDirectory(runs_root, run_id="2026-04-13_zzzzzz")
    foreign_run.create(
        analysis_profile="codebase",
        document_kind="spec",
        detail_level="standard",
        template_id="change_spec",
        summary="",
        user_request="Чужая сессия",
        session_id="foreign",
    )
    foreign_run.update_meta(
        steps=[
            {
                "id": "foreign_step",
                "title": "Чужой шаг",
                "status": "ok",
                "attempts": 1,
                "artifact": "steps/foreign_step.md",
                "summary": "Это не должно попасть в draft текущей сессии",
                "reviewed_sources": ["foreign.py"],
                "confirmed_facts": ["чужой факт"],
                "unconfirmed_gaps": ["не зафиксированы"],
                "history": [],
            }
        ],
        evidence_trail={
            "step_count": 1,
            "completed_steps": 1,
            "completed_steps_with_sources": 1,
            "warning_reasons": [],
        },
    )
    with open(foreign_run.step_artifact_path("foreign_step"), "w", encoding="utf-8") as f:
        f.write("# Шаг foreign_step\n")

    bot_app = types.SimpleNamespace(
        manager=types.SimpleNamespace(active=lambda: session),
        get_mode_runtime=lambda _mode_id: None,
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="k",
                openai_model="m",
                workdir=workdir,
            )
        ),
    )
    handler = CallbackHandler(bot_app)

    monkeypatch.setattr(
        "tg.callbacks.CallbackHandler._resolve_analyst_template_from_mode",
        lambda _self, _session: {"required_sections": ["SEC1"]},
    )

    captured = {}

    async def _fake_chat_completion(_cfg, system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return "# Draft\n\n- SEC1\n\n[В процессе]"

    monkeypatch.setattr("tg.callbacks.chat_completion", _fake_chat_completion)

    out = __import__("asyncio").run(handler._build_analyst_draft_text(session))

    assert "# Draft" in out
    assert "Fact Pack" in captured.get("user", "")
    assert "repo_scan.md" in captured.get("user", "")
    assert '"run_id": "2026-04-13_draft"' in captured.get("user", "")
    assert '"completed_steps_with_sources": 1' in captured.get("user", "")
    assert "foreign_step.md" not in captured.get("user", "")
