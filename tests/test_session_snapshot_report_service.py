from __future__ import annotations

import types
from pathlib import Path

from app.mode_dependencies import RunArtifactsService
from app.services.report_history_service import ReportHistoryService
from app.services.run_artifact_store import RunArtifactStore
from app.services.session_snapshot_report_service import SessionSnapshotReportService
from app.services.session_transfer.canonical import CanonicalMessage, CanonicalSession
from sessions.conversation_scope import ConversationScope


def _session(tmp_path: Path):
    return types.SimpleNamespace(
        id="s1",
        chat_id=101,
        name="Work session",
        workdir=str(tmp_path),
        tool=types.SimpleNamespace(name="codex"),
        cli=types.SimpleNamespace(active_cli="codex", resume_tokens={"codex": "codex-session-1"}),
        modes=types.SimpleNamespace(active_mode="analyst"),
        queue=[],
        busy=False,
        conversation_scope=ConversationScope.from_parts(101),
    )


def test_session_snapshot_report_collects_active_mode_runs_reports_and_chat(tmp_path: Path) -> None:
    config = types.SimpleNamespace(defaults=types.SimpleNamespace(workdir=str(tmp_path), run_artifacts_enabled=True))
    store = RunArtifactStore(config)
    run_artifacts = RunArtifactsService(enabled=True, retention_days=30, artifact_store=store)
    reports = ReportHistoryService()
    session = _session(tmp_path)

    manager_run = store.start_run(session=session, mode_id="manager", run_id="run_manager")
    store.mark_finished(manager_run, status="completed", phase="final")
    analyst_run = store.start_run(session=session, mode_id="analyst", run_id="run_analyst", phase="draft")
    store.save_state(analyst_run, {"status": "completed", "phase": "draft", "current_step_id": "scan"})
    store.save_plan(analyst_run, {"task_family": "analysis", "units": [{"id": "scan"}]})
    store.append_checkpoint(analyst_run, {"phase": "draft", "message": "checkpoint"})
    store.append_event(analyst_run, {"event_type": "phase_end", "phase": "draft"})
    artifact_path = Path(analyst_run.artifacts_dir) / "fact_pack.md"
    artifact_path.write_text("facts", encoding="utf-8")
    reports.save_markdown_report(session, "# Existing report", now=1000)

    def _extract_session(source_cli: str, session_id: str, workspace: str) -> CanonicalSession:
        return CanonicalSession(
            source_cli=source_cli,
            session_id=session_id,
            workspace=workspace,
            messages=[
                CanonicalMessage(role="user", content="initial task", timestamp=1000),
                CanonicalMessage(role="assistant", content="done", timestamp=1010),
            ],
        )

    service = SessionSnapshotReportService(
        report_history_service=reports,
        run_artifacts_service=run_artifacts,
        extract_session_fn=_extract_session,
        now_fn=lambda: 2000,
    )

    summary = service.save_html_report(session, now=2000, lang="ru")
    content = reports.get_report(session, summary.report_id).content or ""

    assert summary.report_id == "session_snapshot_19700101_003320.html"
    assert '<html lang="ru">' in content
    assert "Отчёт по сессии: chat:101:s1" in content
    assert "Короткая версия для чтения человеком" in content
    assert "Коротко" in content
    assert "Что важно" in content
    assert "План содержит 1 шаг" in content
    assert "Технические данные запуска" in content
    assert "run_analyst" in content
    assert "fact_pack.md" in content
    assert "report_19700101_001640.md" in content
    assert "Пользователь" in content
    assert "initial task" in content
    assert "run_manager" not in content


def test_session_snapshot_report_handles_disabled_run_artifacts(tmp_path: Path) -> None:
    reports = ReportHistoryService()
    service = SessionSnapshotReportService(
        report_history_service=reports,
        run_artifacts_service=types.SimpleNamespace(is_enabled=lambda: False),
        extract_session_fn=lambda *_args: None,
        now_fn=lambda: 2000,
    )

    summary = service.save_html_report(_session(tmp_path), now=2000, lang="ru")
    content = reports.get_report(_session(tmp_path), summary.report_id).content or ""

    assert "Артефакты запусков отключены в runtime config." in content


def test_session_snapshot_report_uses_requested_language(tmp_path: Path) -> None:
    reports = ReportHistoryService()
    service = SessionSnapshotReportService(
        report_history_service=reports,
        run_artifacts_service=types.SimpleNamespace(is_enabled=lambda: False),
        extract_session_fn=lambda *_args: None,
        now_fn=lambda: 2000,
    )
    session = _session(tmp_path)

    samples = [
        ("ru", '<html lang="ru">', "Отчёт по сессии", "Коротко"),
        ("en", '<html lang="en">', "Session report", "At a glance"),
        ("de", '<html lang="de">', "Sitzungsbericht", "Kurzüberblick"),
        ("zh", '<html lang="zh">', "会话报告", "概览"),
    ]
    for lang, html_lang, title, section in samples:
        content = service.build_html_report(session, now=2000, lang=lang).html

        assert html_lang in content
        assert title in content
        assert section in content

    en_content = service.build_html_report(session, now=2000, lang="unknown").html
    assert '<html lang="en">' in en_content
    assert "Session report" in en_content
