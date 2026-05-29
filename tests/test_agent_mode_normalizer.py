import pytest

from modes.agent.mode import AgentMode


def test_agent_extract_plugin_id_parses_pid_prefix() -> None:
    mode = AgentMode()
    pid = mode._extract_plugin_id({"value": "p=plug"})
    assert pid == "plug"


def test_agent_extract_plugin_context_parses_compact_session_and_plugin_payload() -> None:
    mode = AgentMode()
    payload = {"value": "s=s42|p=plug"}

    assert mode._extract_plugin_session_id(payload) == "s42"
    assert mode._extract_plugin_id(payload) == "plug"


def test_agent_extract_plugin_id_logs_on_structured_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mode = AgentMode()
    logged: list[str] = []

    def _fake_exception(msg, *args, **kwargs):  # noqa: ANN001, ARG001
        logged.append(str(msg))

    monkeypatch.setattr(mode._log, "exception", _fake_exception)
    pid = mode._extract_plugin_id({"p": 123})

    assert pid == ""
    assert any("agent plugin callback payload parse failed" in msg for msg in logged)
