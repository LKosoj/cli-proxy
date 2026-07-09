from __future__ import annotations

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

    async def run(self, session, prompt, **kwargs):
        type(self).called = True
        return ExecutionResult(
            text=f"tmux:{prompt}",
            backend="tmux",
            request_id="req",
            started_at=1.0,
            finished_at=2.0,
        )

    def paths(self, session):
        return {
            "pane_log": "/tmp/pane.log",
            "state_path": "/tmp/cli-proxy-test-tmux-state.json",
        }

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


def test_close_touches_tmux_for_previous_cli_with_tmux_state(tmp_path, monkeypatch):
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
    session.set_active_cli("codex")

    session.close()

    assert closed_clis == ["claude"]


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
