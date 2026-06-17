from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.report_history_service import ReportContent, ReportSummary
from desktop.services.application_facade import ApplicationFacade


def _summary(report_id: str = "manager_plan_20260101_120000.md") -> ReportSummary:
    return ReportSummary(
        report_id=report_id,
        name=report_id,
        path=f"/tmp/{report_id}",
        size=12,
        mtime=1.0,
        date="2026-01-01 00:00 UTC",
        fmt="md",
        content_type="text/markdown",
        readable=True,
        title="Manager plan",
    )


def _facade(report_history_service, session=None) -> ApplicationFacade:
    session_service = MagicMock()
    session_service.get_session_by_uid.return_value = session
    facade = ApplicationFacade(
        config_service=MagicMock(),
        session_service=session_service,
        task_service=MagicMock(),
        advanced_orchestrator_service=MagicMock(),
        report_history_service=report_history_service,
    )
    facade.config = SimpleNamespace(defaults=SimpleNamespace(default_language="ru"))
    return facade


def test_desktop_facade_lists_reports_through_report_history_service():
    session = SimpleNamespace(workdir="/work")
    service = MagicMock()
    service.list_reports.return_value = [_summary("a.md")]
    facade = _facade(service, session=session)

    reports = facade.list_session_reports("desktop:sess1")

    service.list_reports.assert_called_once_with(session, limit=50)
    assert reports == [_summary("a.md").to_dict()]


def test_desktop_facade_gets_report_content_through_report_history_service():
    session = SimpleNamespace(workdir="/work")
    service = MagicMock()
    summary = _summary("a.md")
    service.get_report.return_value = ReportContent(summary=summary, content="# A")
    facade = _facade(service, session=session)

    report = facade.get_session_report("desktop:sess1", "a.md")

    service.get_report.assert_called_once_with(session, "a.md")
    assert report == service.get_report.return_value.to_dict()


def test_desktop_facade_saves_manager_plan_through_report_history_service():
    session = SimpleNamespace(workdir="/work")
    plan = SimpleNamespace(project_goal="Goal")
    service = MagicMock()
    service.save_manager_plan_report.return_value = _summary("manager_plan.md")
    facade = _facade(service, session=session)
    facade.get_manager_plan = MagicMock(return_value=plan)

    report = facade.save_manager_plan_report("desktop:sess1")

    service.save_manager_plan_report.assert_called_once_with(session, plan)
    assert report == service.save_manager_plan_report.return_value.to_dict()


def test_desktop_facade_does_not_save_report_without_plan():
    session = SimpleNamespace(workdir="/work")
    service = MagicMock()
    facade = _facade(service, session=session)
    facade.get_manager_plan = MagicMock(return_value=None)

    assert facade.save_manager_plan_report("desktop:sess1") is None
    service.save_manager_plan_report.assert_not_called()
