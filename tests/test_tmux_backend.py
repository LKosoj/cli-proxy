import asyncio
import os
import re
import stat
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.services.cli_backends.tmux_backend as tmux_backend_module
from config import ToolConfig
from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, build_tmux_attach_command
from app.services.cli_backends.tmux_driver import TmuxDriverError
from app.services.cli_backends.tmux_parser import done_marker, request_marker
from app.services.cli_backends.transcript_reader import TranscriptLocator, TranscriptPollResult


class FakeTmuxDriver:
    def __init__(self):
        self.sessions = set()
        self.log_path = None
        self.pipe_calls = 0
        self.loaded_prompt_path = None
        self.sent_ctrl_c = False
        self.killed = []
        self.kill_result = True
        self.new_session_commands = []
        self.fail_load = False
        self.write_done_marker = True
        self.loaded_buffer_name = None
        self.pasted_buffer_name = None
        self.paste_delete = False
        self.deleted_buffers = []
        self.ctrl_c_result = True
        self.fail_paste = False
        self.capture_outputs = ["❯"]
        self.capture_calls = 0
        self.events = []
        self.has_session_calls = []
        self.response_text = "assistant answer"
        self.sent_prompts = []

    async def has_session(self, session_name):
        self.has_session_calls.append(session_name)
        return session_name in self.sessions

    async def new_session(self, session_name, *, workdir, command):
        self.sessions.add(session_name)
        self.new_session_commands.append((session_name, workdir, list(command)))

    async def pipe_pane(self, pane_target, log_path):
        self.pipe_calls += 1
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        open(log_path, "a", encoding="utf-8").close()

    async def load_buffer(self, prompt_path, *, buffer_name=None):
        if self.fail_load:
            raise RuntimeError("load failed")
        self.events.append("load_buffer")
        self.loaded_prompt_path = prompt_path
        self.loaded_buffer_name = buffer_name

    async def paste_buffer(self, pane_target, *, buffer_name=None, delete=False):
        if self.fail_paste:
            raise RuntimeError("paste failed")
        self.events.append("paste_buffer")
        self.pasted_buffer_name = buffer_name
        self.paste_delete = bool(delete)
        return None

    async def delete_buffer(self, *, buffer_name):
        self.deleted_buffers.append(buffer_name)

    async def send_enter(self, pane_target):
        self.events.append("send_enter")
        prompt = open(self.loaded_prompt_path, encoding="utf-8").read()
        self.sent_prompts.append(prompt)
        match = re.search(r"<<<CLI_PROXY_REQUEST:([^>]+)>>>", prompt)
        if match is None:
            return
        request_id = match.group(1)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{request_marker(request_id)}\n{self.response_text}\n")
            if self.write_done_marker:
                handle.write(f"{done_marker(request_id)}\n")

    async def capture_pane(self, pane_target):
        self.capture_calls += 1
        self.events.append("capture_pane")
        if self.capture_outputs:
            return self.capture_outputs.pop(0)
        if self.loaded_prompt_path and os.path.exists(self.loaded_prompt_path):
            return open(self.loaded_prompt_path, encoding="utf-8").read()
        return "❯"

    async def send_ctrl_c(self, pane_target):
        self.sent_ctrl_c = True
        return self.ctrl_c_result

    async def kill_session(self, session_name):
        self.killed.append(session_name)
        if self.kill_result:
            self.sessions.discard(session_name)
        return self.kill_result


def _session(tmp_path):
    return SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        idle_timeout_sec=1,
        tool=ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "-p", "{prompt}"],
            interactive_cmd=["claude"],
            env={"ANTHROPIC_API_KEY": None},
        ),
        cli=SimpleNamespace(active_cli="claude"),
        conversation_scope=SimpleNamespace(session_uid="chat:1:s1"),
    )


def test_tmux_runtime_paths_are_stable_across_thread_rebind(tmp_path):
    backend = TmuxExecutionBackend(driver=FakeTmuxDriver(), poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.conversation_scope = SimpleNamespace(
        chat_id=-100777,
        message_thread_id=10,
        session_uid="thread:-100777:10",
    )
    before = backend.paths(session)

    session.conversation_scope = SimpleNamespace(
        chat_id=-100777,
        message_thread_id=20,
        session_uid="thread:-100777:20",
    )
    after = backend.paths(session)

    other_session = _session(tmp_path)
    other_session.id = "s2"
    other_session.conversation_scope = session.conversation_scope
    other = backend.paths(other_session)

    assert after["runtime_dir"] == before["runtime_dir"]
    assert after["session_name"] == before["session_name"]
    assert other["runtime_dir"] != before["runtime_dir"]
    assert other["session_name"] != before["session_name"]


def test_tmux_attach_command_uses_exact_session_name_and_tmux_user(tmp_path):
    session = _session(tmp_path)
    session.tool.tmux_user = "claude-bot"
    session_name = TmuxExecutionBackend().paths(session)["session_name"]

    command = build_tmux_attach_command(session)

    assert command == f"su - claude-bot -c 'tmux attach-session -r -t {session_name}'"


@pytest.mark.asyncio
async def test_tmux_backend_run_returns_delta_and_state(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    result = await backend.run(
        session,
        "do work",
        request_context={
            "prompt": "do work",
            "dest": {"kind": "telegram", "chat_id": 42, "message_thread_id": 7},
        },
    )
    status = await backend.status(session)
    last_request = TmuxExecutionBackend._read_last_request(backend.paths(session))

    assert result.text == "assistant answer"
    assert result.backend == "tmux"
    assert result.abnormal_stop is False
    assert status.state == "idle"
    assert driver.new_session_commands
    assert "-p" not in driver.new_session_commands[0][2]
    assert driver.loaded_buffer_name == driver.pasted_buffer_name
    assert str(driver.loaded_buffer_name).startswith("cli-proxy-")
    assert driver.paste_delete is True
    assert driver.deleted_buffers == []
    assert last_request["prompt"] == "do work"
    assert last_request["dest"] == {"kind": "telegram", "chat_id": 42, "message_thread_id": 7}
    assert last_request["delivery_state"] == "pending"


@pytest.mark.asyncio
async def test_tmux_backend_prefers_structured_transcript_and_persists_locator(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    driver.response_text = "garbled terminal repaint"
    transcript_path = tmp_path / "structured.jsonl"
    locator = TranscriptLocator(
        provider="claude",
        path=str(transcript_path),
        start_offset=17,
        session_id="structured-session",
    )

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def poll(self):
            return TranscriptPollResult(
                assistant_text="Чистый структурированный ответ",
                progress_text="Bash",
                complete=True,
                available=True,
                recognized=True,
                session_id="structured-session",
                locator=locator,
            )

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    progress_updates = []
    session._update_activity = progress_updates.append

    result = await asyncio.wait_for(
        backend.run(
            session,
            "do work",
            request_context={
                "prompt": "do work",
                "dest": {"kind": "telegram", "chat_id": 42},
            },
        ),
        timeout=1,
    )
    last_request = TmuxExecutionBackend._read_last_request(backend.paths(session))

    assert result.text == "Чистый структурированный ответ"
    assert result.abnormal_stop is False
    assert result.diagnostics["completion_source"] == "transcript"
    assert driver.sent_ctrl_c is False
    assert progress_updates == ["Bash"]
    assert session.last_assistant_text_value == "Чистый структурированный ответ"
    assert session.resume_token == "structured-session"
    assert last_request["transcript_provider"] == "claude"
    assert last_request["transcript_path"] == str(transcript_path)
    assert last_request["transcript_offset"] == 17
    assert last_request["transcript_session_id"] == "structured-session"


@pytest.mark.asyncio
async def test_tmux_backend_uses_structured_transcript_without_request_context(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    locator = TranscriptLocator(
        provider="claude",
        path=str(tmp_path / "structured.jsonl"),
        start_offset=0,
    )

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            assert kwargs["request_id"]

        def poll(self):
            return TranscriptPollResult(
                assistant_text="Структурированный ответ прямого вызова",
                complete=True,
                available=True,
                recognized=True,
                locator=locator,
            )

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)

    result = await backend.run(_session(tmp_path), "do work")

    assert result.text == "Структурированный ответ прямого вызова"
    assert result.abnormal_stop is False
    assert result.diagnostics["completion_source"] == "transcript"


@pytest.mark.asyncio
async def test_tmux_backend_waits_for_structured_completion_after_pane_done(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    locator = TranscriptLocator(
        provider="claude",
        path=str(tmp_path / "structured.jsonl"),
        start_offset=0,
        session_id="structured-session",
    )

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            self.poll_count = 0

        def poll(self):
            self.poll_count += 1
            if self.poll_count == 1:
                return TranscriptPollResult(
                    assistant_text="Промежуточный ответ",
                    available=True,
                    recognized=True,
                    locator=locator,
                )
            return TranscriptPollResult(
                assistant_text="Финал из transcript",
                complete=True,
                available=True,
                recognized=True,
                locator=locator,
            )

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    result = await backend.run(
        session,
        "do work",
        request_context={"prompt": "do work", "dest": {"kind": "telegram", "chat_id": 42}},
    )

    assert result.text == "Финал из transcript"
    assert result.diagnostics["completion_source"] == "transcript"


@pytest.mark.asyncio
async def test_tmux_backend_observe_ignores_stale_completion_markers(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            pass

        def poll(self):
            # Старый DONE уже отдан: журнал по-прежнему рапортует о завершении.
            return TranscriptPollResult(
                assistant_text="Новый текст живого агента",
                complete=True,
                available=True,
                recognized=True,
            )

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)
    backend = TmuxExecutionBackend(
        driver=driver,
        poll_interval_sec=0.01,
        idle_fallback_sec=5.0,
        quiet_timeout_sec=0.3,
    )
    session = _session(tmp_path)
    paths = backend.paths(session)
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write(f"старый ответ {done_marker('req-1')}\n")

    request = tmux_backend_module.TmuxRecoveryRequest(
        request_id="req-1",
        started_at=time.time(),
        offset=0,
        prompt="",
        dest={"kind": "telegram", "chat_id": 42},
        observe=True,
    )
    started_at = time.time()
    result = await asyncio.wait_for(backend.recover(session, request), timeout=5)

    assert result.text == "Новый текст живого агента"
    # Наблюдение живёт до тишины в выводе, а не до первого встреченного маркера.
    assert result.diagnostics["completion_source"].endswith("quiet-timeout")
    assert time.time() - started_at >= 0.3


@pytest.mark.asyncio
async def test_tmux_backend_observe_request_starts_from_current_tail(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write("уже доставленный вывод\n")
    transcript_path = tmp_path / "rollout.jsonl"
    transcript_path.write_text('{"type": "event_msg"}\n', encoding="utf-8")
    backend._write_last_request(
        paths,
        {
            "request_id": "req-1",
            "started_at": 10.0,
            "offset": 0,
            "prompt": "исходный промпт",
            "dest": {"kind": "telegram", "chat_id": 42},
            "delivery_state": "delivered",
            "transcript_provider": "claude",
            "transcript_path": str(transcript_path),
            "transcript_offset": 0,
            "transcript_session_id": "sess-1",
        },
    )

    request = await backend.build_observe_request(session)

    assert request is not None
    assert request.request_id == "req-1"
    # Уже отданный вывод не перечитывается: курсоры стоят на текущем конце.
    assert request.offset == os.path.getsize(paths["pane_log"])
    assert request.dest == {"kind": "telegram", "chat_id": 42}
    assert request.transcript_locator is not None
    assert request.transcript_locator.start_offset == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_tmux_backend_observe_request_skips_idle_pane_on_startup(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write("давно доставленный вывод\n")
    stale = time.time() - 600
    os.utime(paths["pane_log"], (stale, stale))
    backend._write_last_request(
        paths,
        {"request_id": "req-1", "delivery_state": "delivered", "dest": {"kind": "telegram", "chat_id": 42}},
    )

    # Автоподхват на старте: pane молчит, занимать сессию наблюдением не за чем.
    assert await backend.build_observe_request(session, require_recent_activity=True) is None
    # По прямой команде пользователя наблюдение начинается без этой проверки.
    assert await backend.build_observe_request(session) is not None


@pytest.mark.asyncio
async def test_tmux_backend_observe_request_picks_up_live_pane_on_startup(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write("CLI печатает прямо сейчас\n")
    backend._write_last_request(
        paths,
        {"request_id": "req-1", "delivery_state": "delivered", "dest": {"kind": "telegram", "chat_id": 42}},
    )

    request = await backend.build_observe_request(session, require_recent_activity=True)

    assert request is not None
    assert request.observe is True
    assert request.offset == os.path.getsize(paths["pane_log"])


@pytest.mark.asyncio
async def test_tmux_backend_observe_request_requires_live_session(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    backend._write_last_request(paths, {"request_id": "req-1", "delivery_state": "delivered"})

    assert await backend.build_observe_request(session) is None


@pytest.mark.asyncio
async def test_tmux_backend_observe_request_requires_known_request(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)

    assert await backend.build_observe_request(session) is None


@pytest.mark.asyncio
async def test_tmux_backend_falls_back_to_pane_for_unrecognized_transcript(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            pass

        def poll(self):
            return TranscriptPollResult(
                available=True,
                recognized=False,
            )

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    result = await backend.run(
        session,
        "do work",
        request_context={"prompt": "do work", "dest": {"kind": "telegram", "chat_id": 42}},
    )

    assert result.text == "assistant answer"
    assert result.abnormal_stop is False
    assert result.diagnostics["completion_source"] == "pane"


@pytest.mark.asyncio
async def test_tmux_backend_keeps_transcript_when_pane_choice_has_no_options(tmp_path, monkeypatch):
    # Экран TUI перерисован так, что варианты не читаются, а транскрипт распознан:
    # раньше сырой буфер экрана вытеснял текст из JSONL и уходил в чат целиком.
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    driver.response_text = (
        "• Ran find /tmp -name 'host_bwrap' -printf '%T@ %p\\n' | sort -nr\n"
        "  1754161234.5678900 /tmp/pytest-of-root/pytest-242/host_bwrap\n"
        "  1754161200.1234500 /tmp/pytest-of-root/pytest-241/host_bwrap\n"
        "Enter selection [1-2]:"
    )

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            pass

        def poll(self):
            return TranscriptPollResult(
                assistant_text="Чистый ответ из JSONL",
                available=True,
                recognized=True,
            )

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)
    backend = TmuxExecutionBackend(
        driver=driver,
        poll_interval_sec=0.01,
        idle_fallback_sec=5.0,
        quiet_timeout_sec=0.3,
    )

    result = await asyncio.wait_for(backend.run(_session(tmp_path), "do work"), timeout=5)

    assert result.text == "Чистый ответ из JSONL"


@pytest.mark.asyncio
async def test_tmux_backend_reports_only_menu_block_for_real_choice(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    driver.response_text = (
        "• Ran pytest -q\n"
        "  120 passed\n"
        "\n"
        "Do you want to proceed?\n"
        "❯ 1. Yes\n"
        "  2. No, tell Claude what to do differently"
    )

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            pass

        def poll(self):
            return TranscriptPollResult(available=True, recognized=False)

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)
    backend = TmuxExecutionBackend(
        driver=driver,
        poll_interval_sec=0.01,
        idle_fallback_sec=5.0,
        quiet_timeout_sec=0.3,
    )

    result = await asyncio.wait_for(backend.run(_session(tmp_path), "do work"), timeout=5)

    assert result.text == (
        "Do you want to proceed?\n1. Yes\n2. No, tell Claude what to do differently"
    )


@pytest.mark.asyncio
async def test_tmux_backend_sends_plain_input_to_active_session(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session._active_execution_backend = "tmux"
    paths = backend.paths(session)
    driver.sessions.add(paths["session_name"])
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "active",
            "active_request_id": "request-1",
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
            "last_activity_at": 1.0,
        },
    )

    assert await backend.is_active(session) is True
    await backend.send_input(session, "steer the active request")

    state = TmuxExecutionBackend._read_state(paths)
    assert driver.sent_prompts[-1] == "steer the active request"
    assert "CLI_PROXY_REQUEST" not in driver.sent_prompts[-1]
    assert driver.paste_delete is True
    paste_index = driver.events.index("paste_buffer")
    enter_index = driver.events.index("send_enter")
    assert "capture_pane" in driver.events[paste_index + 1:enter_index]
    assert state["state"] == "active"
    assert state["active_request_id"] == "request-1"
    assert state["last_activity_at"] > 1.0


@pytest.mark.asyncio
async def test_tmux_backend_rejects_direct_input_when_request_is_not_active(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session._active_execution_backend = "tmux"
    paths = backend.paths(session)
    driver.sessions.add(paths["session_name"])
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "idle",
            "active_request_id": None,
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
        },
    )

    with pytest.raises(TmuxDriverError, match="active tmux session is unavailable"):
        await backend.send_input(session, "must stay pending")

    assert driver.sent_prompts == []


@pytest.mark.asyncio
async def test_tmux_backend_assigns_claude_session_id_for_first_start(tmp_path, monkeypatch):
    generated = iter(
        [
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
            uuid.UUID("22222222-2222-2222-2222-222222222222"),
        ]
    )
    monkeypatch.setattr("app.services.cli_backends.tmux_backend.uuid.uuid4", lambda: next(generated))
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    result = await backend.run(session, "first")
    state = TmuxExecutionBackend._read_state(backend.paths(session))

    assert result.abnormal_stop is False
    assert session.resume_token == "11111111-1111-1111-1111-111111111111"
    assert driver.new_session_commands[0][2][-2:] == [
        "--session-id",
        "11111111-1111-1111-1111-111111111111",
    ]
    assert state["claude_resume_token"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_tmux_backend_assigns_grok_session_id_for_transcript_discovery(tmp_path, monkeypatch):
    generated = iter(
        [
            uuid.UUID("33333333-3333-4333-8333-333333333333"),
            uuid.UUID("44444444-4444-4444-8444-444444444444"),
        ]
    )
    monkeypatch.setattr("app.services.cli_backends.tmux_backend.uuid.uuid4", lambda: next(generated))
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "grok"
    session.tool.interactive_cmd = ["grok", "--no-alt-screen", "--minimal"]
    session.cli.active_cli = "grok"

    result = await backend.run(session, "first")

    assert result.abnormal_stop is False
    assert session.resume_token == "33333333-3333-4333-8333-333333333333"
    assert driver.new_session_commands[0][2][-2:] == [
        "--session-id",
        "33333333-3333-4333-8333-333333333333",
    ]


@pytest.mark.asyncio
async def test_tmux_backend_resumes_claude_when_tmux_session_missing_after_reboot(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.resume_token = "claude-resume-token"
    paths = backend.paths(session)
    TmuxExecutionBackend._write_state(
        paths,
        {
            "schema_version": 1,
            "backend": "tmux",
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
            "state": "idle",
            "claude_resume_token": "claude-resume-token",
        },
    )

    result = await backend.run(session, "after reboot")

    assert result.abnormal_stop is False
    assert driver.has_session_calls == [paths["session_name"]]
    assert driver.new_session_commands[0][2][-2:] == ["--resume", "claude-resume-token"]


@pytest.mark.asyncio
async def test_tmux_backend_respects_preconfigured_claude_session_flags(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.interactive_cmd = ["claude", "--continue"]

    result = await backend.run(session, "first")

    assert result.abnormal_stop is False
    assert not hasattr(session, "resume_token")
    assert driver.new_session_commands[0][2][-2:] == ["claude", "--continue"]


@pytest.mark.asyncio
async def test_tmux_backend_assigns_qwen_session_id_and_resumes_after_reboot(tmp_path, monkeypatch):
    generated = iter(
        [
            uuid.UUID("33333333-3333-3333-3333-333333333333"),
            uuid.UUID("44444444-4444-4444-4444-444444444444"),
            uuid.UUID("55555555-5555-5555-5555-555555555555"),
            uuid.UUID("66666666-6666-6666-6666-666666666666"),
        ]
    )
    monkeypatch.setattr("app.services.cli_backends.tmux_backend.uuid.uuid4", lambda: next(generated))
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "qwen"
    session.tool.interactive_cmd = ["qwen", "--session-id", "{session_id}"]
    session.tool.interactive_resume_cmd = ["qwen", "--resume", "{resume}"]
    session.cli.active_cli = "qwen"

    first = await backend.run(session, "first")
    first_state = TmuxExecutionBackend._read_state(backend.paths(session))
    driver.sessions.clear()
    second = await backend.run(session, "after reboot")
    second_state = TmuxExecutionBackend._read_state(backend.paths(session))

    assert first.abnormal_stop is False
    assert second.abnormal_stop is False
    assert session.resume_token == "33333333-3333-3333-3333-333333333333"
    assert driver.new_session_commands[0][2][-2:] == [
        "--session-id",
        "33333333-3333-3333-3333-333333333333",
    ]
    assert driver.new_session_commands[1][2][-3:] == [
        "qwen",
        "--resume",
        "33333333-3333-3333-3333-333333333333",
    ]
    assert first_state["resume_token"] == "33333333-3333-3333-3333-333333333333"
    assert first_state["claude_resume_token"] is None
    assert second_state["resume_token"] == "33333333-3333-3333-3333-333333333333"


@pytest.mark.asyncio
async def test_tmux_backend_restores_resume_token_from_idle_state_but_not_stopped(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "grok"
    session.tool.interactive_cmd = ["grok", "--no-auto-update"]
    session.tool.interactive_resume_cmd = ["grok", "--no-auto-update", "--resume", "{resume}"]
    session.cli.active_cli = "grok"
    paths = backend.paths(session)
    TmuxExecutionBackend._write_state(
        paths,
        {
            "schema_version": 1,
            "backend": "tmux",
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
            "state": "idle",
            "resume_token": "grok-resume-token",
        },
    )

    result = await backend.run(session, "after reboot")

    assert result.abnormal_stop is False
    assert session.resume_token == "grok-resume-token"
    assert driver.new_session_commands[0][2][-4:] == [
        "grok",
        "--no-auto-update",
        "--resume",
        "grok-resume-token",
    ]

    await backend.close(session)
    session.resume_token = None
    fresh_result = await backend.run(session, "after reset")

    assert fresh_result.abnormal_stop is False
    assert "--resume" not in driver.new_session_commands[1][2]
    assert "grok-resume-token" not in driver.new_session_commands[1][2]


@pytest.mark.asyncio
async def test_tmux_backend_appends_token_to_explicit_resume_command_without_placeholder(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "gemini"
    session.tool.interactive_cmd = ["gemini"]
    session.tool.interactive_resume_cmd = ["gemini", "--resume"]
    session.cli.active_cli = "gemini"
    session.resume_token = "gemini-resume-token"

    result = await backend.run(session, "after reboot")

    assert result.abnormal_stop is False
    assert driver.new_session_commands[0][2][-3:] == ["gemini", "--resume", "gemini-resume-token"]


@pytest.mark.asyncio
async def test_tmux_backend_fails_if_resume_token_has_no_resume_command(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "unknown-cli"
    session.tool.interactive_cmd = ["unknown-cli"]
    session.cli.active_cli = "unknown-cli"
    session.resume_token = "unknown-token"

    with pytest.raises(TmuxDriverError, match="interactive resume command is not configured"):
        await backend.run(session, "after reboot")

    assert driver.new_session_commands == []


@pytest.mark.asyncio
async def test_tmux_backend_builtin_qwen_resume_drops_session_id_placeholder(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "qwen"
    session.tool.interactive_cmd = ["qwen", "--session-id", "{session_id}"]
    session.cli.active_cli = "qwen"
    session.resume_token = "qwen-token"

    result = await backend.run(session, "after reboot")

    assert result.abnormal_stop is False
    assert driver.new_session_commands[0][2][-3:] == ["qwen", "--resume", "qwen-token"]
    assert "{session_id}" not in driver.new_session_commands[0][2]


@pytest.mark.asyncio
async def test_tmux_backend_builtin_claude_resume_drops_session_id_placeholder(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.interactive_cmd = ["claude", "--dangerously-skip-permissions", "--session-id", "{session_id}"]
    session.resume_token = "claude-token"

    result = await backend.run(session, "after reboot")

    assert result.abnormal_stop is False
    assert driver.new_session_commands[0][2][-4:] == [
        "claude",
        "--dangerously-skip-permissions",
        "--resume",
        "claude-token",
    ]
    assert "{session_id}" not in driver.new_session_commands[0][2]


@pytest.mark.asyncio
async def test_tmux_backend_codex_resume_keeps_config_override_flag(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "codex"
    session.tool.interactive_cmd = ["codex", "-c", "model=test-model"]
    session.cli.active_cli = "codex"
    session.resume_token = "codex-resume-token"

    result = await backend.run(session, "after reboot")

    assert result.abnormal_stop is False
    assert driver.new_session_commands[0][2][-5:] == [
        "codex",
        "resume",
        "-c",
        "model=test-model",
        "codex-resume-token",
    ]


@pytest.mark.asyncio
async def test_tmux_backend_uses_codex_resume_subcommand_for_resume_token(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "codex"
    session.tool.interactive_cmd = ["codex"]
    session.cli.active_cli = "codex"
    session.resume_token = "codex-resume-token"

    result = await backend.run(session, "after reboot")

    assert result.abnormal_stop is False
    assert driver.new_session_commands[0][2][-3:] == ["codex", "resume", "codex-resume-token"]


@pytest.mark.asyncio
async def test_tmux_backend_captures_generic_resume_token_from_output(tmp_path):
    driver = FakeTmuxDriver()
    driver.response_text = "session_id=grok-captured-token\nassistant answer"
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "grok"
    session.tool.interactive_cmd = ["grok", "--no-auto-update"]
    session.tool.resume_regex = r"session_id=(\S+)"
    session.cli.active_cli = "grok"

    result = await backend.run(session, "first")
    state = TmuxExecutionBackend._read_state(backend.paths(session))

    assert result.abnormal_stop is False
    assert session.resume_token == "grok-captured-token"
    assert state["resume_token"] == "grok-captured-token"


@pytest.mark.asyncio
async def test_tmux_backend_updates_existing_resume_token_from_output(tmp_path):
    driver = FakeTmuxDriver()
    driver.response_text = "session_id=rotated-token\nassistant answer"
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = "grok"
    session.tool.interactive_cmd = ["grok", "--no-auto-update"]
    session.tool.interactive_resume_cmd = ["grok", "--no-auto-update", "--resume", "{resume}"]
    session.tool.resume_regex = r"session_id=(\S+)"
    session.cli.active_cli = "grok"
    session.resume_token = "old-token"

    result = await backend.run(session, "continue")
    state = TmuxExecutionBackend._read_state(backend.paths(session))

    assert result.abnormal_stop is False
    assert session.resume_token == "rotated-token"
    assert state["resume_token"] == "rotated-token"


@pytest.mark.asyncio
async def test_tmux_backend_reuses_existing_session_without_reopening_pipe(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)

    first = await backend.run(session, "first")
    second = await backend.run(session, "second")

    assert first.abnormal_stop is False
    assert second.abnormal_stop is False
    assert driver.has_session_calls == [paths["session_name"], paths["session_name"]]
    assert len(driver.new_session_commands) == 1
    assert driver.pipe_calls == 1


@pytest.mark.asyncio
async def test_tmux_backend_waits_for_claude_prompt_before_pasting(tmp_path):
    driver = FakeTmuxDriver()
    driver.capture_outputs = ["", "", "❯"]
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    assert driver.capture_calls >= 3
    assert driver.events.index("capture_pane") < driver.events.index("load_buffer")


@pytest.mark.asyncio
async def test_tmux_backend_accepts_configured_claude_screen_reader_prompt(tmp_path):
    driver = FakeTmuxDriver()
    driver.capture_outputs = [
        "Claude Code v2.1.206\nLoading...",
        "Conversation compacted\nSkills restored\n$",
        *(["Claude Code v2.1.206\nLoading..."] * 20),
    ]
    backend = TmuxExecutionBackend(
        driver=driver,
        poll_interval_sec=0.01,
        idle_fallback_sec=0.05,
        startup_timeout_sec=0.05,
    )
    session = _session(tmp_path)
    session.tool.interactive_cmd = ["claude", "--ax-screen-reader"]

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    assert driver.capture_calls >= 2
    assert driver.events.index("capture_pane") < driver.events.index("load_buffer")


@pytest.mark.asyncio
async def test_tmux_backend_returns_only_final_claude_screen_reader_message(tmp_path):
    driver = FakeTmuxDriver()
    driver.capture_outputs = ["$"]
    driver.response_text = (
        "$Scampering… (18s · thinking with xhigh effort)\n"
        "$tool: Bash (grep follow-up docs/sdd.md) Waiting…\n"
        "$Running…\n"
        "198: PromptBudgetBuilder — follow-up.\n"
        "$claude: Чистый финальный ответ."
    )
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.interactive_cmd = ["claude", "--ax-screen-reader"]

    result = await backend.run(session, "do work")

    assert result.text == "Чистый финальный ответ."


def test_tmux_backend_does_not_treat_plain_shell_prompt_as_ready_claude() -> None:
    session = SimpleNamespace(
        tool=ToolConfig(name="claude", mode="headless", cmd=["claude"], interactive_cmd=["claude"]),
        cli=SimpleNamespace(active_cli="claude"),
    )

    assert TmuxExecutionBackend._is_interactive_ready(session, "bash\nuser@host:/tmp$") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cli_name", "ready_output"),
    [
        ("codex", "›"),
        ("qwen", "➜ qwen · qwen3.7-max\n> Введите сообщение"),
        ("grok", "Grok Build 0.2.56\n│❯│"),
    ],
)
async def test_tmux_backend_waits_for_generic_cli_prompt_before_pasting(tmp_path, cli_name, ready_output):
    driver = FakeTmuxDriver()
    driver.capture_outputs = ["", ready_output]
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = cli_name
    session.tool.interactive_cmd = [cli_name]
    session.cli.active_cli = cli_name

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    assert driver.events.index("capture_pane") < driver.events.index("load_buffer")


@pytest.mark.asyncio
async def test_tmux_backend_waits_for_claude_pasted_prompt_before_enter(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    paste_index = driver.events.index("paste_buffer")
    submit_index = driver.events.index("send_enter")
    assert "capture_pane" in driver.events[paste_index + 1:submit_index]


@pytest.mark.asyncio
async def test_tmux_backend_fails_fast_on_claude_trust_prompt(tmp_path):
    driver = FakeTmuxDriver()
    driver.capture_outputs = ["Do you trust the files in this folder?"]
    backend = TmuxExecutionBackend(
        driver=driver,
        poll_interval_sec=0.01,
        idle_fallback_sec=0.05,
        startup_timeout_sec=0.05,
    )
    session = _session(tmp_path)

    with pytest.raises(TmuxDriverError, match="workspace trust prompt"):
        await backend.run(session, "do work")

    status = await backend.status(session)
    assert status.state == "failed"
    assert driver.loaded_prompt_path is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_name", ["claude", "codex", "grok"])
async def test_tmux_backend_keeps_waiting_for_live_session_after_idle_timeout(tmp_path, cli_name):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.03)
    session = _session(tmp_path)
    session.tool.name = cli_name
    session.tool.interactive_cmd = [cli_name]
    session.cli.active_cli = cli_name

    task = asyncio.create_task(backend.run(session, "do work"))
    for _ in range(100):
        if len(driver.has_session_calls) >= 2:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("tmux backend did not probe the idle session")

    assert task.done() is False
    assert driver.sent_ctrl_c is False

    request = TmuxExecutionBackend._read_last_request(backend.paths(session))
    with open(driver.log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{done_marker(request['request_id'])}\n")

    result = await asyncio.wait_for(task, timeout=1)
    status = await backend.status(session)

    assert result.abnormal_stop is False
    assert result.text == "assistant answer"
    assert status.state == "idle"
    assert driver.sent_ctrl_c is False


@pytest.mark.asyncio
async def test_tmux_backend_marks_failed_without_interrupt_when_session_disappears(tmp_path):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.03)
    session = _session(tmp_path)
    paths = backend.paths(session)

    task = asyncio.create_task(backend.run(session, "do work"))
    for _ in range(100):
        status = await backend.status(session)
        if status.state == "active":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("tmux backend did not enter active state")
    driver.sessions.discard(paths["session_name"])

    result = await asyncio.wait_for(task, timeout=1)
    status = await backend.status(session)

    assert result.abnormal_stop is True
    assert result.text == "assistant answer"
    assert status.state == "failed"
    assert driver.sent_ctrl_c is False


@pytest.mark.asyncio
async def test_tmux_backend_cancellation_interrupts_and_closes_active_state(tmp_path):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=30)
    session = _session(tmp_path)

    task = asyncio.create_task(backend.run(session, "do work"))
    for _ in range(100):
        status = await backend.status(session)
        if status.state == "active":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("tmux backend did not enter active state")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await backend.status(session)

    assert status.state == "idle"
    assert driver.sent_ctrl_c is True


@pytest.mark.asyncio
async def test_tmux_backend_shutdown_cancellation_preserves_active_session(tmp_path):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=30)
    session = _session(tmp_path)
    session._preserve_tmux_on_shutdown = True

    task = asyncio.create_task(backend.run(session, "do work"))
    for _ in range(100):
        status = await backend.status(session)
        if status.state == "active":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("tmux backend did not enter active state")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await backend.status(session)

    assert status.state == "active"
    assert driver.sent_ctrl_c is False
    assert status.session_name in driver.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_name", ["claude", "codex", "grok", "qwen"])
async def test_tmux_backend_recovers_existing_active_request_for_any_cli(tmp_path, cli_name):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.name = cli_name
    session.tool.interactive_cmd = [cli_name]
    session.cli.active_cli = cli_name
    paths = backend.paths(session)
    request_id = f"recover-{cli_name}"
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write(
            f"{request_marker(request_id)}\n"
            f"recovered {cli_name} answer\n"
            f"{done_marker(request_id)}\n"
        )
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "active",
            "active_request_id": request_id,
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
        },
    )
    TmuxExecutionBackend._write_last_request(
        paths,
        {
            "request_id": request_id,
            "started_at": 10.0,
            "offset": 0,
            "prompt": f"original {cli_name} prompt",
            "dest": {"kind": "telegram", "chat_id": 42, "message_thread_id": 7},
            "delivery_state": "pending",
        },
    )

    recovery = await backend.get_recovery_request(session)
    assert recovery is not None
    result = await backend.recover(session, recovery)

    assert result.text == f"recovered {cli_name} answer"
    assert result.request_id == request_id
    assert recovery.prompt == f"original {cli_name} prompt"
    assert recovery.dest == {"kind": "telegram", "chat_id": 42, "message_thread_id": 7}
    assert driver.killed == []
    assert driver.new_session_commands == []
    assert driver.loaded_prompt_path is None


@pytest.mark.asyncio
async def test_tmux_backend_recovers_late_structured_completion_from_failed_state(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    request_id = "late-structured"
    transcript_path = tmp_path / "claude.jsonl"
    locator = TranscriptLocator(
        provider="claude",
        path=str(transcript_path),
        start_offset=23,
        session_id="late-session",
    )
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write(f"{request_marker(request_id)}\ngarbled unfinished pane\n")
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "failed",
            "active_request_id": None,
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
        },
    )
    TmuxExecutionBackend._write_last_request(
        paths,
        {
            "request_id": request_id,
            "started_at": 10.0,
            "offset": 0,
            "prompt": "original prompt",
            "dest": {"kind": "telegram", "chat_id": 42},
            "delivery_state": "pending",
            "transcript_provider": locator.provider,
            "transcript_path": locator.path,
            "transcript_offset": locator.start_offset,
            "transcript_session_id": locator.session_id,
        },
    )

    class FakeTranscriptReader:
        def __init__(self, **kwargs):
            assert kwargs["locator"] == locator

        def poll(self):
            return TranscriptPollResult(
                assistant_text="Поздний финальный ответ",
                complete=True,
                available=True,
                recognized=True,
                session_id=locator.session_id,
                locator=locator,
            )

    monkeypatch.setattr(tmux_backend_module, "CliTranscriptReader", FakeTranscriptReader)

    recovery = await backend.get_recovery_request(session)
    assert recovery is not None
    assert recovery.transcript_locator == locator

    result = await backend.recover(session, recovery)

    assert result.text == "Поздний финальный ответ"
    assert result.abnormal_stop is False
    assert result.diagnostics["completion_source"] == "transcript"


@pytest.mark.asyncio
async def test_tmux_backend_does_not_recover_delivered_request(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    request_id = "already-delivered"
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write(f"{request_marker(request_id)}\nanswer\n{done_marker(request_id)}\n")
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "idle",
            "active_request_id": None,
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
        },
    )
    TmuxExecutionBackend._write_last_request(
        paths,
        {
            "request_id": request_id,
            "started_at": 10.0,
            "offset": 0,
            "delivery_state": "pending",
        },
    )

    assert await backend.get_recovery_request(session) is not None
    assert backend.mark_request_delivered(session, request_id) is True
    assert await backend.get_recovery_request(session) is None


@pytest.mark.asyncio
async def test_tmux_backend_rolls_back_active_state_when_send_fails(tmp_path):
    driver = FakeTmuxDriver()
    driver.fail_load = True
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.03)
    session = _session(tmp_path)

    with pytest.raises(RuntimeError, match="load failed"):
        await backend.run(session, "do work")

    status = await backend.status(session)
    assert status.state == "failed"


@pytest.mark.asyncio
async def test_tmux_backend_deletes_named_buffer_when_paste_fails(tmp_path):
    driver = FakeTmuxDriver()
    driver.fail_paste = True
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.03)
    session = _session(tmp_path)

    with pytest.raises(RuntimeError, match="paste failed"):
        await backend.run(session, "do work")

    status = await backend.status(session)
    assert status.state == "failed"
    assert driver.loaded_buffer_name is not None
    assert driver.deleted_buffers == [driver.loaded_buffer_name]


@pytest.mark.asyncio
async def test_tmux_backend_reuses_existing_failed_session_when_prompt_is_ready(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "failed",
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
        },
    )
    driver.sessions.add(paths["session_name"])
    driver.log_path = paths["pane_log"]

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    assert driver.killed == []
    assert driver.new_session_commands == []
    assert driver.events.index("capture_pane") < driver.events.index("load_buffer")


@pytest.mark.asyncio
async def test_tmux_backend_reuses_existing_active_session_when_prompt_is_ready(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "active",
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
            "active_request_id": "stale-request",
        },
    )
    driver.sessions.add(paths["session_name"])
    driver.log_path = paths["pane_log"]

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    assert driver.killed == []
    assert driver.new_session_commands == []
    assert driver.events.index("capture_pane") < driver.events.index("load_buffer")


@pytest.mark.asyncio
async def test_tmux_backend_preserves_busy_existing_session_when_prompt_is_not_ready(tmp_path):
    driver = FakeTmuxDriver()
    driver.capture_outputs = ["still working"] * 20
    backend = TmuxExecutionBackend(
        driver=driver,
        poll_interval_sec=0.01,
        idle_fallback_sec=0.05,
        startup_timeout_sec=0.03,
    )
    session = _session(tmp_path)
    paths = backend.paths(session)
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "active",
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
            "active_request_id": "legacy-request",
        },
    )
    driver.sessions.add(paths["session_name"])

    with pytest.raises(TmuxDriverError, match="interactive prompt did not become ready"):
        await backend.run(session, "do work")

    assert driver.killed == []
    assert driver.new_session_commands == []
    assert driver.loaded_prompt_path is None


@pytest.mark.asyncio
async def test_tmux_backend_prepares_runtime_permissions_for_su_user(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    driver.user = "claude-bot"
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    chown_calls: list[tuple[str, int, int]] = []

    monkeypatch.setattr("app.services.cli_backends.tmux_backend.resolve_user_identity", lambda user: (123, 456))
    monkeypatch.setattr(
        "app.services.cli_backends.tmux_backend.os.chown",
        lambda path, uid, gid: chown_calls.append((str(path), int(uid), int(gid))),
    )

    await backend._ensure_started(session, paths)

    runtime_dir = paths["runtime_dir"]
    tmux_dir = os.path.dirname(runtime_dir)
    runtime_parent = os.path.dirname(tmux_dir)
    pane_log = paths["pane_log"]
    assert (runtime_parent, 123, 456) in chown_calls
    assert (tmux_dir, 123, 456) in chown_calls
    assert (runtime_dir, 123, 456) in chown_calls
    assert (pane_log, 123, 456) in chown_calls
    assert stat.S_IMODE(os.stat(runtime_dir).st_mode) == stat.S_IRWXU
    assert stat.S_IMODE(os.stat(pane_log).st_mode) == stat.S_IRUSR | stat.S_IWUSR


@pytest.mark.asyncio
async def test_tmux_backend_prepares_private_runtime_permissions_without_su_user(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)

    await backend._ensure_started(session, paths)

    assert stat.S_IMODE(os.stat(paths["runtime_dir"]).st_mode) == stat.S_IRWXU
    assert stat.S_IMODE(os.stat(paths["pane_log"]).st_mode) == stat.S_IRUSR | stat.S_IWUSR


@pytest.mark.asyncio
async def test_tmux_backend_rejects_images_without_headless_fallback(tmp_path):
    backend = TmuxExecutionBackend(driver=FakeTmuxDriver(), poll_interval_sec=0.01, idle_fallback_sec=0.05)

    with pytest.raises(RuntimeError, match="does not support image"):
        await backend.run(_session(tmp_path), "describe", image_path="/tmp/image.png")


@pytest.mark.asyncio
async def test_tmux_backend_requires_interactive_cmd_at_runtime(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    session.tool.interactive_cmd = None

    with pytest.raises(TmuxDriverError, match="interactive_cmd is required"):
        await backend.run(session, "do work")

    assert driver.new_session_commands == []


@pytest.mark.asyncio
async def test_tmux_backend_interrupt_and_close_are_idempotent(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    await backend.run(session, "do work")
    assert await backend.interrupt(session) is True
    await backend.close(session)
    await backend.close(session)
    status = await backend.status(session)

    assert driver.sent_ctrl_c is True
    assert len(driver.killed) == 2
    assert status.state == "stopped"


@pytest.mark.asyncio
async def test_tmux_backend_close_fails_if_session_remains_alive(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    await backend.run(session, "do work")
    driver.kill_result = False

    with pytest.raises(TmuxDriverError, match="still running after close"):
        await backend.close(session)

    status = await backend.status(session)
    assert status.state == "idle"
    assert backend.paths(session)["session_name"] in driver.sessions


@pytest.mark.asyncio
async def test_tmux_backend_interrupt_marks_failed_when_ctrl_c_is_not_sent(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    await backend.run(session, "do work")
    driver.ctrl_c_result = False

    assert await backend.interrupt(session) is False
    status = await backend.status(session)

    assert status.state == "failed"


def test_read_pane_chunk_returns_only_new_bytes(tmp_path):
    log_path = str(tmp_path / "pane.log")
    open(log_path, "w", encoding="utf-8").write("first\n")

    data, cursor, truncated = tmux_backend_module._read_pane_chunk(log_path, 0)
    assert data == b"first\n"
    assert cursor == 6
    assert truncated is False

    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("second\n")

    data, cursor, truncated = tmux_backend_module._read_pane_chunk(log_path, cursor)
    assert data == b"second\n"
    assert cursor == 13
    assert truncated is False

    data, cursor, truncated = tmux_backend_module._read_pane_chunk(log_path, cursor)
    assert data == b""
    assert truncated is False


def test_read_pane_chunk_reports_truncation(tmp_path):
    log_path = str(tmp_path / "pane.log")
    open(log_path, "w", encoding="utf-8").write("short\n")

    data, cursor, truncated = tmux_backend_module._read_pane_chunk(log_path, 500)

    assert truncated is True
    assert data == b"short\n"
    assert cursor == 6


def test_pane_stream_restarts_screen_after_truncation(tmp_path):
    log_path = str(tmp_path / "pane.log")
    request_id = "req-trunc"
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(f"{request_marker(request_id)}\nстарый вывод\n")
    stream = tmux_backend_module._PaneStream(log_path, request_id)

    assert "старый вывод" in stream.advance().parsed.text

    # pipe-pane пересоздал журнал уже после маркера запроса: экран от прошлого
    # потока не должен протечь в разбор.
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("новый вывод\n")

    text = stream.advance().parsed.text
    assert "новый вывод" in text
    assert "старый вывод" not in text


def test_read_pane_chunk_limits_catchup_to_tail(tmp_path):
    log_path = str(tmp_path / "pane.log")
    with open(log_path, "wb") as handle:
        handle.write(b"a" * 500 + b"b" * 100)

    data, cursor, discontinuous = tmux_backend_module._read_pane_chunk(log_path, 0, max_bytes=100)

    assert data == b"b" * 100
    assert cursor == 600
    assert discontinuous is True

    # Отставание в пределах лимита читается целиком и разрывом не считается.
    with open(log_path, "ab") as handle:
        handle.write(b"c" * 50)
    data, cursor, discontinuous = tmux_backend_module._read_pane_chunk(log_path, cursor, max_bytes=100)

    assert data == b"c" * 50
    assert cursor == 650
    assert discontinuous is False


def test_pane_stream_catches_up_by_tail_after_recovery_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(tmux_backend_module, "_PANE_CATCHUP_TAIL_BYTES", 4096)
    request_id = "req-catchup"
    log_path = str(tmp_path / "pane.log")
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(f"{request_marker(request_id)}\n")
        handle.write("история запроса\n" * 5000)
        handle.write(f"итоговый ответ\n{done_marker(request_id)}\n")

    fed: list[str] = []
    original_feed = tmux_backend_module.TmuxDeltaReader.feed

    def _tracking_feed(self, chunk):
        fed.append(chunk)
        return original_feed(self, chunk)

    monkeypatch.setattr(tmux_backend_module.TmuxDeltaReader, "feed", _tracking_feed)

    # Восстановление стартует с начала запроса, а журнал успел вырасти: разбор
    # должен ограничиться хвостом, где и лежит маркер завершения.
    stream = tmux_backend_module._PaneStream(log_path, request_id, offset=0)
    parsed = stream.advance().parsed

    assert parsed.complete is True
    assert "итоговый ответ" in parsed.text
    # Символов не больше, чем прочитанных байт: в эмулятор ушёл только хвост.
    assert sum(len(chunk) for chunk in fed) <= 4096

    # Курсор после прыжка стоит на конце файла: дальше читается только дописанное.
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("продолжение\n")
    fed.clear()
    stream.advance()

    assert fed == ["продолжение\n"]


def test_pane_stream_returns_resume_token_without_touching_session(tmp_path):
    log_path = str(tmp_path / "pane.log")
    request_id = "req-token"
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(f"{request_marker(request_id)}\nsession_id: abcd-1234\n")
    stream = tmux_backend_module._PaneStream(log_path, request_id)

    advance = stream.advance(resume_regex=r"session_id:\s*([0-9a-z-]{8,})")

    assert advance.resume_token == "abcd-1234"
    assert stream.advance(resume_regex=r"session_id:\s*([0-9a-z-]{8,})").resume_token is None


@pytest.mark.asyncio
async def test_tmux_backend_feeds_pane_log_without_rereading_history(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    driver.response_text = "streaming start"
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=5)
    session = _session(tmp_path)

    fed: list[str] = []
    original_feed = tmux_backend_module.TmuxDeltaReader.feed

    def _tracking_feed(self, chunk):
        fed.append(chunk)
        return original_feed(self, chunk)

    monkeypatch.setattr(tmux_backend_module.TmuxDeltaReader, "feed", _tracking_feed)

    async def _stream_output():
        # CLI дописывает вывод порциями, поэтому цикл мониторинга успевает
        # сделать несколько опросов до завершения запроса.
        while driver.log_path is None or not driver.sent_prompts:
            await asyncio.sleep(0.01)
        request_id = re.search(r"<<<CLI_PROXY_REQUEST:([^>]+)>>>", driver.sent_prompts[0]).group(1)
        for index in range(5):
            with open(driver.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"chunk {index}: " + "x" * 500 + "\n")
            await asyncio.sleep(0.05)
        with open(driver.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{done_marker(request_id)}\n")

    streamer = asyncio.create_task(_stream_output())
    try:
        await backend.run(session, "do work")
    finally:
        streamer.cancel()
        await asyncio.gather(streamer, return_exceptions=True)

    pane_size = os.path.getsize(backend.paths(session)["pane_log"])
    total_fed = sum(len(chunk.encode("utf-8")) for chunk in fed)

    # Каждый байт pane.log попадает в эмулятор ровно один раз: раньше цикл
    # мониторинга переигрывал всю историю запроса на каждом опросе.
    assert len(fed) > 1, "цикл должен был сделать несколько опросов"
    assert total_fed <= pane_size


def _slow_pane_read(monkeypatch, delay: float) -> None:
    """Имитирует чтение многомегабайтного pane.log: блокирующая задержка на syscall."""

    original = tmux_backend_module._read_pane_chunk

    def _slow(log_path, cursor, **kwargs):
        time.sleep(delay)
        return original(log_path, cursor, **kwargs)

    monkeypatch.setattr(tmux_backend_module, "_read_pane_chunk", _slow)


@asynccontextmanager
async def _loop_lag_probe():
    """Копит задержки между тиками: они растут, если цикл событий кто-то держит."""

    lags: list[float] = []

    async def _tick():
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.01)
            lags.append(time.monotonic() - before)

    task = asyncio.create_task(_tick())
    await asyncio.sleep(0)
    try:
        yield lags
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_tmux_monitor_keeps_event_loop_responsive(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=5)
    session = _session(tmp_path)
    _slow_pane_read(monkeypatch, 0.8)

    async with _loop_lag_probe() as lags:
        await backend.run(session, "do work")

    assert lags, "проба должна была сделать хотя бы один тик"
    # Чтение pane.log и разбор экрана уходят в поток, поэтому цикл событий
    # продолжает крутиться всё время, пока они длятся.
    assert max(lags) < 0.3


@pytest.mark.asyncio
async def test_tmux_recovery_check_keeps_event_loop_responsive(tmp_path, monkeypatch):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    paths = backend.paths(session)
    request_id = "recovery-lag"
    driver.sessions.add(paths["session_name"])
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    with open(paths["pane_log"], "w", encoding="utf-8") as handle:
        handle.write(f"{request_marker(request_id)}\nответ\n{done_marker(request_id)}\n")
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "failed",
            "active_request_id": None,
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
        },
    )
    TmuxExecutionBackend._write_last_request(
        paths,
        {"request_id": request_id, "started_at": 10.0, "offset": 0, "delivery_state": "pending"},
    )
    _slow_pane_read(monkeypatch, 0.8)

    async with _loop_lag_probe() as lags:
        recovery = await backend.get_recovery_request(session)

    assert recovery is not None
    assert lags, "проба должна была сделать хотя бы один тик"
    assert max(lags) < 0.3


@pytest.mark.asyncio
async def test_tmux_backend_captures_resume_token_split_across_reads(tmp_path):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    driver.response_text = "working"
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=5)
    session = _session(tmp_path)
    session.tool.resume_regex = r"session_id:\s*([0-9a-f-]{8,})"

    async def _stream_output():
        while driver.log_path is None or not driver.sent_prompts:
            await asyncio.sleep(0.01)
        request_id = re.search(r"<<<CLI_PROXY_REQUEST:([^>]+)>>>", driver.sent_prompts[0]).group(1)
        # Токен разрывается между двумя опросами: первый кусок обрывается
        # посреди строки с идентификатором сессии.
        with open(driver.log_path, "a", encoding="utf-8") as handle:
            handle.write("session_id: 1234abcd-")
        await asyncio.sleep(0.08)
        with open(driver.log_path, "a", encoding="utf-8") as handle:
            handle.write("5678-9abc\n")
        await asyncio.sleep(0.08)
        with open(driver.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{done_marker(request_id)}\n")

    streamer = asyncio.create_task(_stream_output())
    try:
        await backend.run(session, "do work")
    finally:
        streamer.cancel()
        await asyncio.gather(streamer, return_exceptions=True)

    assert session.resume_token == "1234abcd-5678-9abc"
