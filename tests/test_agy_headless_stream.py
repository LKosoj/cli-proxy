import asyncio
import json
import sys
from pathlib import Path

import pytest

from config import load_config
from session import Session


@pytest.mark.asyncio
async def test_agy_headless_stream_uses_final_response_and_resumes(monkeypatch, tmp_path):
    config = load_config(str(Path(__file__).resolve().parents[1] / "config_example.yaml"))
    session = Session(
        id="s1", tool=config.tools["agy"], workdir=str(tmp_path),
        idle_timeout_sec=10, config=config,
    )
    conversation_id = "75e762e4-0b26-47bc-912f-3e3e18e4a083"
    records = [
        {"event": "init", "conversation_id": conversation_id, "init": {}},
        {"event": "step_update", "step_update": {
            "step_type": "agent_response", "state": "ACTIVE", "text_delta": "Two",
        }},
        {"event": "step_update", "step_update": {
            "step_type": "tool", "state": "DONE", "tool_name": "list_dir",
        }},
        {"event": "step_update", "step_update": {
            "step_type": "agent_response", "state": "DONE", "text_delta": " files.",
        }},
        {"event": "result", "result": {
            "conversation_id": conversation_id, "status": "SUCCESS", "response": "Two files.",
        }},
    ]
    create_subprocess = asyncio.create_subprocess_exec
    commands = []
    received_prompt = tmp_path / "received.json"

    async def fake_agy(*command, **kwargs):
        commands.append(command)
        if "--input-format" not in command:
            stream = "\n".join(map(json.dumps, records))
            return await create_subprocess(sys.executable, "-c", "import sys; print(sys.argv[1])", stream, **kwargs)
        script = (
            "import sys; from pathlib import Path; "
            "print(sys.argv[1], flush=True); "
            "Path(sys.argv[3]).write_text(sys.stdin.readline()); "
            "print(sys.argv[2])"
        )
        return await create_subprocess(
            sys.executable, "-c", script, json.dumps(records[0]),
            json.dumps(records[-1], ensure_ascii=False), str(received_prompt), **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_agy)

    assert await session._run_headless("-first prompt") == "Two files."
    assert session.resume_token == conversation_id
    assert commands[0] == ("agy", "--output-format", "stream-json", "--print=-first prompt")
    assert session.last_assistant_text_value == "Two files."

    records[-1] = {"padding": "", "event": "result", "result": {
        "conversation_id": conversation_id, "status": "ERROR", "response": "",
        "error": "Нет действующей лицензии. (#3501)",
    }}
    wire_result = json.dumps(records[-1], ensure_ascii=False).encode()
    records[-1]["padding"] = " " * (4095 - wire_result.index("Н".encode()))
    assert await session._run_headless("follow-up") == "Нет действующей лицензии. (#3501)"
    assert commands[1] == (
        "agy", "--output-format", "stream-json", "--input-format", "stream-json", "--conversation", conversation_id,
    )
    assert json.loads(received_prompt.read_text()) == {"event": "user", "message": {"content": "follow-up"}}

    received_prompt.unlink()
    records[0] = {"event": "init", "conversation_id": "unexpected", "init": {}}
    assert await session._run_headless("follow-up") == f"Antigravity could not resume conversation {conversation_id}."
    assert session.resume_token == conversation_id
    assert not received_prompt.exists()
