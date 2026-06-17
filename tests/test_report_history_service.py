from __future__ import annotations

import os
import types

import pytest

from app.services.report_history_service import (
    InvalidReportIdError,
    ReportHistoryService,
    ReportNotFoundError,
)
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from utils.paths import cli_proxy_artifact_path


def _session(tmp_path):
    return types.SimpleNamespace(workdir=str(tmp_path), id="s1")


def test_save_and_read_markdown_report(tmp_path):
    service = ReportHistoryService()
    session = _session(tmp_path)

    summary = service.save_markdown_report(session, "# Report\nBody", now=1000)
    content = service.get_report(session, summary.report_id)

    assert summary.report_id == "report_19700101_001640.md"
    assert content.content == "# Report\nBody"
    assert content.summary.to_dict()["content_type"] == "text/markdown"


def test_list_reports_sorts_newest_first_and_ignores_unknown_extensions(tmp_path):
    service = ReportHistoryService()
    session = _session(tmp_path)
    reports_dir = service.ensure_reports_dir(session)
    old_path = os.path.join(reports_dir, "old.md")
    new_path = os.path.join(reports_dir, "new.md")
    ignored_path = os.path.join(reports_dir, "data.csv")
    for path, body in (
        (old_path, "old"),
        (new_path, "new"),
        (ignored_path, "csv"),
    ):
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    os.utime(old_path, (1000, 1000))
    os.utime(new_path, (2000, 2000))
    os.utime(ignored_path, (3000, 3000))

    reports = service.list_reports(session)

    assert [item.report_id for item in reports] == ["new.md", "old.md"]


def test_save_markdown_report_allocates_unique_names(tmp_path):
    service = ReportHistoryService()
    session = _session(tmp_path)

    first = service.save_markdown_report(session, "one", now=1000)
    second = service.save_markdown_report(session, "two", now=1000)

    assert first.report_id == "report_19700101_001640.md"
    assert second.report_id == "report_19700101_001640_1.md"


@pytest.mark.parametrize("report_id", ["../secret.md", "nested/report.md", "bad.txt", ""])
def test_get_report_rejects_invalid_report_ids(tmp_path, report_id):
    service = ReportHistoryService()
    session = _session(tmp_path)

    with pytest.raises(InvalidReportIdError):
        service.get_report(session, report_id)


def test_get_report_missing_file(tmp_path):
    service = ReportHistoryService()
    session = _session(tmp_path)
    service.ensure_reports_dir(session)

    with pytest.raises(ReportNotFoundError):
        service.get_report(session, "missing.md")


def test_report_dir_uses_existing_manager_reports_contract(tmp_path):
    service = ReportHistoryService()
    session = _session(tmp_path)

    assert service.reports_dir(session) == cli_proxy_artifact_path(str(tmp_path), ".manager_reports")


def test_build_manager_plan_report(tmp_path):
    service = ReportHistoryService()
    session = _session(tmp_path)
    plan = ProjectPlan(
        project_goal="Ship reports",
        tasks=[
            DevTask(
                id="t1",
                title="Backend",
                description="Shared service",
                acceptance_criteria=[],
                status="approved",
            )
        ],
        created_at="2026-01-01",
        updated_at="2026-01-02",
        status="active",
    )

    summary = service.save_manager_plan_report(session, plan)
    content = service.get_report(session, summary.report_id).content

    assert summary.report_id.startswith("manager_plan_")
    assert "# Project Report: Ship reports" in str(content)
    assert "**[APPROVED]** Backend `(t1)`" in str(content)
