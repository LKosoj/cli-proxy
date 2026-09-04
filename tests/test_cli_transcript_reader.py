import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from app.services.cli_backends.transcript_reader import CliTranscriptReader, TranscriptLocator
from app.services.session_transfer.reader_kimi import _workspace_key as _kimi_workspace_key


def _iso_utc(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _append_jsonl(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_claude_transcript_reader_streams_clean_text_and_completion(tmp_path):
    started_at = time.time() - 5
    session_id = "11111111-1111-4111-8111-111111111111"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at - 60),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ответ прошлого хода"}]},
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 1),
            "message": {"role": "user", "content": "do work"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 2),
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private thought"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
                    {"type": "text", "text": "Промежуточный статус"},
                ],
            },
        },
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    assert first.assistant_text == "Промежуточный статус"
    assert first.progress_text == "Bash"
    assert first.complete is False
    assert "private thought" not in first.assistant_text

    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 3),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Финальный ответ"}],
            },
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "pendingBackgroundAgentCount": 0,
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 4),
        },
    )

    final = reader.poll()

    assert final.assistant_text == "Финальный ответ"
    assert final.complete is True
    assert final.locator is not None
    assert final.locator.path == str(path.resolve())


def test_claude_transcript_reader_completes_with_background_agents(tmp_path):
    """turn_duration закрывает ход, даже если фоновые агенты ещё работают."""
    started_at = time.time() - 5
    session_id = "66666666-6666-4666-8666-666666666666"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 1),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Запустил агентов"}]},
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "pendingBackgroundAgentCount": 10,
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 2),
        },
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.assistant_text == "Запустил агентов"
    assert result.complete is True


def test_claude_transcript_reader_skips_records_of_previous_turn(tmp_path):
    """Журнал общий для всего треда: ход начинается с первой записи после старта."""
    started_at = time.time() - 5
    session_id = "77777777-7777-4777-8777-777777777777"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at - 3600),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ответ прошлого хода"}]},
        },
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    # Пока CLI не написал ни одной свежей записи, цепляться не к чему.
    assert first.available is False
    assert first.assistant_text == ""

    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 1),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "свежий ответ"}]},
        },
    )

    second = reader.poll()

    assert second.available is True
    assert second.assistant_text == "свежий ответ"


def test_codex_transcript_reader_uses_task_complete(tmp_path):
    """codex в tmux пишет тот же rollout, что и headless: текст в журнале чистый,
    без элементов TUI, а конец хода отмечен событием task_complete."""
    started_at = time.time() - 5
    session_id = "22222222-2222-4222-8222-222222222222"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "10"
        / f"rollout-2026-07-10T09-00-00-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": _iso_utc(started_at - 60),
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/srv/project", "source": "cli"},
        },
        {
            "timestamp": _iso_utc(started_at + 1),
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "do work"},
        },
        {
            "timestamp": _iso_utc(started_at + 2),
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Проверяю тесты", "phase": "commentary"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        workdir="/srv/project",
        started_at=started_at,
        home_dir=tmp_path,
    )

    intermediate = reader.poll()

    assert intermediate.available is True
    assert intermediate.recognized is True
    assert intermediate.assistant_text == "Проверяю тесты"
    assert intermediate.complete is False
    assert intermediate.session_id == session_id

    _append_jsonl(
        path,
        {
            "timestamp": _iso_utc(started_at + 3),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "Codex готов"},
        },
    )

    result = reader.poll()

    assert result.assistant_text == "Codex готов"
    assert result.complete is True


def test_codex_transcript_reader_uses_assistant_response_item_for_preview(tmp_path):
    started_at = time.time() - 5
    session_id = "22222222-2222-4222-8222-222222222222"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "09"
        / "04"
        / f"rollout-2026-09-04T09-00-00-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": _iso_utc(started_at - 60),
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/srv/project", "source": "cli"},
        },
        {
            "timestamp": _iso_utc(started_at + 1),
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "output": "секретный вывод тула"},
        },
        {
            "timestamp": _iso_utc(started_at + 2),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Проверяю live-сессию"}],
                "phase": "commentary",
            },
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        workdir="/srv/project",
        started_at=started_at,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.available is True
    assert result.recognized is True
    assert result.assistant_text == "Проверяю live-сессию"
    assert "секретный вывод тула" not in result.assistant_text
    assert result.complete is False


def test_codex_transcript_reader_ignores_rollout_of_other_workdir(tmp_path):
    """Путь rollout рабочую директорию не кодирует, поэтому принадлежность
    проверяется по полю cwd внутри журнала."""
    started_at = time.time() - 5
    session_id = "99999999-9999-4999-8999-999999999999"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "10"
        / f"rollout-2026-07-10T09-00-00-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": _iso_utc(started_at + 1),
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/srv/other"},
        },
        {
            "timestamp": _iso_utc(started_at + 2),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "чужой ответ"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        workdir="/srv/project",
        started_at=started_at,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.available is False
    assert result.assistant_text == ""


def test_codex_resume_keeps_appending_to_same_rollout(tmp_path):
    """codex resume дописывает тот же файл, поэтому поиск по session_id
    остаётся валидным между ходами."""
    started_at = time.time() - 5
    session_id = "019fb756-d431-76d2-a4a6-0898b70a2882"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "31"
        / f"rollout-2026-07-31T11-42-28-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": _iso_utc(started_at - 3600),
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/tmp/tmuxdbg"},
        },
        {
            "timestamp": _iso_utc(started_at + 1),
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "ответь ok"},
        },
        {
            "timestamp": _iso_utc(started_at + 2),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "ok"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        workdir="/tmp/tmuxdbg",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.complete is True
    assert result.assistant_text == "ok"
    assert result.locator is not None and result.locator.path.endswith(f"{session_id}.jsonl")


def test_codex_transcript_reader_attaches_to_known_thread(tmp_path):
    """Тред известен по session_id: читатель цепляется к его текущему концу и
    берёт только то, что появится после старта запроса."""
    started_at = time.time() - 5
    session_id = "019fade8-142e-7c91-ae5b-3dc3c03a2448"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "29"
        / f"rollout-2026-07-29T15-44-55-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": _iso_utc(started_at - 3600),
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/srv/project"},
        },
        {
            "timestamp": _iso_utc(started_at - 3500),
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "ответ прошлого хода"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    assert reader.poll().assistant_text == ""

    _append_jsonl(
        path,
        {
            "timestamp": _iso_utc(started_at + 1),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "свежий ответ"},
        },
    )
    second = reader.poll()

    # Всё, что записано до старта запроса, уже отдано пользователю и не повторяется.
    assert second.assistant_text == "свежий ответ"
    assert second.recognized is True
    assert second.session_id == session_id


def test_codex_transcript_reader_follows_thread_into_new_rollout(tmp_path):
    """Если CLI продолжил тред в новом файле, старый журнал свежих записей не
    получит — читатель обязан найти настоящий журнал хода."""
    started_at = time.time() - 5
    session_id = "019fade8-142e-7c91-ae5b-3dc3c03a2448"
    base = tmp_path / ".codex" / "sessions" / "2026" / "07" / "29"
    _append_jsonl(
        base / f"rollout-2026-07-29T15-44-55-{session_id}.jsonl",
        {
            "timestamp": _iso_utc(started_at - 3600),
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/srv/project"},
        },
    )
    fresh_id = "01a00a64-0fe1-7fa3-a3d0-23124f88d3a4"
    _append_jsonl(
        base / f"rollout-2026-07-29T16-00-00-{fresh_id}.jsonl",
        {
            "timestamp": _iso_utc(started_at + 1),
            "type": "session_meta",
            "payload": {"id": fresh_id, "cwd": "/srv/project"},
        },
        {
            "timestamp": _iso_utc(started_at + 2),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "ответ из нового файла"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.assistant_text == "ответ из нового файла"
    assert result.locator is not None and result.locator.path.endswith(f"{fresh_id}.jsonl")
    # Идентификатор треда переезжает вслед за журналом.
    assert result.session_id == fresh_id


def test_grok_transcript_reader_uses_online_updates(tmp_path):
    started_at = time.time() - 5
    session_id = "33333333-3333-4333-8333-333333333333"
    workspace_key = urllib.parse.quote("/srv/project", safe="")
    path = tmp_path / ".grok" / "sessions" / workspace_key / session_id / "updates.jsonl"
    _append_jsonl(
        path,
        {
            "timestamp": started_at + 1,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "do work"},
                },
            },
        },
        {
            "timestamp": started_at + 2,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "tool_call", "title": "Run tests"},
            },
        },
        {
            "timestamp": started_at + 3,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Grok "},
                    "_meta": {"streamStartMs": 100},
                },
            },
        },
        {
            "timestamp": started_at + 3.5,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "готов"},
                    "_meta": {"streamStartMs": 100},
                },
            },
        },
    )
    reader = CliTranscriptReader(
        cli_name="grok",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    intermediate = reader.poll()

    assert intermediate.available is True
    assert intermediate.progress_text == "Run tests"
    # Чанки склеиваются встык, поэтому пробел на границе обязан уцелеть.
    assert intermediate.assistant_text == "Grok готов"
    assert intermediate.complete is False

    _append_jsonl(
        path,
        {
            "timestamp": started_at + 4,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "turn_completed", "stop_reason": "end_turn"},
            },
        },
    )

    result = reader.poll()

    # Конец хода grok отмечает отдельным событием turn_completed.
    assert result.assistant_text == "Grok готов"
    assert result.complete is True


def _kimi_wire_path(home: Path, session_id: str) -> Path:
    return (
        home
        / ".kimi-code"
        / "sessions"
        / _kimi_workspace_key("/srv/project")
        / session_id
        / "agents"
        / "main"
        / "wire.jsonl"
    )


def test_kimi_transcript_reader_merges_step_parts(tmp_path):
    started_at = time.time() - 5
    session_id = "session_55555555-5555-4555-8555-555555555555"
    path = _kimi_wire_path(tmp_path, session_id)

    def stamp(offset: float) -> int:
        return int((started_at + offset) * 1000)

    _append_jsonl(
        path,
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "собери проект"}],
            "origin": {"kind": "user"},
            "time": stamp(1),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "turnId": "0", "step": 1},
            "time": stamp(2),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "tool.call", "name": "Bash", "toolCallId": "call-1"},
            "time": stamp(3),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Собираю."}},
            "time": stamp(4),
        },
    )
    reader = CliTranscriptReader(
        cli_name="kimi",
        workdir="/srv/project",
        started_at=started_at,
        session_id="",
        home_dir=tmp_path,
    )

    intermediate = reader.poll()

    assert intermediate.available is True
    assert intermediate.recognized is True
    assert intermediate.session_id == session_id
    assert intermediate.progress_text == "Bash"
    assert intermediate.assistant_text == "Собираю."
    assert intermediate.complete is False
    # Kimi stamps epoch milliseconds; the backend compares activity in seconds.
    assert intermediate.activity_at == stamp(4) / 1000.0

    _append_jsonl(
        path,
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Готово."}},
            "time": stamp(5),
        },
        {"type": "turn.ended", "turnId": 0, "reason": "completed", "time": stamp(6)},
    )

    result = reader.poll()

    assert result.assistant_text == "Собираю.\nГотово."
    assert result.complete is True


def test_kimi_transcript_reader_restarts_text_on_next_step(tmp_path):
    started_at = time.time() - 5
    session_id = "session_66666666-6666-4666-8666-666666666666"
    path = _kimi_wire_path(tmp_path, session_id)

    def stamp(offset: float) -> int:
        return int((started_at + offset) * 1000)

    _append_jsonl(
        path,
        {
            "type": "context.append_message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "проверь тесты"}],
                "origin": {"kind": "user"},
            },
            "time": stamp(1),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "turnId": "0", "step": 1},
            "time": stamp(2),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Запускаю тесты."}},
            "time": stamp(3),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.end", "turnId": "0", "step": 1, "finishReason": "tool_use"},
            "time": stamp(4),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "turnId": "0", "step": 2},
            "time": stamp(5),
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Тесты зелёные."}},
            "time": stamp(6),
        },
    )
    reader = CliTranscriptReader(
        cli_name="kimi",
        workdir="/srv/project",
        started_at=started_at,
        session_id="",
        home_dir=tmp_path,
    )

    result = reader.poll()

    # Only the last step's text is the answer; the previous step is progress noise.
    assert result.assistant_text == "Тесты зелёные."
    assert result.complete is False


def test_transcript_reader_recovers_from_persisted_locator(tmp_path):
    started_at = time.time() - 5
    session_id = "44444444-4444-4444-8444-444444444444"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 1),
            "message": {"role": "user", "content": "do work"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 2),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Восстановленный ответ"}],
            },
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 3),
        },
    )
    locator = TranscriptLocator(
        provider="claude",
        path=str(path.resolve()),
        start_offset=0,
        session_id=session_id,
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
        locator=locator,
    )

    result = reader.poll()

    assert result.complete is True
    assert result.assistant_text == "Восстановленный ответ"


def test_transcript_reader_rejects_locator_outside_provider_root(tmp_path):
    path = tmp_path / "outside.jsonl"
    _append_jsonl(
        path,
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Чужой ответ"}]},
        },
        {"type": "system", "subtype": "turn_duration"},
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        workdir="/srv/project",
        started_at=time.time() - 5,
        home_dir=tmp_path,
        locator=TranscriptLocator(
            provider="claude",
            path=str(path),
            start_offset=0,
        ),
    )

    result = reader.poll()

    assert result.available is False
    assert result.complete is False
    assert result.assistant_text == ""
    assert result.session_id == ""


def test_transcript_reader_does_not_consume_partial_jsonl_record(tmp_path):
    started_at = time.time() - 5
    session_id = "55555555-5555-4555-8555-555555555555"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 1),
            "message": {"role": "user", "content": "do work"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )
    assert reader.poll().assistant_text == ""

    record = json.dumps(
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 2),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "После дописи"}]},
        },
        ensure_ascii=False,
    )
    split_at = len(record) // 2
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record[:split_at])
    assert reader.poll().assistant_text == ""

    with path.open("a", encoding="utf-8") as handle:
        handle.write(record[split_at:] + "\n")

    assert reader.poll().assistant_text == "После дописи"


def _qwen_path(home: Path, project_key: str, session_id: str) -> Path:
    return home / ".qwen" / "projects" / project_key / "chats" / f"{session_id}.jsonl"


def test_qwen_transcript_reader_reads_parts_and_skips_thoughts(tmp_path):
    started_at = time.time() - 5
    session_id = "22222222-2222-4222-8222-222222222222"
    path = _qwen_path(tmp_path, "-srv-project", session_id)
    _append_jsonl(
        path,
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 1),
            "message": {"role": "user", "parts": [{"text": "сделай"}]},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 2),
            "message": {
                "role": "model",
                "parts": [
                    {"text": "надо сначала поискать", "thought": True},
                    {"functionCall": {"id": "call_1", "name": "glob", "args": {"pattern": "*.py"}}},
                ],
            },
        },
    )
    reader = CliTranscriptReader(
        cli_name="qwen",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    assert first.recognized is True
    # Размышления модели в ответ не попадают, имя инструмента идёт в прогресс.
    assert first.assistant_text == ""
    assert first.progress_text == "glob"
    assert first.complete is False

    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": _iso_utc(started_at + 3),
            "message": {
                "role": "model",
                "parts": [
                    {"text": "осталось оформить", "thought": True},
                    {"text": "Итог работы"},
                ],
            },
        },
    )
    second = reader.poll()

    # Отдельного события о конце хода qwen не пишет: ход закончен, когда модель
    # ответила текстом и не позвала инструмент.
    assert second.complete is True
    assert second.assistant_text == "Итог работы"
    assert second.session_id == session_id


def test_qwen_transcript_reader_ignores_other_workspace(tmp_path):
    """Журнал ищется только в каталоге рабочей директории: чужой проект,
    который пишет в тот же момент, подхватываться не должен."""
    started_at = time.time() - 5
    path = _qwen_path(tmp_path, "-srv-other", "33333333-3333-4333-8333-333333333333")
    _append_jsonl(
        path,
        {
            "type": "assistant",
            "timestamp": _iso_utc(started_at + 1),
            "message": {"role": "model", "parts": [{"text": "чужой ответ"}]},
        },
    )
    reader = CliTranscriptReader(
        cli_name="qwen",
        workdir="/srv/project",
        started_at=started_at,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.available is False
    assert result.assistant_text == ""


def _write_gemini_session(home: Path, session_id: str, messages: list, *, cwd: str = "/srv/project") -> Path:
    path = home / ".gemini" / "tmp" / "project-hash" / "chats" / f"session-{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sessionId": session_id,
        "projectHash": "project-hash",
        "cwd": cwd,
        "messages": messages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_gemini_transcript_reader_survives_file_rewrite(tmp_path):
    """gemini переписывает файл целиком, а не дописывает: читатель обязан
    переоткрывать его, иначе после перезаписи ответ будет потерян."""
    started_at = time.time() - 5
    session_id = "44444444-4444-4444-8444-444444444444"
    previous = [
        {"id": "m1", "type": "user", "content": "прошлый вопрос", "timestamp": _iso_utc(started_at - 3600)},
        {"id": "m2", "type": "gemini", "content": "прошлый ответ", "timestamp": _iso_utc(started_at - 3599)},
    ]
    question = {"id": "m3", "type": "user", "content": "посчитай", "timestamp": _iso_utc(started_at + 1)}
    _write_gemini_session(tmp_path, session_id, [*previous, question])
    reader = CliTranscriptReader(
        cli_name="gemini",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    # Ответ прошлого хода лежит до старта запроса и не должен попасть в результат.
    assert first.assistant_text == ""
    assert first.complete is False

    intermediate = {"id": "m4", "type": "gemini", "content": "промежуточно", "timestamp": _iso_utc(started_at + 2)}
    _write_gemini_session(tmp_path, session_id, [*previous, question, intermediate])
    second = reader.poll()

    assert second.assistant_text == "промежуточно"
    assert second.complete is False

    # Старые сообщения из снимка вытеснены, а ответ дописан — файл читается заново.
    answer = {"id": "m5", "type": "gemini", "content": "Ответ: 4", "timestamp": _iso_utc(started_at + 4)}
    _write_gemini_session(tmp_path, session_id, [question, intermediate, answer])
    third = reader.poll()

    assert third.assistant_text == "Ответ: 4"
    assert third.complete is False

    fourth = reader.poll()

    # Записи о конце хода в снимке нет: ход закончен, когда последний ответ
    # модели не изменился между чтениями.
    assert fourth.complete is True
    assert fourth.assistant_text == "Ответ: 4"
    assert fourth.session_id == session_id


def test_gemini_transcript_reader_waits_while_tool_calls_are_attached(tmp_path):
    """Вызовы инструментов дописываются в то же сообщение следом за текстом,
    поэтому текст без toolCalls сам по себе концом хода не считается."""
    started_at = time.time() - 5
    session_id = "77777777-7777-4777-8777-777777777777"
    question = {"id": "m1", "type": "user", "content": "посчитай", "timestamp": _iso_utc(started_at + 1)}
    thinking = {"id": "m2", "type": "gemini", "content": "Запускаю поиск", "timestamp": _iso_utc(started_at + 2)}
    _write_gemini_session(tmp_path, session_id, [question, thinking])
    reader = CliTranscriptReader(
        cli_name="gemini",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    assert reader.poll().complete is False

    with_tool = {**thinking, "toolCalls": [{"name": "glob", "status": "success"}]}
    _write_gemini_session(tmp_path, session_id, [question, with_tool])

    assert reader.poll().complete is False
    assert reader.poll().complete is False

    answer = {"id": "m3", "type": "gemini", "content": "Готово", "timestamp": _iso_utc(started_at + 4)}
    _write_gemini_session(tmp_path, session_id, [question, with_tool, answer])

    assert reader.poll().complete is False
    final = reader.poll()

    assert final.complete is True
    assert final.assistant_text == "Готово"


def test_gemini_transcript_reader_tolerates_partially_written_file(tmp_path):
    """Файл переписывается целиком, поэтому опрос может попасть на момент записи."""
    started_at = time.time() - 5
    session_id = "55555555-5555-4555-8555-555555555555"
    path = _write_gemini_session(
        tmp_path,
        session_id,
        [
            {"id": "m1", "type": "user", "content": "посчитай", "timestamp": _iso_utc(started_at + 1)},
            {"id": "m2", "type": "gemini", "content": "первый ответ", "timestamp": _iso_utc(started_at + 2)},
        ],
    )
    reader = CliTranscriptReader(
        cli_name="gemini",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )
    assert reader.poll().assistant_text == "первый ответ"

    path.write_text('{"sessionId": "55555555", "messages": [{"id": "m1"', encoding="utf-8")

    result = reader.poll()

    # Обрывок не должен ни падать, ни стирать уже прочитанный ответ, ни закрывать ход.
    assert result.assistant_text == "первый ответ"
    assert result.complete is False


def test_gemini_transcript_reader_finds_session_by_workdir(tmp_path):
    """Каталог проекта gemini называет по-своему, поэтому журнал признаётся
    своим по полю cwd внутри снимка."""
    started_at = time.time() - 5
    session_id = "66666666-6666-4666-8666-666666666666"
    path = tmp_path / ".gemini" / "tmp" / "llmapigateway" / "chats" / f"session-{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "cwd": "/srv/git_projects/LLMApiGateway",
                "messages": [
                    {"id": "m1", "type": "user", "content": "посчитай", "timestamp": _iso_utc(started_at + 1)},
                    {"id": "m2", "type": "gemini", "content": "Готово", "timestamp": _iso_utc(started_at + 2)},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reader = CliTranscriptReader(
        cli_name="gemini",
        workdir="/srv/git_projects/LLMApiGateway",
        started_at=started_at,
        home_dir=tmp_path,
    )

    assert reader.poll().assistant_text == "Готово"
    result = reader.poll()

    assert result.complete is True
    assert result.session_id == session_id


def test_gemini_transcript_reader_ignores_session_of_other_workdir(tmp_path):
    started_at = time.time() - 5
    _write_gemini_session(
        tmp_path,
        "88888888-8888-4888-8888-888888888888",
        [{"id": "m1", "type": "gemini", "content": "чужой ответ", "timestamp": _iso_utc(started_at + 1)}],
        cwd="/srv/other",
    )
    reader = CliTranscriptReader(
        cli_name="gemini",
        workdir="/srv/project",
        started_at=started_at,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.available is False
    assert result.assistant_text == ""
