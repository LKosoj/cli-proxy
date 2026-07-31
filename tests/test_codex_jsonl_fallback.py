import json
import sys
import time
from datetime import datetime, timezone

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from session import SessionManager


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _build_config(tmp_path, *, fallback_sec: int, emit_start: bool = True) -> AppConfig:
    # Процесс печатает только начало потока и зависает — так вёл себя codex на
    # сломанном треде: приходил thread.started и дальше тишина.
    # emit_start=False моделирует обрыв stdout до первого события.
    start_line = (
        "sys.stdout.write('{\"type\": \"thread.started\", \"thread_id\": \"t1\"}\\n'); sys.stdout.flush(); "
        if emit_start
        else ""
    )
    hang_script = f"import sys, time; {start_line}time.sleep(60)"
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "codex": ToolConfig(
                name="codex",
                mode="headless",
                cmd=[sys.executable, "-c", hang_script],
                separate_stderr=True,
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
            codex_jsonl_fallback_sec=fallback_sec,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


def _write_rollout(home, workdir, *, completed: bool, text: str = "ответ из rollout") -> None:
    path = home / ".codex" / "sessions" / "2026" / "07" / "31" / "rollout-2026-07-31T12-00-00-abc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    records = [
        {"timestamp": _iso(now), "type": "session_meta", "payload": {"id": "abc", "cwd": str(workdir)}},
        {"timestamp": _iso(now), "type": "event_msg", "payload": {"type": "agent_message", "message": text}},
    ]
    if completed:
        records.append(
            {
                "timestamp": _iso(now),
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": text},
            }
        )
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


@pytest.mark.asyncio
async def test_silent_headless_codex_falls_back_to_rollout(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _build_config(tmp_path, fallback_sec=2)
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    _write_rollout(tmp_path, workdir, completed=True)

    manager = SessionManager(cfg)
    sessions = SessionService(manager, TaskService())
    session = sessions.create_session(1, "codex", str(workdir))

    started = time.monotonic()
    result = await session.run_prompt("сделай что-нибудь")

    assert "ответ из rollout" in result
    # Процесс спит 60 секунд: без запасного пути ждали бы его до конца.
    assert time.monotonic() - started < 30
    # Токен, полученный из stdout, важнее прочитанного из файла.
    assert session.resume_token == "t1"


@pytest.mark.asyncio
async def test_fallback_disabled_by_zero_threshold(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _build_config(tmp_path, fallback_sec=0)
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    _write_rollout(tmp_path, workdir, completed=True)

    manager = SessionManager(cfg)
    sessions = SessionService(manager, TaskService())
    session = sessions.create_session(1, "codex", str(workdir))
    session.current_proc = None

    import asyncio

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(session.run_prompt("сделай что-нибудь"), timeout=8)


@pytest.mark.asyncio
async def test_resume_token_is_taken_from_rollout_when_stdout_gave_none(tmp_path, monkeypatch) -> None:
    """Процесс замолчал, не сообщив id треда: без подхвата из rollout следующий
    ход начался бы без контекста диалога."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _build_config(tmp_path, fallback_sec=2, emit_start=False)
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    _write_rollout(tmp_path, workdir, completed=True)

    manager = SessionManager(cfg)
    sessions = SessionService(manager, TaskService())
    session = sessions.create_session(1, "codex", str(workdir))
    assert not session.resume_token

    await session.run_prompt("сделай что-нибудь")

    assert session.resume_token == "abc"


@pytest.mark.asyncio
async def test_progress_is_reported_while_rollout_keeps_growing(tmp_path, monkeypatch) -> None:
    """Пока файл пополняется, ход не завершают — но и молчать нельзя:
    прогресс берётся из rollout, раз stdout мёртв."""
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _build_config(tmp_path, fallback_sec=2)
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    path = tmp_path / ".codex" / "sessions" / "2026" / "07" / "31" / "rollout-2026-07-31T12-00-00-abc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "timestamp": _iso(time.time()), "type": "session_meta", "payload": {"id": "abc", "cwd": str(workdir)},
        }) + "\n")

    async def _keep_writing() -> None:
        for step in range(13):
            await asyncio.sleep(1)
            record = (
                {"type": "task_complete", "last_agent_message": "итог"}
                if step == 12
                else {"type": "agent_message", "message": f"шаг {step}"}
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": _iso(time.time()), "type": "event_msg", "payload": record},
                                        ensure_ascii=False) + "\n")

    manager = SessionManager(cfg)
    sessions = SessionService(manager, TaskService())
    session = sessions.create_session(1, "codex", str(workdir))

    progress: list[str] = []
    original = session._update_activity

    def _spy(text, **kwargs):
        progress.append(str(text))
        return original(text, **kwargs)

    session._update_activity = _spy

    writer = asyncio.create_task(_keep_writing())
    try:
        result = await session.run_prompt("сделай что-нибудь")
    finally:
        writer.cancel()

    assert "итог" in result
    # Промежуточные шаги показаны до завершения хода, а не только финал.
    assert [line for line in progress if line.startswith("шаг ")]


@pytest.mark.asyncio
async def test_fallback_does_not_finish_turn_without_answer(tmp_path, monkeypatch) -> None:
    """Пока в rollout нет ответа, ход не завершается: иначе пользователь
    получил бы пустоту вместо результата."""
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _build_config(tmp_path, fallback_sec=2)
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    path = tmp_path / ".codex" / "sessions" / "2026" / "07" / "31" / "rollout-2026-07-31T12-00-00-abc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "timestamp": _iso(now), "type": "session_meta", "payload": {"id": "abc", "cwd": str(workdir)},
        }) + "\n")
        handle.write(json.dumps({
            "timestamp": _iso(now), "type": "event_msg", "payload": {"type": "task_started"},
        }) + "\n")

    manager = SessionManager(cfg)
    sessions = SessionService(manager, TaskService())
    session = sessions.create_session(1, "codex", str(workdir))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(session.run_prompt("сделай что-нибудь"), timeout=8)
