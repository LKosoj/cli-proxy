from __future__ import annotations

import asyncio

import pytest

from app.services.cli_backends.models import ExecutionResult
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import SessionManager


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1]),
        tools={
            "claude": ToolConfig(
                name="claude",
                mode="headless",
                cmd=["claude", "-p", "{prompt}"],
                interactive_cmd=["claude"],
                execution_backends=["headless", "tmux"],
                default_execution_backend="headless",
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            default_cli="claude",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


class FakeTmuxBackend:
    called = False
    interrupted = False
    closed = False

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, session, prompt, **kwargs):
        type(self).called = True
        return ExecutionResult(
            text=f"tmux:{prompt}",
            backend="tmux",
            request_id="req",
            started_at=1.0,
            finished_at=2.0,
            diagnostics={"transcript_path": "/tmp/transcript.jsonl"},
        )

    def paths(self, session):
        return {
            "pane_log": "/tmp/pane.log",
            "state_path": "/tmp/cli-proxy-test-tmux-state.json",
        }

    @staticmethod
    def _read_state(_paths):
        return {}

    async def interrupt(self, session):
        type(self).interrupted = True
        return True

    async def close(self, session):
        type(self).closed = True


@pytest.mark.asyncio
async def test_run_prompt_uses_tmux_without_headless_fallback(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", FakeTmuxBackend)
    cfg = _cfg(tmp_path)
    cfg.tools["claude"].default_execution_backend = "tmux"
    manager = SessionManager(cfg)
    session = manager.create(1, "claude", str(tmp_path))

    async def _forbidden_headless(*args, **kwargs):
        raise AssertionError("headless must not be called for tmux backend")

    monkeypatch.setattr(session, "_run_headless", _forbidden_headless)

    output = await session.run_prompt("hello")

    assert output == "tmux:hello"
    assert FakeTmuxBackend.called is True
    assert session.resume_token is None
    assert session.last_cli_raw_stream_path == "/tmp/pane.log"
    assert session.last_cli_normalized_stream_path == "/tmp/transcript.jsonl"


@pytest.mark.asyncio
async def test_run_prompt_rejects_tmux_image_without_headless_fallback(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.tools["claude"].default_execution_backend = "tmux"
    manager = SessionManager(cfg)
    session = manager.create(1, "claude", str(tmp_path))

    async def _forbidden_headless(*args, **kwargs):
        raise AssertionError("headless must not be called for tmux images")

    monkeypatch.setattr(session, "_run_headless", _forbidden_headless)

    with pytest.raises(RuntimeError, match="does not support image"):
        await session.run_prompt("describe", image_path="/tmp/image.png")


def test_interrupt_routes_tmux_without_touching_headless_proc(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    FakeTmuxBackend.interrupted = False
    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", FakeTmuxBackend)
    manager = SessionManager(_cfg(tmp_path))
    session = manager.create(1, "claude", str(tmp_path))
    session._active_execution_backend = "tmux"

    session.interrupt()

    assert FakeTmuxBackend.interrupted is True


def test_close_does_not_touch_tmux_for_headless_only_session(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    class TrackingTmuxBackend(FakeTmuxBackend):
        def paths(self, session):
            return {
                "pane_log": str(tmp_path / "pane.log"),
                "state_path": str(tmp_path / "missing-state.json"),
            }

    TrackingTmuxBackend.closed = False
    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", TrackingTmuxBackend)
    manager = SessionManager(_cfg(tmp_path))
    session = manager.create(1, "claude", str(tmp_path))

    session.close()

    assert TrackingTmuxBackend.closed is False


@pytest.mark.asyncio
async def test_close_active_tmux_async_closes_active_cli_and_marks_runtime_inactive(tmp_path, monkeypatch):
    session = SessionManager(_cfg(tmp_path)).create(1, "claude", str(tmp_path))
    session._active_execution_backend = "tmux"
    closed_clis = []

    async def _close_tmux(cli_name, *, tool_override=None):
        assert tool_override is None
        closed_clis.append(cli_name)
        return True

    monkeypatch.setattr(session, "_close_tmux_for_cli_async", _close_tmux)

    closed = await session.close_active_tmux_async()

    assert closed is True
    assert closed_clis == ["claude"]
    assert session._active_execution_backend == "none"


def test_close_active_tmux_closes_active_cli_and_marks_runtime_inactive(tmp_path, monkeypatch):
    session = SessionManager(_cfg(tmp_path)).create(1, "claude", str(tmp_path))
    session._active_execution_backend = "tmux"
    closed_clis = []

    async def _close_tmux(cli_name, *, tool_override=None):
        assert tool_override is None
        closed_clis.append(cli_name)
        return True

    monkeypatch.setattr(session, "_close_tmux_for_cli_async", _close_tmux)

    closed = session.close_active_tmux()

    assert closed is True
    assert closed_clis == ["claude"]
    assert session._active_execution_backend == "none"


@pytest.mark.asyncio
async def test_close_active_tmux_sync_works_with_running_event_loop(tmp_path, monkeypatch):
    session = SessionManager(_cfg(tmp_path)).create(1, "claude", str(tmp_path))
    session._active_execution_backend = "tmux"
    closed_clis = []

    async def _close_tmux(cli_name, *, tool_override=None):
        assert tool_override is None
        closed_clis.append(cli_name)
        return True

    monkeypatch.setattr(session, "_close_tmux_for_cli_async", _close_tmux)

    closed = session.close_active_tmux()

    assert closed is True
    assert closed_clis == ["claude"]
    assert session._active_execution_backend == "none"


def test_cli_switch_closes_tmux_for_previous_cli_with_tmux_state(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    closed_clis = []

    class TrackingTmuxBackend(FakeTmuxBackend):
        def paths(self, session):
            cli_name = str(getattr(getattr(session, "cli", None), "active_cli", "") or "")
            state_path = tmp_path / f"{cli_name}-state.json"
            return {
                "pane_log": str(tmp_path / f"{cli_name}-pane.log"),
                "state_path": str(state_path),
            }

        async def close(self, session):
            closed_clis.append(str(getattr(getattr(session, "cli", None), "active_cli", "") or ""))

    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", TrackingTmuxBackend)
    cfg = _cfg(tmp_path)
    cfg.tools["codex"] = ToolConfig(name="codex", mode="headless", cmd=["codex"], interactive_cmd=["codex"])
    manager = SessionManager(cfg)
    session = manager.create(1, "claude", str(tmp_path))
    (tmp_path / "claude-state.json").write_text("{}", encoding="utf-8")
    session.set_active_cli("codex", close_previous_tmux=True)

    assert closed_clis == ["claude"]


def test_cli_switch_closes_tmux_only_for_current_bot_session(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    closed: list[tuple[str, str]] = []

    class TrackingTmuxBackend(FakeTmuxBackend):
        def paths(self, session):
            cli_name = str(getattr(getattr(session, "cli", None), "active_cli", "") or "")
            return {
                "pane_log": str(tmp_path / f"{session.id}-{cli_name}-pane.log"),
                "state_path": str(tmp_path / f"{session.id}-{cli_name}-state.json"),
            }

        async def close(self, session):
            cli_name = str(getattr(getattr(session, "cli", None), "active_cli", "") or "")
            closed.append((str(session.id), cli_name))

    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", TrackingTmuxBackend)
    cfg = _cfg(tmp_path)
    cfg.tools["codex"] = ToolConfig(name="codex", mode="headless", cmd=["codex"], interactive_cmd=["codex"])
    manager = SessionManager(cfg)
    current = manager.create(1, "claude", str(tmp_path))
    other = manager.create(1, "claude", str(tmp_path))
    current_state = tmp_path / f"{current.id}-claude-state.json"
    other_state = tmp_path / f"{other.id}-claude-state.json"
    current_state.write_text("{}", encoding="utf-8")
    other_state.write_text("{}", encoding="utf-8")

    current.set_active_cli("codex", close_previous_tmux=True)

    assert closed == [(current.id, "claude")]
    assert current.active_cli == "codex"
    assert other.active_cli == "claude"
    assert other_state.exists()


def test_temporary_cli_switch_does_not_close_tmux(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    class TrackingTmuxBackend(FakeTmuxBackend):
        def paths(self, session):
            return {
                "pane_log": str(tmp_path / "pane.log"),
                "state_path": str(tmp_path / "tmux-state.json"),
            }

    TrackingTmuxBackend.closed = False
    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", TrackingTmuxBackend)
    cfg = _cfg(tmp_path)
    cfg.tools["codex"] = ToolConfig(name="codex", mode="headless", cmd=["codex"], interactive_cmd=["codex"])
    session = SessionManager(cfg).create(1, "claude", str(tmp_path))
    (tmp_path / "tmux-state.json").write_text("{}", encoding="utf-8")

    session.set_active_cli("codex")

    assert TrackingTmuxBackend.closed is False


def test_persistent_cli_switch_closes_orphan_tmux_without_state_file(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    closed_clis: list[str] = []

    class TrackingTmuxBackend(FakeTmuxBackend):
        def paths(self, session):
            return {
                "pane_log": str(tmp_path / "pane.log"),
                "state_path": str(tmp_path / "missing-state.json"),
            }

        async def close(self, session):
            closed_clis.append(str(getattr(getattr(session, "cli", None), "active_cli", "") or ""))
            return True

    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", TrackingTmuxBackend)
    cfg = _cfg(tmp_path)
    cfg.tools["codex"] = ToolConfig(name="codex", mode="headless", cmd=["codex"], interactive_cmd=["codex"])
    session = SessionManager(cfg).create(1, "claude", str(tmp_path))

    session.set_active_cli("codex", close_previous_tmux=True)

    assert closed_clis == ["claude"]
    assert session.active_cli == "codex"


def test_persistent_cli_switch_failure_keeps_previous_cli(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    class FailingTmuxBackend(FakeTmuxBackend):
        async def close(self, session):
            raise RuntimeError("kill failed")

    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", FailingTmuxBackend)
    cfg = _cfg(tmp_path)
    cfg.tools["codex"] = ToolConfig(name="codex", mode="headless", cmd=["codex"], interactive_cmd=["codex"])
    session = SessionManager(cfg).create(1, "claude", str(tmp_path))

    with pytest.raises(RuntimeError, match="tmux close failed"):
        session.set_active_cli("codex", close_previous_tmux=True)

    assert session.active_cli == "claude"
    assert session.tool.name == "claude"


def test_headless_cli_switch_succeeds_without_tmux_binary(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.tools["codex"] = ToolConfig(name="codex", mode="headless", cmd=["codex"])
    session = SessionManager(cfg).create(1, "claude", str(tmp_path))
    monkeypatch.setattr(
        "app.services.cli_backends.tmux_driver.TmuxDriver.tmux_available",
        lambda: False,
    )

    session.set_active_cli("codex", close_previous_tmux=True)

    assert session.active_cli == "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_cli", "target_cli"),
    [
        ("claude", "codex"),
        ("codex", "grok"),
        ("grok", "claude"),
    ],
)
async def test_cli_switch_kills_only_previous_cli_tmux_for_current_session(
    tmp_path,
    monkeypatch,
    previous_cli,
    target_cli,
):
    from app.services.cli_backends.tmux_backend import TmuxExecutionBackend

    class TrackingDriver:
        live_sessions: set[str] = set()
        killed_sessions: list[str] = []

        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def tmux_available():
            return True

        async def kill_session(self, session_name):
            self.killed_sessions.append(session_name)
            if session_name not in self.live_sessions:
                return False
            self.live_sessions.remove(session_name)
            return True

        async def has_session(self, session_name):
            return session_name in self.live_sessions

    monkeypatch.setattr("app.services.cli_backends.tmux_driver.TmuxDriver", TrackingDriver)
    cfg = _cfg(tmp_path)
    for cli_name in ("codex", "grok"):
        cfg.tools[cli_name] = ToolConfig(
            name=cli_name,
            mode="headless",
            cmd=[cli_name],
            interactive_cmd=[cli_name],
            execution_backends=["headless", "tmux"],
        )
    manager = SessionManager(cfg)
    current = manager.create(1, previous_cli, str(tmp_path))
    other = manager.create(1, previous_cli, str(tmp_path))
    current_tmux = TmuxExecutionBackend().paths(current)["session_name"]
    other_tmux = TmuxExecutionBackend().paths(other)["session_name"]
    TrackingDriver.live_sessions = {current_tmux, other_tmux}
    TrackingDriver.killed_sessions = []

    await current.set_active_cli_persistent(target_cli)

    assert TrackingDriver.killed_sessions == [current_tmux]
    assert current_tmux not in TrackingDriver.live_sessions
    assert other_tmux in TrackingDriver.live_sessions
    assert current.active_cli == target_cli
    assert other.active_cli == previous_cli


@pytest.mark.asyncio
async def test_idle_cli_switch_holds_run_lock_until_tmux_is_closed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.tools["codex"] = ToolConfig(name="codex", mode="headless", cmd=["codex"])
    session = SessionManager(cfg).create(1, "claude", str(tmp_path))
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    prompt_started = asyncio.Event()
    observed_cli: list[str] = []

    async def _slow_close(cli_name, *, tool_override=None):
        assert cli_name == "claude"
        assert tool_override is None
        assert session.run_lock.locked()
        close_started.set()
        await allow_close.wait()
        return True

    monkeypatch.setattr(session, "_close_tmux_for_cli_async", _slow_close)

    async def _start_prompt():
        async with session.run_lock:
            observed_cli.append(session.active_cli)
            prompt_started.set()

    switch_task = asyncio.create_task(session.set_active_cli_persistent_when_idle("codex"))
    await close_started.wait()
    prompt_task = asyncio.create_task(_start_prompt())
    await asyncio.sleep(0)

    assert prompt_started.is_set() is False

    allow_close.set()
    await switch_task
    await prompt_task

    assert observed_cli == ["codex"]


def test_close_touches_stale_tmux_state_after_backend_switch_to_headless(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    closed_clis = []

    class TrackingTmuxBackend(FakeTmuxBackend):
        def paths(self, session):
            cli_name = str(getattr(getattr(session, "cli", None), "active_cli", "") or "")
            state_path = tmp_path / f"{cli_name}-state.json"
            return {
                "pane_log": str(tmp_path / f"{cli_name}-pane.log"),
                "state_path": str(state_path),
            }

        async def close(self, session):
            closed_clis.append(str(getattr(getattr(session, "cli", None), "active_cli", "") or ""))

    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", TrackingTmuxBackend)
    manager = SessionManager(_cfg(tmp_path))
    session = manager.create(1, "claude", str(tmp_path))
    (tmp_path / "claude-state.json").write_text("{}", encoding="utf-8")

    session.close()

    assert closed_clis == ["claude"]


def test_shutdown_close_preserves_existing_tmux_state(tmp_path, monkeypatch):
    import app.services.cli_backends as cli_backends

    class TrackingTmuxBackend(FakeTmuxBackend):
        def paths(self, session):
            return {
                "pane_log": str(tmp_path / "pane.log"),
                "state_path": str(tmp_path / "tmux-state.json"),
            }

    TrackingTmuxBackend.closed = False
    monkeypatch.setattr(cli_backends, "TmuxExecutionBackend", TrackingTmuxBackend)
    manager = SessionManager(_cfg(tmp_path))
    session = manager.create(1, "claude", str(tmp_path))
    (tmp_path / "tmux-state.json").write_text("{}", encoding="utf-8")

    session.close(preserve_tmux=True)

    assert TrackingTmuxBackend.closed is False
