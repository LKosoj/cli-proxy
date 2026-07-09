import asyncio
import os
import re
import stat
import uuid
from types import SimpleNamespace

import pytest

from config import ToolConfig
from app.services.cli_backends.tmux_backend import TmuxExecutionBackend
from app.services.cli_backends.tmux_driver import TmuxDriverError
from app.services.cli_backends.tmux_parser import done_marker, request_marker


class FakeTmuxDriver:
    def __init__(self):
        self.sessions = set()
        self.log_path = None
        self.pipe_calls = 0
        self.loaded_prompt_path = None
        self.sent_ctrl_c = False
        self.killed = []
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
        request_id = re.search(r"<<<CLI_PROXY_REQUEST:([^>]+)>>>", prompt).group(1)
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
        self.sessions.discard(session_name)
        return True


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


@pytest.mark.asyncio
async def test_tmux_backend_run_returns_delta_and_state(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)

    result = await backend.run(session, "do work")
    status = await backend.status(session)

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
async def test_tmux_backend_restores_generic_resume_token_from_state(tmp_path):
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
async def test_tmux_backend_marks_failed_and_interrupts_on_idle_timeout(tmp_path):
    driver = FakeTmuxDriver()
    driver.write_done_marker = False
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.03)
    session = _session(tmp_path)

    result = await backend.run(session, "do work")
    status = await backend.status(session)

    assert result.abnormal_stop is True
    assert result.text == "assistant answer"
    assert status.state == "failed"
    assert driver.sent_ctrl_c is True


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
async def test_tmux_backend_recreates_existing_failed_session(tmp_path):
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

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    assert driver.killed == [paths["session_name"]]
    assert len(driver.new_session_commands) == 1


@pytest.mark.asyncio
async def test_tmux_backend_recreates_existing_active_session(tmp_path):
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

    result = await backend.run(session, "do work")

    assert result.abnormal_stop is False
    assert driver.killed == [paths["session_name"]]
    assert len(driver.new_session_commands) == 1


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
async def test_tmux_backend_interrupt_marks_failed_when_ctrl_c_is_not_sent(tmp_path):
    driver = FakeTmuxDriver()
    backend = TmuxExecutionBackend(driver=driver, poll_interval_sec=0.01, idle_fallback_sec=0.05)
    session = _session(tmp_path)
    await backend.run(session, "do work")
    driver.ctrl_c_result = False

    assert await backend.interrupt(session) is False
    status = await backend.status(session)

    assert status.state == "failed"
