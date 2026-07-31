import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.services.cli_backends.codex_rollout_tail import CodexRolloutTail


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write_rollout(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _rollout_path(home: Path, session_id: str) -> Path:
    return home / ".codex" / "sessions" / "2026" / "07" / "31" / f"rollout-2026-07-31T12-00-00-{session_id}.jsonl"


def test_finds_rollout_by_session_id_and_reports_completion(tmp_path):
    session_id = "019fb756-d431-76d2-a4a6-0898b70a2882"
    started = time.time() - 60
    path = _rollout_path(tmp_path, session_id)
    _write_rollout(
        path,
        {"timestamp": _iso(started), "type": "session_meta", "payload": {"id": session_id, "cwd": "/srv/project"}},
        {"timestamp": _iso(started + 1), "type": "event_msg", "payload": {"type": "user_message", "message": "задача"}},
        {"timestamp": _iso(started + 2), "type": "event_msg", "payload": {"type": "agent_message", "message": "готово"}},
        {
            "timestamp": _iso(started + 3),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "готово"},
        },
    )

    tail = CodexRolloutTail(
        workdir="/srv/project",
        started_at=started,
        session_id=session_id,
        home_dir=tmp_path,
    )
    state = tail.poll()

    assert state is not None
    assert state.turn_complete is True
    assert state.assistant_text == "готово"
    assert state.path == str(path)


def test_previous_turn_is_not_picked_up_on_resume(tmp_path):
    """resume дописывает тот же файл, поэтому старый ответ должен отсекаться."""
    session_id = "019fb757-0000-4000-8000-000000000000"
    old = time.time() - 3600
    path = _rollout_path(tmp_path, session_id)
    _write_rollout(
        path,
        {"timestamp": _iso(old), "type": "session_meta", "payload": {"id": session_id, "cwd": "/srv/project"}},
        {"timestamp": _iso(old + 1), "type": "event_msg", "payload": {"type": "agent_message", "message": "прошлый"}},
        {
            "timestamp": _iso(old + 2),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "прошлый"},
        },
    )

    tail = CodexRolloutTail(
        workdir="/srv/project",
        started_at=time.time(),
        session_id=session_id,
        home_dir=tmp_path,
    )
    state = tail.poll()

    assert state is not None
    assert state.assistant_text == ""
    assert state.turn_complete is False


def test_running_turn_is_reported_without_completion(tmp_path):
    session_id = "019fb758-0000-4000-8000-000000000000"
    started = time.time() - 10
    path = _rollout_path(tmp_path, session_id)
    _write_rollout(
        path,
        {"timestamp": _iso(started), "type": "session_meta", "payload": {"id": session_id, "cwd": "/srv/project"}},
        {"timestamp": _iso(started + 1), "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": _iso(started + 2),
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "промежуточный комментарий"},
        },
    )

    state = CodexRolloutTail(
        workdir="/srv/project",
        started_at=started,
        session_id=session_id,
        home_dir=tmp_path,
    ).poll()

    assert state is not None
    assert state.assistant_text == "промежуточный комментарий"
    assert state.turn_complete is False


def test_discovery_by_workdir_when_session_id_is_unknown(tmp_path):
    started = time.time() - 30
    mine = _rollout_path(tmp_path, "019fb759-0000-4000-8000-000000000000")
    other = _rollout_path(tmp_path, "019fb75a-0000-4000-8000-000000000000")
    _write_rollout(
        other,
        {"timestamp": _iso(started), "type": "session_meta", "payload": {"cwd": "/srv/other"}},
        {
            "timestamp": _iso(started + 1),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "чужой ответ"},
        },
    )
    _write_rollout(
        mine,
        {"timestamp": _iso(started), "type": "session_meta", "payload": {"cwd": "/srv/project"}},
        {
            "timestamp": _iso(started + 1),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "мой ответ"},
        },
    )

    state = CodexRolloutTail(workdir="/srv/project", started_at=started, home_dir=tmp_path).poll()

    assert state is not None
    assert state.assistant_text == "мой ответ"


def test_missing_rollout_returns_none(tmp_path):
    state = CodexRolloutTail(workdir="/srv/project", started_at=time.time(), home_dir=tmp_path).poll()

    assert state is None


def test_session_id_is_read_from_meta_of_previous_turn(tmp_path):
    """session_meta пишется один раз при создании треда, поэтому её отсекает
    фильтр по времени — идентификатор всё равно должен читаться."""
    old = time.time() - 3600
    now = time.time()
    path = _rollout_path(tmp_path, "019fb75b-0000-4000-8000-000000000000")
    _write_rollout(
        path,
        {
            "timestamp": _iso(old),
            "type": "session_meta",
            "payload": {"id": "019fb75b-0000-4000-8000-000000000000", "cwd": "/srv/project"},
        },
        {"timestamp": _iso(now), "type": "event_msg", "payload": {"type": "agent_message", "message": "текущий"}},
    )

    state = CodexRolloutTail(workdir="/srv/project", started_at=now - 5, home_dir=tmp_path).poll()

    assert state is not None
    assert state.session_id == "019fb75b-0000-4000-8000-000000000000"
    assert state.assistant_text == "текущий"
