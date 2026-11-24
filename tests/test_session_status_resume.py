from types import SimpleNamespace

import pytest

from i18n import t
from sessions.session_status import build_session_status_text
from tg.markdown import to_telegram_entities


@pytest.fixture
def status_session(monkeypatch):
    monkeypatch.setattr("session.has_live_tmux", lambda session: False)
    monkeypatch.setattr("sessions.session_status.load_ssh_config", lambda workdir: {})
    tool = SimpleNamespace(name="codex", mode="headless", env={"SECRET_TOKEN": "hidden"})
    return SimpleNamespace(
        id="s1", name=None, workdir="/work/my project", tool=tool, resume_token="cli-session-id",
    )


@pytest.mark.parametrize(
    ("cli", "backend", "tmux_user", "resume", "user"),
    [
        ("codex", "headless", "", "codex resume", ""),
        ("grok", "headless", "", "grok --resume", ""),
        ("claude", "headless", "", "claude --resume", "claude-bot"),
        ("claude", "tmux", "agent-user", "claude --resume", "agent-user"),
    ],
)
def test_status_includes_copyable_native_resume(status_session, cli, backend, tmux_user, resume, user):
    status_session.tool.name = cli
    status_session.tool.execution_backends = ["headless", "tmux"]
    status_session.tool.default_execution_backend = backend
    status_session.tool.tmux_user = tmux_user
    status_session.workdir = "/work/my 'project' `dir`"
    status_session.resume_token = "named session"

    status = build_session_status_text(status_session, lang="en")
    key = "session_status.resume_terminal_user" if user else "session_status.resume_terminal"
    assert t(key, "en", user=user) in status
    text, entities = to_telegram_entities(status)
    assert f"""cd -- '/work/my '"'"'project'"'"' `dir`' && {resume} 'named session'""" in text
    assert any(entity.type == "pre" for entity in entities)
    assert "SECRET_TOKEN" not in text
    assert "hidden" not in text


@pytest.mark.parametrize(
    "attrs",
    [
        {"resume_token": None},
        {"resume_token": "--last"},
        {"busy": True},
        {"active_mode": "agent"},
        {"advanced_orchestrator_enabled": True},
        {"ssh_remote_enabled": True},
        {"remote_control_enabled": True},
        {"tool": SimpleNamespace(name="qwen")},
        {"tool": SimpleNamespace(name="claude", no_session_persistence_on_fresh=True)},
        {"active_cli": "claude"},
    ],
)
def test_status_omits_native_resume_when_unavailable(status_session, attrs):
    for key, value in attrs.items():
        setattr(status_session, key, value)
    assert "```sh" not in build_session_status_text(status_session)


def test_status_prefers_existing_tmux_attach(status_session, monkeypatch):
    monkeypatch.setattr("session.has_live_tmux", lambda session: True)
    monkeypatch.setattr("app.services.cli_backends.tmux_backend.TmuxExecutionBackend._read_state", lambda paths: {})
    status = build_session_status_text(status_session)

    assert "Tmux attach: `tmux attach-session -r -t " in status
    assert "```sh" not in status
