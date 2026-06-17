from __future__ import annotations

import datetime as _dt
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from utils.paths import cli_proxy_artifact_path, is_within_root


_REPORTS_ARTIFACT = ".manager_reports"
_ALLOWED_EXTENSIONS = {".md", ".html", ".pdf"}
_TEXT_EXTENSIONS = {".md", ".html"}
_SAFE_PREFIX_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ReportHistoryError(Exception):
    """Base report history service error."""


class InvalidReportIdError(ReportHistoryError):
    """Raised when a report id is not a safe report filename."""


class ReportNotFoundError(ReportHistoryError):
    """Raised when a report does not exist for the session."""


@dataclass(frozen=True)
class ReportSummary:
    report_id: str
    name: str
    path: str
    size: int
    mtime: float
    date: str
    fmt: str
    content_type: str
    readable: bool
    title: str = ""
    source: str = "manager_reports"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.report_id,
            "report_id": self.report_id,
            "name": self.name,
            "filename": self.name,
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "date": self.date,
            "format": self.fmt,
            "content_type": self.content_type,
            "readable": self.readable,
            "title": self.title or self.name,
            "source": self.source,
        }


@dataclass(frozen=True)
class ReportContent:
    summary: ReportSummary
    content: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        data = self.summary.to_dict()
        data["content"] = self.content
        return data


class ReportHistoryService:
    """Shared history for generated session reports.

    The first implementation intentionally keeps the existing
    `.cli-proxy/.manager_reports` storage contract so Desktop, Telegram and
    MiniApp can converge without a migration.
    """

    def reports_dir(self, session: Any) -> str:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            raise ValueError("session workdir is required")
        return cli_proxy_artifact_path(workdir, _REPORTS_ARTIFACT)

    def ensure_reports_dir(self, session: Any) -> str:
        reports_dir = self.reports_dir(session)
        os.makedirs(reports_dir, exist_ok=True)
        return reports_dir

    def list_reports(self, session: Any, *, limit: int = 50) -> list[ReportSummary]:
        reports_dir = self.reports_dir(session)
        if not os.path.isdir(reports_dir):
            return []

        reports: list[ReportSummary] = []
        try:
            for entry in os.scandir(reports_dir):
                if not entry.is_file(follow_symlinks=False):
                    continue
                ext = os.path.splitext(entry.name.lower())[1]
                if ext not in _ALLOWED_EXTENSIONS:
                    continue
                if not is_within_root(entry.path, reports_dir):
                    continue
                stat = entry.stat(follow_symlinks=False)
                reports.append(self._summary_from_stat(entry.path, entry.name, stat))
        except OSError:
            return []

        reports.sort(key=lambda item: item.mtime, reverse=True)
        if limit > 0:
            return reports[: int(limit)]
        return reports

    def get_report(self, session: Any, report_id: str) -> ReportContent:
        reports_dir = self.reports_dir(session)
        name = self._safe_report_id(report_id)
        path = os.path.join(reports_dir, name)
        if not is_within_root(path, reports_dir):
            raise InvalidReportIdError("invalid report id")
        if not os.path.isfile(path):
            raise ReportNotFoundError(name)

        stat = os.stat(path)
        summary = self._summary_from_stat(path, name, stat)
        content: Optional[str] = None
        if summary.readable:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        return ReportContent(summary=summary, content=content)

    def get_report_path(self, session: Any, report_id: str) -> str:
        return self.get_report(session, report_id).summary.path

    def save_markdown_report(
        self,
        session: Any,
        content: str,
        *,
        prefix: str = "report",
        title: str = "",
        now: Optional[float] = None,
    ) -> ReportSummary:
        reports_dir = self.ensure_reports_dir(session)
        filename = self._next_report_filename(reports_dir, prefix=prefix, now=now)
        path = os.path.join(reports_dir, filename)
        if not is_within_root(path, reports_dir):
            raise InvalidReportIdError("invalid report target")
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(content or ""))
        stat = os.stat(path)
        return self._summary_from_stat(path, filename, stat, title=title)

    def save_manager_plan_report(self, session: Any, plan: Any) -> ReportSummary:
        content = self.build_manager_plan_markdown(plan)
        return self.save_markdown_report(
            session,
            content,
            prefix="manager_plan",
            title=str(getattr(plan, "project_goal", "") or "Manager plan report"),
        )

    def build_manager_plan_markdown(self, plan: Any) -> str:
        goal = str(getattr(plan, "project_goal", "") or "Project Report")
        lines = [f"# Project Report: {goal}"]
        created_at = str(getattr(plan, "created_at", "") or "").strip()
        updated_at = str(getattr(plan, "updated_at", "") or "").strip()
        status = str(getattr(plan, "status", "") or "").strip()
        if created_at:
            lines.append(f"**Created:** {created_at}")
        if updated_at:
            lines.append(f"**Updated:** {updated_at}")
        if status:
            lines.append(f"**Status:** {status.upper()}")
        lines.append("")

        analysis = getattr(plan, "analysis", None)
        if analysis is not None:
            current_state = str(getattr(analysis, "current_state", "") or "").strip()
            remaining_work = list(getattr(analysis, "remaining_work", []) or [])
            if current_state or remaining_work:
                lines.append("## Analysis")
                if current_state:
                    lines.append(f"**Current State:** {current_state}")
                    lines.append("")
                if remaining_work:
                    lines.append("### Remaining Work")
                    for item in remaining_work:
                        lines.append(f"- {item}")
                    lines.append("")

        tasks = list(getattr(plan, "tasks", []) or [])
        lines.append("## Task Tree")
        if not tasks:
            lines.append("_No tasks in the current plan._")
        else:
            lines.extend(self._format_task_tree(tasks))
        lines.append("")
        lines.append("---")
        lines.append("Generated by cli-proxy")
        return "\n".join(lines)

    def _format_task_tree(self, tasks: Iterable[Any]) -> list[str]:
        task_list = list(tasks or [])
        tasks_by_id = {str(getattr(task, "id", "") or ""): task for task in task_list}
        dependents: dict[str, list[str]] = {task_id: [] for task_id in tasks_by_id}
        roots: list[str] = []
        for task in task_list:
            task_id = str(getattr(task, "id", "") or "")
            deps = list(getattr(task, "depends_on", []) or [])
            if not deps:
                roots.append(task_id)
                continue
            known_dep = False
            for dep_id_raw in deps:
                dep_id = str(dep_id_raw or "")
                if dep_id in dependents:
                    dependents[dep_id].append(task_id)
                    known_dep = True
            if not known_dep and task_id not in roots:
                roots.append(task_id)

        lines: list[str] = []
        added: set[str] = set()

        def add_task(task_id: str, level: int) -> None:
            if task_id in added:
                return
            task = tasks_by_id.get(task_id)
            if task is None:
                return
            indent = "  " * level
            status = str(getattr(task, "status", "") or "").strip().upper() or "TODO"
            title = str(getattr(task, "title", "") or "").strip() or task_id
            lines.append(f"{indent}- **[{status}]** {title} `({task_id})`")
            description = str(getattr(task, "description", "") or "").strip()
            if description:
                lines.append(f"{indent}  _{description}_")
            added.add(task_id)
            for child_id in dependents.get(task_id, []):
                add_task(child_id, level + 1)

        for root_id in roots:
            add_task(root_id, 0)
        for task_id in tasks_by_id:
            add_task(task_id, 0)
        return lines

    def _safe_report_id(self, value: str) -> str:
        raw = str(value or "").strip()
        name = os.path.basename(raw)
        if not name or name != raw or "/" in raw or "\\" in raw:
            raise InvalidReportIdError("invalid report id")
        ext = os.path.splitext(name.lower())[1]
        if ext not in _ALLOWED_EXTENSIONS:
            raise InvalidReportIdError("invalid report extension")
        return name

    def _next_report_filename(self, reports_dir: str, *, prefix: str, now: Optional[float]) -> str:
        safe_prefix = _SAFE_PREFIX_RE.sub("_", str(prefix or "report")).strip("._-") or "report"
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now if now is not None else time.time()))
        filename = f"{safe_prefix}_{timestamp}.md"
        if not os.path.exists(os.path.join(reports_dir, filename)):
            return filename
        for idx in range(1, 1000):
            candidate = f"{safe_prefix}_{timestamp}_{idx}.md"
            if not os.path.exists(os.path.join(reports_dir, candidate)):
                return candidate
        raise ReportHistoryError("unable to allocate report filename")

    def _summary_from_stat(
        self,
        path: str,
        name: str,
        stat: os.stat_result,
        *,
        title: str = "",
    ) -> ReportSummary:
        ext = os.path.splitext(name.lower())[1]
        fmt = ext.lstrip(".")
        content_type = {
            ".md": "text/markdown",
            ".html": "text/html",
            ".pdf": "application/pdf",
        }.get(ext, "application/octet-stream")
        return ReportSummary(
            report_id=name,
            name=name,
            path=path,
            size=int(stat.st_size),
            mtime=float(stat.st_mtime),
            date=_dt.datetime.fromtimestamp(
                float(stat.st_mtime),
                tz=_dt.timezone.utc,
            ).strftime("%Y-%m-%d %H:%M UTC"),
            fmt=fmt,
            content_type=content_type,
            readable=ext in _TEXT_EXTENSIONS,
            title=title or name,
        )
