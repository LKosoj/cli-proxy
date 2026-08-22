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
from app.services.session_transfer.canonical import TOOL_CALL_MARKER_RE, strip_tool_calls
from app.services.session_transfer.service import extract_session
from session import session_active_cli_name, session_runtime_uid
from sessions.session_state_access import get_active_mode

logger = logging.getLogger(__name__)


ExtractSessionFn = Callable[[str, str, str], Any]

_CHAT_REPLIES_LIMIT = 50
_TOOL_TAIL_LIMIT = 20
_TOOL_OUTPUT_MAX_CHARS = 600


@dataclass(frozen=True)
class SessionSnapshotReport:
    title: str
    html: str


@dataclass(frozen=True)
class _ReportCopy:
    lang: str
    text: dict[str, str]
    statuses: dict[str, str]
    roles: dict[str, str]

    def t(self, key: str, **kwargs: Any) -> str:
        value = self.text.get(key, _REPORT_COPIES["en"].text.get(key, key))
        return value.format(**kwargs)


_REPORT_COPIES: dict[str, _ReportCopy] = {
    "en": _ReportCopy(
        lang="en",
        text={
            "title": "Session report: {session_uid}",
            "lede": (
                "A human-readable summary: what this session is, what it is doing, "
                "which artifacts and messages matter now. Technical details remain "
                "below for verification."
            ),
            "overview_title": "At a glance",
            "runs_title": "Artifacts and progress",
            "chat_title": "Recent agent replies",
            "reports_title": "Saved reports",
            "session_card_title": "Technical session card",
            "run_card_title": "What matters",
            "run_technical_title": "Technical run data",
            "field": "Field",
            "value": "Value",
            "session_uid": "Session UID",
            "session_id": "Session ID",
            "name": "Name",
            "workdir": "Workdir",
            "active_cli": "Active CLI",
            "active_mode": "Active mode",
            "status": "Status",
            "queue": "Queue",
            "generated": "Generated",
            "direct_cli": "direct CLI",
            "no_cli": "no CLI selected",
            "busy_running": "session is running a task",
            "busy_idle": "session is idle",
            "queue_empty": "Queue is empty",
            "queue_one": "1 item in queue",
            "queue_count": "{n} items in queue",
            "overview_session": "Session: {name}",
            "overview_now": "Now: {busy}. {queue}.",
            "overview_context": "Context: {mode} via {cli}.",
            "overview_workdir": "Workdir: {workdir}.",
            "overview_footer": (
                "Below are the latest artifacts for the active mode, saved reports, "
                "and the available chat excerpt."
            ),
            "artifacts_service_missing": "Artifact service is unavailable.",
            "artifacts_disabled": "Run artifacts are disabled in runtime config.",
            "artifact_store_missing": "Artifact storage is unavailable.",
            "artifacts_read_failed": "Failed to read run artifacts.",
            "no_artifacts": "No run artifacts{scope} yet.",
            "active_mode_scope": " for active mode {mode_id}",
            "run_mode": "Mode",
            "run_id": "Run ID",
            "phase": "Phase",
            "started": "Started",
            "updated": "Updated",
            "finished": "Finished",
            "checkpoints": "Checkpoints",
            "files": "Files",
            "run_summary": "Run {run_id} in mode {mode_id}: {status}, phase {phase}.",
            "plan_missing": "The run plan is not saved.",
            "plan_text": "Plan: {text}",
            "plan_steps": "The plan has {count} step(s): {names}{suffix}.",
            "plan_more": " and {count} more",
            "plan_family": "Plan task type: {family}.",
            "plan_no_summary": "The plan is saved, but it has no short description.",
            "last_checkpoint": "Last checkpoint",
            "last_event": "Last event",
            "latest_missing": "{label}: no data.",
            "latest_value": "{label}: {text}.",
            "artifact_files_missing": "No artifact files were saved.",
            "artifact_files": "Artifact files: {files}{suffix}.",
            "artifact_files_more": " and {count} more",
            "reports_read_failed": "Failed to read saved reports.",
            "reports_empty": "No saved reports yet.",
            "report": "Report",
            "date": "Date",
            "format": "Format",
            "size": "Size",
            "chat_no_cli": "Active CLI is not set.",
            "chat_no_token": "Active CLI resume token is unavailable.",
            "chat_no_workdir": "Session workdir is not set.",
            "chat_missing": "Native CLI transcript is unavailable.",
            "chat_no_replies": "The CLI transcript has no agent replies yet.",
            "chat_note": "Latest agent replies: {shown} of {count}. Source: {source}. Tool calls are omitted.",
            "tool_tail_title": "Tool activity after the last reply",
            "tool_tail_note": "Steps after the last reply: {shown} of {count}. Output is truncated.",
            "tool_call": "Call: {name}",
            "tool_output": "Tool output",
            "status_missing": "status is not specified",
            "message": "Message",
        },
        statuses={
            "completed": "completed",
            "done": "completed",
            "failed": "failed",
            "error": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "running": "running",
            "in_progress": "running",
            "active": "running",
        },
        roles={
            "user": "User",
            "assistant": "Assistant",
            "system": "System",
            "tool": "Tool",
        },
    ),
    "ru": _ReportCopy(
        lang="ru",
        text={
            "title": "Отчёт по сессии: {session_uid}",
            "lede": (
                "Короткая версия для чтения человеком: что за сессия, чем она "
                "занята, какие артефакты и сообщения важны сейчас. Технические "
                "детали оставлены ниже для проверки."
            ),
            "overview_title": "Коротко",
            "runs_title": "Артефакты и прогресс",
            "chat_title": "Последние ответы агента",
            "reports_title": "Сохранённые отчёты",
            "session_card_title": "Техническая карточка сессии",
            "run_card_title": "Что важно",
            "run_technical_title": "Технические данные запуска",
            "field": "Поле",
            "value": "Значение",
            "session_uid": "UID сессии",
            "session_id": "ID сессии",
            "name": "Название",
            "workdir": "Рабочая папка",
            "active_cli": "Активный CLI",
            "active_mode": "Активный режим",
            "status": "Состояние",
            "queue": "Очередь",
            "generated": "Сформирован",
            "direct_cli": "прямой CLI",
            "no_cli": "не выбранный CLI",
            "busy_running": "сессия сейчас выполняет задачу",
            "busy_idle": "сессия свободна",
            "queue_empty": "Очередь пуста",
            "queue_one": "В очереди 1 задача",
            "queue_few": "В очереди {n} задачи",
            "queue_count": "В очереди {n} задач",
            "overview_session": "Сессия: {name}",
            "overview_now": "Сейчас: {busy}. {queue}.",
            "overview_context": "Контекст: {mode} через {cli}.",
            "overview_workdir": "Рабочая папка: {workdir}.",
            "overview_footer": (
                "Ниже собраны последние артефакты активного режима, сохранённые "
                "отчёты и доступный фрагмент чата."
            ),
            "artifacts_service_missing": "Сервис артефактов недоступен.",
            "artifacts_disabled": "Артефакты запусков отключены в runtime config.",
            "artifact_store_missing": "Хранилище артефактов недоступно.",
            "artifacts_read_failed": "Не удалось прочитать артефакты запусков.",
            "no_artifacts": "Артефактов запусков{scope} пока нет.",
            "active_mode_scope": " для активного режима {mode_id}",
            "run_mode": "Режим",
            "run_id": "Run ID",
            "phase": "Фаза",
            "started": "Начат",
            "updated": "Обновлён",
            "finished": "Завершён",
            "checkpoints": "Чекпоинты",
            "files": "Файлы",
            "run_summary": "Запуск {run_id} в режиме {mode_id}: {status}, фаза {phase}.",
            "plan_missing": "План запуска не сохранён.",
            "plan_text": "План: {text}",
            "plan_steps": "План содержит {count} шаг(ов): {names}{suffix}.",
            "plan_more": " и ещё {count}",
            "plan_family": "Тип задачи в плане: {family}.",
            "plan_no_summary": "План сохранён, но в нём нет короткого описания.",
            "last_checkpoint": "Последний чекпоинт",
            "last_event": "Последнее событие",
            "latest_missing": "{label}: нет данных.",
            "latest_value": "{label}: {text}.",
            "artifact_files_missing": "Файлы артефактов не сохранены.",
            "artifact_files": "Файлы артефактов: {files}{suffix}.",
            "artifact_files_more": " и ещё {count}",
            "reports_read_failed": "Не удалось прочитать сохранённые отчёты.",
            "reports_empty": "Сохранённых отчётов пока нет.",
            "report": "Отчёт",
            "date": "Дата",
            "format": "Формат",
            "size": "Размер",
            "chat_no_cli": "Активный CLI не выбран.",
            "chat_no_token": "Resume token активного CLI недоступен.",
            "chat_no_workdir": "Рабочая папка сессии не задана.",
            "chat_missing": "Transcript нативного CLI недоступен.",
            "chat_no_replies": "В журнале CLI пока нет ответов агента.",
            "chat_note": "Последние ответы агента: {shown} из {count}. Источник: {source}. Вызовы инструментов не показаны.",
            "tool_tail_title": "Инструменты после последнего ответа",
            "tool_tail_note": "Шаги после последнего ответа: {shown} из {count}. Вывод обрезан.",
            "tool_call": "Вызов: {name}",
            "tool_output": "Вывод инструмента",
            "status_missing": "статус не указан",
            "message": "Сообщение",
        },
        statuses={
            "completed": "завершён",
            "done": "завершён",
            "failed": "завершился с ошибкой",
            "error": "завершился с ошибкой",
            "cancelled": "остановлен",
            "canceled": "остановлен",
            "running": "в работе",
            "in_progress": "в работе",
            "active": "в работе",
        },
        roles={
            "user": "Пользователь",
            "assistant": "Ассистент",
            "system": "Система",
            "tool": "Инструмент",
        },
    ),
}


_REPORT_COPIES["de"] = _ReportCopy(
    lang="de",
    text={
        **_REPORT_COPIES["en"].text,
        "title": "Sitzungsbericht: {session_uid}",
        "lede": (
            "Lesbare Kurzfassung: welche Sitzung das ist, woran sie arbeitet "
            "und welche Artefakte und Nachrichten jetzt wichtig sind. "
            "Technische Details stehen unten zur Prüfung."
        ),
        "overview_title": "Kurzüberblick",
        "runs_title": "Artefakte und Fortschritt",
        "chat_title": "Letzte Antworten des Agenten",
        "reports_title": "Gespeicherte Berichte",
        "session_card_title": "Technische Sitzungskarte",
        "run_card_title": "Wichtig",
        "run_technical_title": "Technische Laufdaten",
        "field": "Feld",
        "value": "Wert",
        "session_uid": "Sitzungs-UID",
        "session_id": "Sitzungs-ID",
        "name": "Name",
        "workdir": "Arbeitsverzeichnis",
        "active_cli": "Aktive CLI",
        "active_mode": "Aktiver Modus",
        "status": "Status",
        "queue": "Warteschlange",
        "generated": "Erstellt",
        "direct_cli": "direkte CLI",
        "no_cli": "keine CLI ausgewählt",
        "busy_running": "die Sitzung führt gerade eine Aufgabe aus",
        "busy_idle": "die Sitzung ist frei",
        "queue_empty": "Warteschlange ist leer",
        "queue_one": "1 Aufgabe in der Warteschlange",
        "queue_count": "{n} Aufgaben in der Warteschlange",
        "overview_session": "Sitzung: {name}",
        "overview_now": "Jetzt: {busy}. {queue}.",
        "overview_context": "Kontext: {mode} über {cli}.",
        "overview_workdir": "Arbeitsverzeichnis: {workdir}.",
        "overview_footer": (
            "Unten stehen die neuesten Artefakte des aktiven Modus, gespeicherte "
            "Berichte und der verfügbare Chat-Auszug."
        ),
        "artifacts_service_missing": "Artefakt-Service ist nicht verfügbar.",
        "artifacts_disabled": "Run-Artefakte sind in der Runtime-Konfiguration deaktiviert.",
        "artifact_store_missing": "Artefakt-Speicher ist nicht verfügbar.",
        "artifacts_read_failed": "Run-Artefakte konnten nicht gelesen werden.",
        "no_artifacts": "Noch keine Run-Artefakte{scope}.",
        "active_mode_scope": " für den aktiven Modus {mode_id}",
        "run_mode": "Modus",
        "run_id": "Run ID",
        "phase": "Phase",
        "started": "Gestartet",
        "updated": "Aktualisiert",
        "finished": "Beendet",
        "checkpoints": "Checkpoints",
        "files": "Dateien",
        "run_summary": "Run {run_id} im Modus {mode_id}: {status}, Phase {phase}.",
        "plan_missing": "Der Run-Plan ist nicht gespeichert.",
        "plan_text": "Plan: {text}",
        "plan_steps": "Der Plan hat {count} Schritt(e): {names}{suffix}.",
        "plan_more": " und {count} weitere",
        "plan_family": "Aufgabentyp im Plan: {family}.",
        "plan_no_summary": "Der Plan ist gespeichert, hat aber keine Kurzbeschreibung.",
        "last_checkpoint": "Letzter Checkpoint",
        "last_event": "Letztes Ereignis",
        "latest_missing": "{label}: keine Daten.",
        "latest_value": "{label}: {text}.",
        "artifact_files_missing": "Keine Artefaktdateien gespeichert.",
        "artifact_files": "Artefaktdateien: {files}{suffix}.",
        "artifact_files_more": " und {count} weitere",
        "reports_read_failed": "Gespeicherte Berichte konnten nicht gelesen werden.",
        "reports_empty": "Noch keine gespeicherten Berichte.",
        "report": "Bericht",
        "date": "Datum",
        "format": "Format",
        "size": "Grösse",
        "chat_no_cli": "Aktive CLI ist nicht gesetzt.",
        "chat_no_token": "Resume-Token der aktiven CLI ist nicht verfügbar.",
        "chat_no_workdir": "Arbeitsverzeichnis der Sitzung ist nicht gesetzt.",
        "chat_missing": "Transcript der nativen CLI ist nicht verfügbar.",
        "chat_no_replies": "Im CLI-Protokoll gibt es noch keine Antworten des Agenten.",
        "chat_note": "Letzte Antworten des Agenten: {shown} von {count}. Quelle: {source}. Werkzeugaufrufe werden ausgelassen.",
        "tool_tail_title": "Werkzeuge nach der letzten Antwort",
        "tool_tail_note": "Schritte nach der letzten Antwort: {shown} von {count}. Ausgabe ist gekürzt.",
        "tool_call": "Aufruf: {name}",
        "tool_output": "Werkzeugausgabe",
        "status_missing": "Status ist nicht angegeben",
        "message": "Nachricht",
    },
    statuses={
        "completed": "abgeschlossen",
        "done": "abgeschlossen",
        "failed": "fehlgeschlagen",
        "error": "fehlgeschlagen",
        "cancelled": "abgebrochen",
        "canceled": "abgebrochen",
        "running": "läuft",
        "in_progress": "läuft",
        "active": "läuft",
    },
    roles={
        "user": "Benutzer",
        "assistant": "Assistent",
        "system": "System",
        "tool": "Werkzeug",
    },
)


_REPORT_COPIES["zh"] = _ReportCopy(
    lang="zh",
    text={
        **_REPORT_COPIES["en"].text,
        "title": "会话报告：{session_uid}",
        "lede": (
            "面向用户的简短摘要：这个会话是什么、正在做什么、当前哪些"
            "产物和消息重要。技术细节保留在下方供核查。"
        ),
        "overview_title": "概览",
        "runs_title": "产物和进度",
        "chat_title": "最近的助手回复",
        "reports_title": "已保存报告",
        "session_card_title": "会话技术卡片",
        "run_card_title": "重点",
        "run_technical_title": "运行技术数据",
        "field": "字段",
        "value": "值",
        "session_uid": "会话 UID",
        "session_id": "会话 ID",
        "name": "名称",
        "workdir": "工作目录",
        "active_cli": "当前 CLI",
        "active_mode": "当前模式",
        "status": "状态",
        "queue": "队列",
        "generated": "生成时间",
        "direct_cli": "直接 CLI",
        "no_cli": "未选择 CLI",
        "busy_running": "会话正在执行任务",
        "busy_idle": "会话空闲",
        "queue_empty": "队列为空",
        "queue_one": "队列中有 1 个任务",
        "queue_count": "队列中有 {n} 个任务",
        "overview_session": "会话：{name}",
        "overview_now": "当前：{busy}。{queue}。",
        "overview_context": "上下文：{mode}，通过 {cli}。",
        "overview_workdir": "工作目录：{workdir}。",
        "overview_footer": "下方汇总当前模式的最新产物、已保存报告和可用聊天摘录。",
        "artifacts_service_missing": "产物服务不可用。",
        "artifacts_disabled": "运行产物已在 runtime config 中关闭。",
        "artifact_store_missing": "产物存储不可用。",
        "artifacts_read_failed": "无法读取运行产物。",
        "no_artifacts": "尚无运行产物{scope}。",
        "active_mode_scope": "（当前模式 {mode_id}）",
        "run_mode": "模式",
        "run_id": "Run ID",
        "phase": "阶段",
        "started": "开始时间",
        "updated": "更新时间",
        "finished": "完成时间",
        "checkpoints": "检查点",
        "files": "文件",
        "run_summary": "运行 {run_id}，模式 {mode_id}：{status}，阶段 {phase}。",
        "plan_missing": "未保存运行计划。",
        "plan_text": "计划：{text}",
        "plan_steps": "计划包含 {count} 个步骤：{names}{suffix}。",
        "plan_more": "，另有 {count} 个",
        "plan_family": "计划任务类型：{family}。",
        "plan_no_summary": "计划已保存，但没有简短说明。",
        "last_checkpoint": "最后检查点",
        "last_event": "最后事件",
        "latest_missing": "{label}：无数据。",
        "latest_value": "{label}：{text}。",
        "artifact_files_missing": "未保存产物文件。",
        "artifact_files": "产物文件：{files}{suffix}。",
        "artifact_files_more": "，另有 {count} 个",
        "reports_read_failed": "无法读取已保存报告。",
        "reports_empty": "尚无已保存报告。",
        "report": "报告",
        "date": "日期",
        "format": "格式",
        "size": "大小",
        "chat_no_cli": "未设置当前 CLI。",
        "chat_no_token": "当前 CLI 的 resume token 不可用。",
        "chat_no_workdir": "未设置会话工作目录。",
        "chat_missing": "原生 CLI transcript 不可用。",
        "chat_no_replies": "CLI transcript 中还没有助手回复。",
        "chat_note": "最近的助手回复：{shown}/{count}。来源：{source}。已省略工具调用。",
        "tool_tail_title": "最后一条回复之后的工具",
        "tool_tail_note": "最后一条回复之后的步骤：{shown}/{count}。输出已截断。",
        "tool_call": "调用：{name}",
        "tool_output": "工具输出",
        "status_missing": "未指定状态",
        "message": "消息",
    },
    statuses={
        "completed": "已完成",
        "done": "已完成",
        "failed": "失败",
        "error": "失败",
        "cancelled": "已取消",
        "canceled": "已取消",
        "running": "运行中",
        "in_progress": "运行中",
        "active": "运行中",
    },
    roles={
        "user": "用户",
        "assistant": "助手",
        "system": "系统",
        "tool": "工具",
    },
)


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

    def save_html_report(self, session: Any, *, now: Optional[float] = None, lang: str = "en") -> ReportSummary:
        snapshot = self.build_html_report(session, now=now, lang=lang)
        return self.report_history_service.save_html_report(
            session,
            snapshot.html,
            prefix="session_snapshot",
            title=snapshot.title,
            now=now,
        )

    def build_html_report(
        self,
        session: Any,
        *,
        now: Optional[float] = None,
        lang: str = "en",
    ) -> SessionSnapshotReport:
        copy = _copy_for(lang)
        generated_at = float(now if now is not None else self._now())
        session_uid = session_runtime_uid(session) or str(getattr(session, "id", "") or "session")
        active_mode = str(get_active_mode(session, "") or "").strip()
        active_cli = session_active_cli_name(session)
        title = copy.t("title", session_uid=session_uid)
        summary_rows = [
            (copy.t("session_uid"), session_uid),
            (copy.t("session_id"), str(getattr(session, "id", "") or "-")),
            (copy.t("name"), str(getattr(session, "name", "") or "-")),
            (copy.t("workdir"), str(getattr(session, "workdir", "") or "-")),
            (copy.t("active_cli"), active_cli or "-"),
            (copy.t("active_mode"), active_mode or copy.t("direct_cli")),
            (copy.t("status"), _busy_text(session, copy)),
            (copy.t("queue"), _queue_text(session, copy)),
            (copy.t("generated"), _format_ts(generated_at)),
        ]
        body = [
            "<!doctype html>",
            f'<html lang="{_e(copy.lang)}">',
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
            f'<p class="lede">{_e(copy.t("lede"))}</p>',
            "</section>",
            _section(
                copy.t("overview_title"),
                self._render_overview(session, active_mode=active_mode, active_cli=active_cli, copy=copy),
            ),
            _section(copy.t("runs_title"), self._render_runs(session, mode_id=active_mode, copy=copy)),
            _section(copy.t("chat_title"), self._render_chat_excerpt(session, active_cli=active_cli, copy=copy)),
            _section(copy.t("reports_title"), self._render_report_history(session, copy=copy)),
            _details(
                copy.t("session_card_title"),
                _table(summary_rows, headers=(copy.t("field"), copy.t("value"))),
                open_=False,
            ),
            "</main>",
            "</body>",
            "</html>",
        ]
        return SessionSnapshotReport(title=title, html="\n".join(body))

    def _render_overview(
        self,
        session: Any,
        *,
        active_mode: str,
        active_cli: str,
        copy: _ReportCopy,
    ) -> str:
        name = str(getattr(session, "name", "") or "").strip()
        workdir = str(getattr(session, "workdir", "") or "").strip()
        bullets = [
            copy.t("overview_session", name=name or session_runtime_uid(session) or getattr(session, "id", "-")),
            copy.t("overview_now", busy=_busy_text(session, copy), queue=_queue_text(session, copy)),
            copy.t("overview_context", mode=active_mode or copy.t("direct_cli"), cli=active_cli or copy.t("no_cli")),
        ]
        if workdir:
            bullets.append(copy.t("overview_workdir", workdir=workdir))
        bullets.append(copy.t("overview_footer"))
        return _bullet_list(bullets, class_name="human-list")

    def _render_runs(self, session: Any, *, mode_id: str, copy: _ReportCopy) -> str:
        service = self.run_artifacts_service
        if service is None:
            return _empty(copy.t("artifacts_service_missing"))
        try:
            if hasattr(service, "is_enabled") and not bool(service.is_enabled()):
                return _empty(copy.t("artifacts_disabled"))
            store = getattr(service, "artifact_store", service)
            list_runs = getattr(store, "list_runs", None)
            if not callable(list_runs):
                return _empty(copy.t("artifact_store_missing"))
            runs = list_runs(session=session, mode_id=(mode_id or None), limit=5)
        except Exception:
            logger.exception("session snapshot failed to list run artifacts")
            return _empty(copy.t("artifacts_read_failed"))
        if not runs:
            scope = copy.t("active_mode_scope", mode_id=mode_id) if mode_id else ""
            return _empty(copy.t("no_artifacts", scope=scope))

        chunks: list[str] = []
        for run in runs:
            chunks.append(self._render_run(store, run, copy=copy))
        return "\n".join(chunks)

    def _render_run(self, store: Any, run: Any, *, copy: _ReportCopy) -> str:
        state = _safe_call_dict(store, "load_state", run)
        plan = _safe_call_dict(store, "load_plan", run)
        checkpoints = _safe_call_dict(store, "load_checkpoints", run)
        metrics = _safe_call_dict(store, "load_metrics", run)
        recovery = _safe_call_dict(store, "load_recovery", run)
        events = _safe_call_list(store, "load_events_tail", run, limit=8)
        checkpoint_items = list(checkpoints.get("items") or []) if isinstance(checkpoints, dict) else []
        artifact_files = _list_files(getattr(run, "artifacts_dir", ""), limit=20)
        rows = [
            (copy.t("run_mode"), getattr(run, "mode_id", "") or state.get("mode_id") or "-"),
            (copy.t("run_id"), getattr(run, "run_id", "") or state.get("run_id") or "-"),
            (copy.t("status"), state.get("status") or "-"),
            (copy.t("phase"), state.get("phase") or "-"),
            (copy.t("started"), _format_ts(state.get("started_at"))),
            (copy.t("updated"), _format_ts(state.get("updated_at"))),
            (copy.t("finished"), _format_ts(state.get("finished_at"))),
            (copy.t("checkpoints"), str(len(checkpoint_items))),
            (copy.t("files"), str(len(artifact_files))),
        ]
        technical = [
            _table(rows, headers=(copy.t("field"), copy.t("value"))),
            "<h3>PLAN</h3>",
            _json_block(_short_json(plan)),
            "<h3>METRICS</h3>",
            _json_block(_short_json(metrics)),
            "<h3>RECOVERY</h3>",
            _json_block(_short_json(recovery)),
            f"<h3>{_e(copy.t('last_checkpoint'))}</h3>",
            _json_block(_short_json(checkpoint_items[-5:])),
            f"<h3>{_e(copy.t('last_event'))}</h3>",
            _json_block(_short_json(events)),
            f"<h3>{_e(copy.t('files'))}</h3>",
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
                copy=copy,
            ),
            _details(copy.t("run_technical_title"), "\n".join(technical), open_=False),
        ]
        title = f"{getattr(run, 'mode_id', '-')}/{getattr(run, 'run_id', '-')}"
        return _details(str(title), "\n".join(content), open_=False)

    def _render_report_history(self, session: Any, *, copy: _ReportCopy) -> str:
        try:
            reports = self.report_history_service.list_reports(session, limit=10)
        except Exception:
            logger.exception("session snapshot failed to list saved reports")
            return _empty(copy.t("reports_read_failed"))
        if not reports:
            return _empty(copy.t("reports_empty"))
        rows = [
            (
                item.report_id,
                item.date,
                item.fmt,
                _format_bytes(item.size),
            )
            for item in reports
        ]
        return _table(rows, headers=(copy.t("report"), copy.t("date"), copy.t("format"), copy.t("size")))

    def _render_chat_excerpt(self, session: Any, *, active_cli: str, copy: _ReportCopy) -> str:
        resume_token = _resume_token(session, active_cli)
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not active_cli:
            return _empty(copy.t("chat_no_cli"))
        if not resume_token:
            return _empty(copy.t("chat_no_token"))
        if not workdir:
            return _empty(copy.t("chat_no_workdir"))
        try:
            canonical = self._extract_session(active_cli, resume_token, workdir)
        except Exception:
            logger.exception("session snapshot failed to extract CLI transcript")
            canonical = None
        messages = list(getattr(canonical, "messages", []) or []) if canonical is not None else []
        if not messages:
            return _empty(copy.t("chat_missing"))
        replies: list[tuple[int, Any, str]] = []
        for position, msg in enumerate(messages):
            if str(getattr(msg, "role", "") or "").strip().lower() != "assistant":
                continue
            text = _assistant_reply_text(msg)
            if text:
                replies.append((position, msg, text))
        if not replies:
            return _empty(copy.t("chat_no_replies"))
        shown = replies[-_CHAT_REPLIES_LIMIT:]
        note = copy.t(
            "chat_note",
            shown=len(shown),
            count=len(replies),
            source=getattr(canonical, "source_cli", "") or active_cli,
        )
        chunks = [f'<p class="note">{_e(note)}</p>']
        for idx, (_position, msg, text) in enumerate(shown, start=len(replies) - len(shown) + 1):
            ts = _format_ts(getattr(msg, "timestamp", None))
            chunks.append(
                '<article class="message role-assistant">'
                f"<h3>{idx}. {_e(_role_label('assistant', copy))} <span>{_e(ts)}</span></h3>"
                f"<p>{_e(_truncate(text, 2500))}</p></article>"
            )
        chunks.extend(_render_tool_tail(messages[replies[-1][0] + 1:], copy=copy))
        return "\n".join(chunks)


def _copy_for(lang: str) -> _ReportCopy:
    code = str(lang or "").strip().lower().split("-", 1)[0]
    return _REPORT_COPIES.get(code, _REPORT_COPIES["en"])


def _busy_text(session: Any, copy: _ReportCopy) -> str:
    return copy.t("busy_running") if bool(getattr(session, "busy", False)) else copy.t("busy_idle")


def _queue_text(session: Any, copy: _ReportCopy) -> str:
    size = len(list(getattr(session, "queue", []) or []))
    if size == 0:
        return copy.t("queue_empty")
    if size == 1:
        return copy.t("queue_one")
    if copy.lang == "ru" and 2 <= size <= 4:
        return copy.t("queue_few", n=size)
    return copy.t("queue_count", n=size)


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
    copy: _ReportCopy,
) -> str:
    mode_id = str(getattr(run, "mode_id", "") or state.get("mode_id") or "-")
    run_id = str(getattr(run, "run_id", "") or state.get("run_id") or "-")
    status = str(state.get("status") or "-")
    phase = str(state.get("phase") or "-")
    bullets = [
        copy.t("run_summary", run_id=run_id, mode_id=mode_id, status=_status_text(status, copy), phase=phase),
        _describe_plan(plan, copy),
        _describe_latest(copy.t("last_checkpoint"), checkpoints, copy),
        _describe_latest(copy.t("last_event"), events, copy),
        _describe_artifact_files(artifact_files, copy),
    ]
    return '<div class="run-card"><h3>%s</h3>%s</div>' % (
        _e(copy.t("run_card_title")),
        _bullet_list(bullets, class_name="human-list"),
    )


def _status_text(status: str, copy: _ReportCopy) -> str:
    normalized = str(status or "").strip().lower()
    return copy.statuses.get(normalized, status or copy.t("status_missing"))


def _describe_plan(plan: dict[str, Any], copy: _ReportCopy) -> str:
    if not plan:
        return copy.t("plan_missing")
    for key in ("goal", "summary", "task", "title"):
        value = str(plan.get(key) or "").strip()
        if value:
            return copy.t("plan_text", text=_truncate(value, 220))
    units = plan.get("units") or plan.get("tasks") or []
    if isinstance(units, list) and units:
        names = []
        for item in units[:3]:
            if isinstance(item, dict):
                names.append(str(item.get("title") or item.get("name") or item.get("id") or "шаг").strip())
            else:
                names.append(str(item).strip())
        suffix = copy.t("plan_more", count=len(units) - 3) if len(units) > 3 else ""
        return copy.t("plan_steps", count=len(units), names=", ".join(names), suffix=suffix)
    family = str(plan.get("task_family") or "").strip()
    if family:
        return copy.t("plan_family", family=family)
    return copy.t("plan_no_summary")


def _describe_latest(label: str, values: list[Any], copy: _ReportCopy) -> str:
    if not values:
        return copy.t("latest_missing", label=label)
    value = values[-1]
    if isinstance(value, dict):
        for key in ("message", "summary", "title", "status", "phase", "event_type", "current_step_id"):
            text = str(value.get(key) or "").strip()
            if text:
                return copy.t("latest_value", label=label, text=_truncate(text, 220))
    return copy.t("latest_value", label=label, text=_truncate(str(value), 220))


def _describe_artifact_files(files: list[str], copy: _ReportCopy) -> str:
    if not files:
        return copy.t("artifact_files_missing")
    visible = files[:3]
    suffix = copy.t("artifact_files_more", count=len(files) - 3) if len(files) > 3 else ""
    return copy.t("artifact_files", files=", ".join(visible), suffix=suffix)


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


def _role_label(role: str, copy: _ReportCopy) -> str:
    return copy.roles.get(str(role or "").strip().lower(), role or copy.t("message"))


def _assistant_reply_text(message: Any) -> str:
    """Ответ агента без строк о вызовах инструментов — как в превью бота."""
    return strip_tool_calls(str(getattr(message, "content", "") or ""))


def _render_tool_tail(messages: list[Any], *, copy: _ReportCopy) -> list[str]:
    """Инструменты, которые агент вызвал уже после последнего своего ответа."""
    steps: list[tuple[str, str, Any]] = []
    for msg in messages:
        role = str(getattr(msg, "role", "") or "").strip().lower()
        content = str(getattr(msg, "content", "") or "")
        timestamp = getattr(msg, "timestamp", None)
        if role == "assistant":
            for name in TOOL_CALL_MARKER_RE.findall(content):
                steps.append((copy.t("tool_call", name=name.strip() or "-"), "", timestamp))
        elif role == "tool":
            steps.append((copy.t("tool_output"), content.strip(), timestamp))
    if not steps:
        return []
    visible = steps[-_TOOL_TAIL_LIMIT:]
    note = copy.t("tool_tail_note", shown=len(visible), count=len(steps))
    blocks = [
        f"<h3>{_e(copy.t('tool_tail_title'))}</h3>",
        f'<p class="note">{_e(note)}</p>',
    ]
    for label, output, timestamp in visible:
        body = f"<p>{_e(_truncate(output, _TOOL_OUTPUT_MAX_CHARS))}</p>" if output else ""
        blocks.append(
            '<article class="message role-tool">'
            f"<h3>{_e(label)} <span>{_e(_format_ts(timestamp))}</span></h3>{body}</article>"
        )
    return blocks


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
