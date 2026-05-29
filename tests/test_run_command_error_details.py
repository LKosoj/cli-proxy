import asyncio
import time


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


def test_execute_shell_command_does_not_block_event_loop(monkeypatch):
    from agent.tooling import helpers

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(*args, **kwargs):
        time.sleep(0.2)
        return _Completed()

    monkeypatch.setattr(helpers.subprocess, "run", _fake_run)

    ticks = 0

    async def _ticker():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.05)
            ticks += 1

    async def _run():
        cmd_task = asyncio.create_task(helpers.execute_shell_command("echo ok", "/tmp"))
        ticker_task = asyncio.create_task(_ticker())
        await asyncio.gather(cmd_task, ticker_task)
        return cmd_task.result()

    res = asyncio.run(_run())
    assert res["success"] is True
    assert ticks >= 3
