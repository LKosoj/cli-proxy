from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.services.report_history_service import ReportHistoryService, ReportSummary
from app.services.session_transfer.service import extract_session
from session import session_active_cli_name, session_runtime_uid
from sessions.session_state_access import get_active_mode

logger = logging.getLogger(__name__)


ExtractSessionFn = Callable[[str, str, str], Any]


@dataclass(frozen=True)
class SessionSnapshotReport:
    title: str
    html: str


class SessionSnapshotReportService:
    """Builds a self-contained HTML snapshot from the current session state."""

    def __init__(
        self,
        *,
        report_history_service: ReportHistoryService,
        run_artifacts_service: Any = None,
        extract_session_fn: ExtractSessionFn = extract_session,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.report_history_service = report_history_service
        self.run_artifacts_service = run_artifacts_service
        self._extract_session = extract_session_fn
        self._now = now_fn

    def save_html_report(self, session: Any, *, now: Optional[float] = None) -> ReportSummary:
        snapshot = self.build_html_report(session, now=now)
        return self.report_history_service.save_html_report(
            session,
            snapshot.html,
            prefix="session_snapshot",
            title=snapshot.title,
            now=now,
        )

    def build_html_report(self, session: Any, *, now: Optional[float] = None) -> SessionSnapshotReport:
        generated_at = float(now if now is not None else self._now())
        session_uid = session_runtime_uid(session) or str(getattr(session, "id", "") or "session")
        active_mode = str(get_active_mode(session, "") or "").strip()
        active_cli = session_active_cli_name(session)
        title = f"Session snapshot: {session_uid}"
        summary_rows = [
            ("Session UID", session_uid),
            ("Session ID", str(getattr(session, "id", "") or "-")),
            ("Name", str(getattr(session, "name", "") or "-")),
            ("Workdir", str(getattr(session, "workdir", "") or "-")),
            ("Active CLI", active_cli or "-"),
            ("Active mode", active_mode or "direct CLI"),
            ("Busy", "yes" if bool(getattr(session, "busy", False)) else "no"),
            ("Queue length", str(len(list(getattr(session, "queue", []) or [])))),
            ("Generated", _format_ts(generated_at)),
        ]
        body = [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_e(title)}</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            "<main>",
            f"<h1>{_e(title)}</h1>",
            '<p class="lede">Generated from session metadata, saved reports, native CLI transcript, and run artifacts.</p>',
            _details("Session", _table(summary_rows), open_=True),
            _details("Mode run artifacts", self._render_runs(session, mode_id=active_mode), open_=True),
            _details("Saved report history", self._render_report_history(session), open_=False),
            _details("Chat transcript excerpt", self._render_chat_excerpt(session, active_cli=active_cli), open_=False),
            "</main>",
            "</body>",
            "</html>",
        ]
        return SessionSnapshotReport(title=title, html="\n".join(body))

    def _render_runs(self, session: Any, *, mode_id: str) -> str:
        service = self.run_artifacts_service
        if service is None:
            return _empty("Run artifact service is not available.")
        try:
            if hasattr(service, "is_enabled") and not bool(service.is_enabled()):
                return _empty("Run artifacts are disabled in runtime config.")
            store = getattr(service, "artifact_store", service)
            list_runs = getattr(store, "list_runs", None)
            if not callable(list_runs):
                return _empty("Run artifact store is not available.")
            runs = list_runs(session=session, mode_id=(mode_id or None), limit=5)
        except Exception:
            logger.exception("session snapshot failed to list run artifacts")
            return _empty("Failed to read run artifacts.")
        if not runs:
            scope = f" for active mode {mode_id}" if mode_id else ""
            return _empty(f"No run artifacts{scope}.")

        chunks: list[str] = []
        for run in runs:
            chunks.append(self._render_run(store, run))
        return "\n".join(chunks)

    def _render_run(self, store: Any, run: Any) -> str:
        state = _safe_call_dict(store, "load_state", run)
        plan = _safe_call_dict(store, "load_plan", run)
        checkpoints = _safe_call_dict(store, "load_checkpoints", run)
        metrics = _safe_call_dict(store, "load_metrics", run)
        recovery = _safe_call_dict(store, "load_recovery", run)
        events = _safe_call_list(store, "load_events_tail", run, limit=8)
        checkpoint_items = list(checkpoints.get("items") or []) if isinstance(checkpoints, dict) else []
        artifact_files = _list_files(getattr(run, "artifacts_dir", ""), limit=20)
        rows = [
            ("Mode", getattr(run, "mode_id", "") or state.get("mode_id") or "-"),
            ("Run ID", getattr(run, "run_id", "") or state.get("run_id") or "-"),
            ("Status", state.get("status") or "-"),
            ("Phase", state.get("phase") or "-"),
            ("Started", _format_ts(state.get("started_at"))),
            ("Updated", _format_ts(state.get("updated_at"))),
            ("Finished", _format_ts(state.get("finished_at"))),
            ("Checkpoints", str(len(checkpoint_items))),
            ("Artifact files", str(len(artifact_files))),
        ]
        content = [
            _table(rows),
            "<h3>Plan</h3>",
            _json_block(_short_json(plan)),
            "<h3>Metrics</h3>",
            _json_block(_short_json(metrics)),
            "<h3>Recovery</h3>",
            _json_block(_short_json(recovery)),
            "<h3>Recent checkpoints</h3>",
            _json_block(_short_json(checkpoint_items[-5:])),
            "<h3>Recent events</h3>",
            _json_block(_short_json(events)),
            "<h3>Artifact files</h3>",
            _list_block(artifact_files),
        ]
        title = f"{getattr(run, 'mode_id', '-')}/{getattr(run, 'run_id', '-')}"
        return _details(str(title), "\n".join(content), open_=False)

    def _render_report_history(self, session: Any) -> str:
        try:
            reports = self.report_history_service.list_reports(session, limit=10)
        except Exception:
            logger.exception("session snapshot failed to list saved reports")
            return _empty("Failed to read saved reports.")
        if not reports:
            return _empty("No saved reports yet.")
        rows = [
            (
                item.report_id,
                item.date,
                item.fmt,
                f"{item.size} bytes",
            )
            for item in reports
        ]
        return _table(rows, headers=("Report", "Date", "Format", "Size"))

    def _render_chat_excerpt(self, session: Any, *, active_cli: str) -> str:
        resume_token = _resume_token(session, active_cli)
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not active_cli:
            return _empty("Active CLI is not set.")
        if not resume_token:
            return _empty("Active CLI resume token is not available.")
        if not workdir:
            return _empty("Session workdir is not set.")
        try:
            canonical = self._extract_session(active_cli, resume_token, workdir)
        except Exception:
            logger.exception("session snapshot failed to extract CLI transcript")
            canonical = None
        messages = list(getattr(canonical, "messages", []) or []) if canonical is not None else []
        if not messages:
            return _empty("Native CLI transcript is not available.")
        role_counts: dict[str, int] = {}
        for msg in messages:
            role = str(getattr(msg, "role", "") or "unknown").strip() or "unknown"
            role_counts[role] = role_counts.get(role, 0) + 1
        chunks = [
            _table(
                [
                    ("Source CLI", str(getattr(canonical, "source_cli", "") or active_cli)),
                    ("Source session", str(getattr(canonical, "session_id", "") or resume_token)),
                    ("Messages", str(len(messages))),
                    ("Role counts", ", ".join(f"{key}: {value}" for key, value in sorted(role_counts.items()))),
                    ("Extracted", _format_ts(getattr(canonical, "extracted_at", None))),
                ]
            )
        ]
        for idx, msg in enumerate(messages[-8:], start=max(1, len(messages) - 7)):
            role = str(getattr(msg, "role", "") or "message").strip() or "message"
            ts = _format_ts(getattr(msg, "timestamp", None))
            text = _truncate(str(getattr(msg, "content", "") or ""), 2500)
            chunks.append(
                f'<article class="message"><h3>{idx}. {_e(role)} <span>{_e(ts)}</span></h3>'
                f"<pre>{_e(text)}</pre></article>"
            )
        return "\n".join(chunks)


def _safe_call_dict(store: Any, name: str, run: Any) -> dict[str, Any]:
    fn = getattr(store, name, None)
    if not callable(fn):
        return {}
    try:
        value = fn(run)
    except Exception:
        logger.exception("session snapshot %s failed run_id=%s", name, getattr(run, "run_id", ""))
        return {}
    return value if isinstance(value, dict) else {}


def _safe_call_list(store: Any, name: str, run: Any, **kwargs: Any) -> list[Any]:
    fn = getattr(store, name, None)
    if not callable(fn):
        return []
    try:
        value = fn(run, **kwargs)
    except Exception:
        logger.exception("session snapshot %s failed run_id=%s", name, getattr(run, "run_id", ""))
        return []
    return list(value or []) if isinstance(value, list) else []


def _resume_token(session: Any, active_cli: str) -> str:
    token = str(getattr(session, "resume_token", "") or "").strip()
    if token:
        return token
    tokens = getattr(getattr(session, "cli", None), "resume_tokens", None)
    if isinstance(tokens, dict):
        return str(tokens.get(active_cli) or "").strip()
    return ""


def _format_ts(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return str(value)
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _truncate(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 25)] + "\n[truncated]"


def _short_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        text = str(value)
    return _truncate(text, 6000)


def _json_block(value: Any) -> str:
    return f"<pre>{_e(value)}</pre>"


def _table(rows: list[tuple[Any, ...]], headers: tuple[str, ...] = ("Field", "Value")) -> str:
    header_html = "".join(f"<th>{_e(item)}</th>" for item in headers)
    row_html = []
    for row in rows:
        cells = "".join(f"<td>{_e(item)}</td>" for item in row)
        row_html.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{''.join(row_html)}</tbody></table>"


def _details(title: str, content: str, *, open_: bool) -> str:
    open_attr = " open" if open_ else ""
    return f"<details{open_attr}><summary>{_e(title)}</summary>{content}</details>"


def _empty(text: str) -> str:
    return f'<p class="empty">{_e(text)}</p>'


def _list_block(items: list[str]) -> str:
    if not items:
        return _empty("No files.")
    return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in items) + "</ul>"


def _list_files(root: str, *, limit: int) -> list[str]:
    base = str(root or "").strip()
    if not base or not os.path.isdir(base):
        return []
    items: list[str] = []
    try:
        for current_root, _dir_names, file_names in os.walk(base):
            for name in sorted(file_names):
                path = os.path.join(current_root, name)
                try:
                    rel = os.path.relpath(path, base)
                    size = os.path.getsize(path)
                except OSError:
                    continue
                items.append(f"{rel} ({size} bytes)")
                if len(items) >= limit:
                    return items
    except OSError:
        return items
    return items


_CSS = """
:root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
body { margin: 0; background: #f6f7f9; color: #18202a; }
main { max-width: 1080px; margin: 0 auto; padding: 32px 20px 56px; }
h1 { margin: 0 0 8px; font-size: 28px; line-height: 1.2; }
.lede { margin: 0 0 24px; color: #5b6675; }
details { margin: 14px 0; border: 1px solid #d9dee7; border-radius: 8px; background: #ffffff; }
summary { cursor: pointer; padding: 13px 16px; font-weight: 700; }
details > :not(summary) { margin-left: 16px; margin-right: 16px; }
table { width: calc(100% - 32px); border-collapse: collapse; margin: 0 16px 16px; font-size: 14px; }
th, td { border-top: 1px solid #e6e9ef; padding: 8px 10px; text-align: left; vertical-align: top; }
th { color: #4b5563; font-size: 12px; text-transform: uppercase; }
pre {
  overflow: auto; margin: 0 16px 16px; padding: 12px; border-radius: 6px;
  background: #111827; color: #f9fafb; font-size: 13px; line-height: 1.45;
}
h3 { margin: 16px 16px 8px; font-size: 15px; }
h3 span { color: #6b7280; font-weight: 400; }
ul { margin: 0 16px 16px; padding-left: 20px; }
.empty { margin: 0 16px 16px; color: #687386; }
.message { border-top: 1px solid #e6e9ef; }
@media (prefers-color-scheme: dark) {
  body { background: #111827; color: #e5e7eb; }
  .lede, .empty, h3 span { color: #9ca3af; }
  details { background: #1f2937; border-color: #374151; }
  th, td, .message { border-color: #374151; }
  th { color: #9ca3af; }
  pre { background: #030712; }
}
"""
