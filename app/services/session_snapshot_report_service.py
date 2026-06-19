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
    """Builds a human-readable HTML report from the current session state."""

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
        title = f"Отчёт по сессии: {session_uid}"
        summary_rows = [
            ("UID сессии", session_uid),
            ("ID сессии", str(getattr(session, "id", "") or "-")),
            ("Название", str(getattr(session, "name", "") or "-")),
            ("Рабочая папка", str(getattr(session, "workdir", "") or "-")),
            ("Активный CLI", active_cli or "-"),
            ("Активный режим", active_mode or "прямой CLI"),
            ("Состояние", _busy_text(session)),
            ("Очередь", _queue_text(session)),
            ("Сформирован", _format_ts(generated_at)),
        ]
        body = [
            "<!doctype html>",
            '<html lang="ru">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_e(title)}</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            "<main>",
            '<section class="hero">',
            f"<h1>{_e(title)}</h1>",
            (
                '<p class="lede">Короткая версия для чтения человеком: '
                "что за сессия, чем она занята, какие артефакты и сообщения "
                "важны сейчас. Технические детали оставлены ниже для проверки.</p>"
            ),
            "</section>",
            _section("Коротко", self._render_overview(session, active_mode=active_mode, active_cli=active_cli)),
            _section("Артефакты и прогресс", self._render_runs(session, mode_id=active_mode)),
            _section("Последние сообщения", self._render_chat_excerpt(session, active_cli=active_cli)),
            _section("Сохранённые отчёты", self._render_report_history(session)),
            _details("Техническая карточка сессии", _table(summary_rows), open_=False),
            "</main>",
            "</body>",
            "</html>",
        ]
        return SessionSnapshotReport(title=title, html="\n".join(body))

    def _render_overview(self, session: Any, *, active_mode: str, active_cli: str) -> str:
        name = str(getattr(session, "name", "") or "").strip()
        workdir = str(getattr(session, "workdir", "") or "").strip()
        bullets = [
            f"Сессия: {name or session_runtime_uid(session) or getattr(session, 'id', '-')}",
            f"Сейчас: {_busy_text(session)}, {_queue_text(session).lower()}.",
            f"Контекст: {active_mode or 'прямой CLI'} через {active_cli or 'не выбранный CLI'}.",
        ]
        if workdir:
            bullets.append(f"Рабочая папка: {workdir}.")
        bullets.append(
            "Ниже собраны последние артефакты активного режима, сохранённые отчёты "
            "и доступный фрагмент чата."
        )
        return _bullet_list(bullets, class_name="human-list")

    def _render_runs(self, session: Any, *, mode_id: str) -> str:
        service = self.run_artifacts_service
        if service is None:
            return _empty("Сервис артефактов недоступен.")
        try:
            if hasattr(service, "is_enabled") and not bool(service.is_enabled()):
                return _empty("Артефакты запусков отключены в runtime config.")
            store = getattr(service, "artifact_store", service)
            list_runs = getattr(store, "list_runs", None)
            if not callable(list_runs):
                return _empty("Хранилище артефактов недоступно.")
            runs = list_runs(session=session, mode_id=(mode_id or None), limit=5)
        except Exception:
            logger.exception("session snapshot failed to list run artifacts")
            return _empty("Не удалось прочитать артефакты запусков.")
        if not runs:
            scope = f" для активного режима {mode_id}" if mode_id else ""
            return _empty(f"Артефактов запусков{scope} пока нет.")

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
            ("Режим", getattr(run, "mode_id", "") or state.get("mode_id") or "-"),
            ("Run ID", getattr(run, "run_id", "") or state.get("run_id") or "-"),
            ("Статус", state.get("status") or "-"),
            ("Фаза", state.get("phase") or "-"),
            ("Начат", _format_ts(state.get("started_at"))),
            ("Обновлён", _format_ts(state.get("updated_at"))),
            ("Завершён", _format_ts(state.get("finished_at"))),
            ("Чекпоинты", str(len(checkpoint_items))),
            ("Файлы", str(len(artifact_files))),
        ]
        technical = [
            _table(rows),
            "<h3>PLAN</h3>",
            _json_block(_short_json(plan)),
            "<h3>METRICS</h3>",
            _json_block(_short_json(metrics)),
            "<h3>RECOVERY</h3>",
            _json_block(_short_json(recovery)),
            "<h3>Последние checkpoints</h3>",
            _json_block(_short_json(checkpoint_items[-5:])),
            "<h3>Последние events</h3>",
            _json_block(_short_json(events)),
            "<h3>Файлы артефактов</h3>",
            _list_block(artifact_files),
        ]
        content = [
            _run_human_summary(
                run=run,
                state=state,
                plan=plan,
                checkpoints=checkpoint_items,
                events=events,
                artifact_files=artifact_files,
            ),
            _details("Технические данные запуска", "\n".join(technical), open_=False),
        ]
        title = f"{getattr(run, 'mode_id', '-')}/{getattr(run, 'run_id', '-')}"
        return _details(str(title), "\n".join(content), open_=False)

    def _render_report_history(self, session: Any) -> str:
        try:
            reports = self.report_history_service.list_reports(session, limit=10)
        except Exception:
            logger.exception("session snapshot failed to list saved reports")
            return _empty("Не удалось прочитать сохранённые отчёты.")
        if not reports:
            return _empty("Сохранённых отчётов пока нет.")
        rows = [
            (
                item.report_id,
                item.date,
                item.fmt,
                _format_bytes(item.size),
            )
            for item in reports
        ]
        return _table(rows, headers=("Отчёт", "Дата", "Формат", "Размер"))

    def _render_chat_excerpt(self, session: Any, *, active_cli: str) -> str:
        resume_token = _resume_token(session, active_cli)
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not active_cli:
            return _empty("Активный CLI не выбран.")
        if not resume_token:
            return _empty("Resume token активного CLI недоступен.")
        if not workdir:
            return _empty("Рабочая папка сессии не задана.")
        try:
            canonical = self._extract_session(active_cli, resume_token, workdir)
        except Exception:
            logger.exception("session snapshot failed to extract CLI transcript")
            canonical = None
        messages = list(getattr(canonical, "messages", []) or []) if canonical is not None else []
        if not messages:
            return _empty("Transcript нативного CLI недоступен.")
        role_counts: dict[str, int] = {}
        for msg in messages:
            role = str(getattr(msg, "role", "") or "unknown").strip() or "unknown"
            role_counts[role] = role_counts.get(role, 0) + 1
        chunks = [
            (
                '<p class="note">'
                f"Показаны последние сообщения из {len(messages)}. "
                f"Источник: {_e(getattr(canonical, 'source_cli', '') or active_cli)}. "
                f"Роли: {_e(', '.join(f'{key}: {value}' for key, value in sorted(role_counts.items())))}."
                "</p>"
            )
        ]
        for idx, msg in enumerate(messages[-8:], start=max(1, len(messages) - 7)):
            role = str(getattr(msg, "role", "") or "message").strip() or "message"
            ts = _format_ts(getattr(msg, "timestamp", None))
            text = _truncate(str(getattr(msg, "content", "") or ""), 2500)
            chunks.append(
                f'<article class="message role-{_css_token(role)}">'
                f"<h3>{idx}. {_e(_role_label(role))} <span>{_e(ts)}</span></h3>"
                f"<p>{_e(text)}</p></article>"
            )
        return "\n".join(chunks)


def _busy_text(session: Any) -> str:
    return "сессия сейчас выполняет задачу" if bool(getattr(session, "busy", False)) else "сессия свободна"


def _queue_text(session: Any) -> str:
    size = len(list(getattr(session, "queue", []) or []))
    if size == 0:
        return "Очередь пуста"
    if size == 1:
        return "В очереди 1 задача"
    if 2 <= size <= 4:
        return f"В очереди {size} задачи"
    return f"В очереди {size} задач"


def _section(title: str, content: str) -> str:
    return f'<section class="panel"><h2>{_e(title)}</h2>{content}</section>'


def _bullet_list(items: list[str], *, class_name: str = "") -> str:
    class_attr = f' class="{_e(class_name)}"' if class_name else ""
    return "<ul%s>%s</ul>" % (
        class_attr,
        "".join(f"<li>{_e(item)}</li>" for item in items if str(item or "").strip()),
    )


def _run_human_summary(
    *,
    run: Any,
    state: dict[str, Any],
    plan: dict[str, Any],
    checkpoints: list[Any],
    events: list[Any],
    artifact_files: list[str],
) -> str:
    mode_id = str(getattr(run, "mode_id", "") or state.get("mode_id") or "-")
    run_id = str(getattr(run, "run_id", "") or state.get("run_id") or "-")
    status = str(state.get("status") or "-")
    phase = str(state.get("phase") or "-")
    bullets = [
        f"Запуск {run_id} в режиме {mode_id}: {_status_text(status)}, фаза {phase}.",
        _describe_plan(plan),
        _describe_latest("Последний чекпоинт", checkpoints),
        _describe_latest("Последнее событие", events),
        _describe_artifact_files(artifact_files),
    ]
    return '<div class="run-card"><h3>Что важно</h3>%s</div>' % _bullet_list(bullets, class_name="human-list")


def _status_text(status: str) -> str:
    normalized = str(status or "").strip().lower()
    labels = {
        "completed": "завершён",
        "done": "завершён",
        "failed": "завершился с ошибкой",
        "error": "завершился с ошибкой",
        "cancelled": "остановлен",
        "canceled": "остановлен",
        "running": "в работе",
        "in_progress": "в работе",
        "active": "в работе",
    }
    return labels.get(normalized, status or "статус не указан")


def _describe_plan(plan: dict[str, Any]) -> str:
    if not plan:
        return "План запуска не сохранён."
    for key in ("goal", "summary", "task", "title"):
        value = str(plan.get(key) or "").strip()
        if value:
            return f"План: {_truncate(value, 220)}"
    units = plan.get("units") or plan.get("tasks") or []
    if isinstance(units, list) and units:
        names = []
        for item in units[:3]:
            if isinstance(item, dict):
                names.append(str(item.get("title") or item.get("name") or item.get("id") or "шаг").strip())
            else:
                names.append(str(item).strip())
        suffix = f" и ещё {len(units) - 3}" if len(units) > 3 else ""
        return f"План содержит {len(units)} шаг(ов): {', '.join(names)}{suffix}."
    family = str(plan.get("task_family") or "").strip()
    if family:
        return f"Тип задачи в плане: {family}."
    return "План сохранён, но в нём нет короткого описания."


def _describe_latest(label: str, values: list[Any]) -> str:
    if not values:
        return f"{label}: нет данных."
    value = values[-1]
    if isinstance(value, dict):
        for key in ("message", "summary", "title", "status", "phase", "event_type", "current_step_id"):
            text = str(value.get(key) or "").strip()
            if text:
                return f"{label}: {_truncate(text, 220)}."
    return f"{label}: {_truncate(str(value), 220)}."


def _describe_artifact_files(files: list[str]) -> str:
    if not files:
        return "Файлы артефактов не сохранены."
    visible = files[:3]
    suffix = f" и ещё {len(files) - 3}" if len(files) > 3 else ""
    return f"Файлы артефактов: {', '.join(visible)}{suffix}."


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


def _format_bytes(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size)
    units = ("B", "KB", "MB", "GB")
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}"


def _role_label(role: str) -> str:
    labels = {
        "user": "Пользователь",
        "assistant": "Ассистент",
        "system": "Система",
        "tool": "Инструмент",
    }
    return labels.get(str(role or "").strip().lower(), role or "Сообщение")


def _css_token(value: str) -> str:
    token = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or "message"))
    return token.strip("-") or "message"


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
.hero { margin-bottom: 18px; }
.panel {
  margin: 16px 0; padding: 18px 18px 20px; border: 1px solid #d9dee7;
  border-radius: 8px; background: #ffffff;
}
h2 { margin: 0 0 12px; font-size: 19px; line-height: 1.25; }
.human-list { margin: 0; padding-left: 22px; }
.human-list li { margin: 8px 0; line-height: 1.5; }
.note { margin: 0 0 14px; color: #4b5563; line-height: 1.5; }
.run-card { margin: 0 16px 16px; padding: 12px 14px; border: 1px solid #e6e9ef; border-radius: 8px; }
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
.message p {
  margin: 0 16px 16px; white-space: pre-wrap; line-height: 1.5; color: #273241;
}
@media (prefers-color-scheme: dark) {
  body { background: #111827; color: #e5e7eb; }
  .lede, .empty, .note, h3 span { color: #9ca3af; }
  .panel, details { background: #1f2937; border-color: #374151; }
  .run-card, th, td, .message { border-color: #374151; }
  th { color: #9ca3af; }
  pre { background: #030712; }
  .message p { color: #d1d5db; }
}
"""
