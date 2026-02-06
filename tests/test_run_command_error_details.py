import asyncio


def test_execute_shell_command_includes_cmd_and_cwd_when_no_output(monkeypatch):
    from agent.tooling import helpers

    class _Completed:
        returncode = 1
        stdout = ""
        stderr = ""

    def _fake_run(*args, **kwargs):
        return _Completed()

    monkeypatch.setattr(helpers.subprocess, "run", _fake_run)

    res = asyncio.run(helpers.execute_shell_command("false", "/tmp"))
    assert res["success"] is False
    assert "no output" in (res.get("error") or "")
    assert "command=" in (res.get("error") or "")
    assert "cwd=" in (res.get("error") or "")
    assert res.get("meta", {}).get("returncode") == 1

