import asyncio
import json
import logging
import os
import subprocess
import uuid
import pytest

from app.services.cli_json_stream import recover_cli_text_from_raw_stream
from app.services.session_tick_history_store import load_session_ticks
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProc:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.pid = 424242
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream(stderr_chunks)

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = 0
        return 0

    async def communicate(self):
        out = bytearray()
        err = bytearray()
        while True:
            chunk = await self.stdout.read(4096)
            if not chunk:
                break
            out.extend(chunk)
        while True:
            chunk = await self.stderr.read(4096)
            if not chunk:
                break
            err.extend(chunk)
        self.returncode = 0
        return bytes(out), bytes(err)


class _FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


class _NeverEndingStream:
    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(10)
        return b""


class _NeverEndingProc:
    def __init__(self):
        self.pid = 525252
        self.returncode = None
        self.stdin = None
        self.stdout = _NeverEndingStream()
        self.stderr = _NeverEndingStream()

    async def wait(self) -> int:
        await asyncio.sleep(10)
        self.returncode = -15
        return self.returncode


def test_headless_uses_devnull_unless_prompt_is_sent_via_stdin(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="dummy",
            mode="headless",
            cmd=["dummy", "{prompt}"],
            headless_cmd=["dummy", "{prompt}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"dummy": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        captured_stdin = []

        async def _fake_create_subprocess_exec(*_args, **kwargs):
            captured_stdin.append(kwargs.get("stdin"))
            return _FakeProc(stdout_chunks=[b"answer\n"], stderr_chunks=[])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")

        assert out == "answer\n"
        assert captured_stdin == [subprocess.DEVNULL]

        stdin = _FakeStdin()
        stdin_tool = ToolConfig(
            name="dummy",
            mode="headless",
            cmd=["dummy"],
            headless_cmd=["dummy"],
        )
        stdin_cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"dummy": stdin_tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config-stdin.yaml"),
        )
        stdin_session = Session(
            id="s2",
            tool=stdin_tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=stdin_cfg,
        )

        async def _fake_create_stdin_subprocess_exec(*_args, **kwargs):
            captured_stdin.append(kwargs.get("stdin"))
            proc = _FakeProc(stdout_chunks=[b"stdin-answer\n"], stderr_chunks=[])
            proc.stdin = stdin
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_stdin_subprocess_exec)

        out = await stdin_session._run_headless("hello")

        assert out == "stdin-answer\n"
        assert captured_stdin[-1] == asyncio.subprocess.PIPE
        assert stdin.writes == [b"hello\n"]
        assert stdin.closed is True

    asyncio.run(_run())


class _BlockingUntilReturncodeStream:
    def __init__(self, proc, chunks):
        self._proc = proc
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        if self._proc.returncode is not None:
            return b""
        await asyncio.Future()
        raise AssertionError("unreachable")


class _FakeSemanticCodexProc:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.pid = 626262
        self.returncode = None
        self.stdin = None
        self.stdout = _BlockingUntilReturncodeStream(self, stdout_chunks)
        self.stderr = _BlockingUntilReturncodeStream(self, stderr_chunks)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return int(self.returncode)


class _FakeSemanticGeminiProc:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.pid = 636363
        self.returncode = None
        self.stdin = None
        self.stdout = _BlockingUntilReturncodeStream(self, stdout_chunks)
        self.stderr = _BlockingUntilReturncodeStream(self, stderr_chunks)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return int(self.returncode)


class _FakeSemanticQwenProc:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.pid = 646464
        self.returncode = None
        self.stdin = None
        self.stdout = _BlockingUntilReturncodeStream(self, stdout_chunks)
        self.stderr = _BlockingUntilReturncodeStream(self, stderr_chunks)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return int(self.returncode)


class _FakeSemanticClaudeProc:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.pid = 656565
        self.returncode = None
        self.stdin = None
        self.stdout = _BlockingUntilReturncodeStream(self, stdout_chunks)
        self.stderr = _BlockingUntilReturncodeStream(self, stderr_chunks)

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return int(self.returncode)


def test_recover_cli_text_from_raw_stream_for_qwen_result_payload() -> None:
    raw = "\n".join(
        [
            '{"type":"system","subtype":"init","session_id":"s-qwen"}',
            '{"type":"result","result":"Recovered final answer","is_error":false}',
        ]
    )
    assert recover_cli_text_from_raw_stream("qwen", raw) == "Recovered final answer"


def test_headless_qwen_json_stream_sets_last_normalized_stream_path(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--continue", "--prompt", "{prompt}"],
            headless_cmd=["qwen", "--continue", "--prompt", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"qwen": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path), cli_json_stream_archive_enabled=True),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[
                    (
                        '{"type":"system","subtype":"init","session_id":"s-qwen"}\n'
                        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"call-1",'
                        '"name":"read_file","input":{"absolute_path":"'
                        + str(tmp_path / "views" / "header.blade.php").replace("\\", "/")
                        + '"}},{"type":"text","text":"OK"}]}}\n'
                        '{"type":"result","result":"OK","is_error":false}\n'
                    ).encode()
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "OK"
        assert session.last_cli_normalized_stream_path
        assert os.path.isfile(session.last_cli_normalized_stream_path)

    asyncio.run(_run())


def test_headless_resume_token_detected_from_stderr(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            resume_regex=r"\"thread_id\"\s*:\s*\"([^\"]+)\"",
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[b"result\n"],
                stderr_chunks=[b'{"thread_id":"019c353d-5d3d-7441-9178-da0630800212"}\n'],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert "result" in out
        assert session.resume_token == "019c353d-5d3d-7441-9178-da0630800212"

    asyncio.run(_run())


def test_headless_codex_suppresses_transient_apply_patch_stderr(monkeypatch, tmp_path, caplog):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        stderr_payload = (
            "2026-04-13T13:09:35.576633Z ERROR codex_core::tools::router: "
            "error=apply_patch verification failed: Failed to find expected lines:\n"
            "        self._run_dir.update_phase(\"gate1\")\n"
            "\n"
            "        answers: list[str] = []\n"
        ).encode("utf-8")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[b"answer\n"],
                stderr_chunks=[stderr_payload],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        caplog.set_level(logging.DEBUG, logger="session.headless")

        out = await session._run_headless("hello")

        assert out == "answer\n"
        info_messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "session.headless" and record.levelno == logging.INFO
        ]
        debug_messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "session.headless" and record.levelno == logging.DEBUG
        ]
        assert not any("apply_patch verification failed" in message for message in info_messages)
        assert any("suppressed transient stderr from codex" in message for message in debug_messages)

    asyncio.run(_run())


def test_headless_codex_keeps_non_transient_stderr_after_router_noise(monkeypatch, tmp_path, caplog):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        stderr_payload = (
            "2026-04-13T13:09:35.576633Z ERROR codex_core::tools::router: "
            "error=apply_patch verification failed: Failed to find expected lines:\n"
            "        self._run_dir.update_phase(\"gate1\")\n"
            "service lines only\n"
        ).encode("utf-8")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[b"answer\n"],
                stderr_chunks=[stderr_payload],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        caplog.set_level(logging.INFO, logger="session.headless")

        out = await session._run_headless("hello")

        assert out == "answer\n"
        info_messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "session.headless" and record.levelno == logging.INFO
        ]
        stderr_logs = [message for message in info_messages if message.startswith("stderr (")]
        assert len(stderr_logs) == 1
        assert "service lines only" in stderr_logs[0]
        assert "apply_patch verification failed" not in stderr_logs[0]
        assert "transient lines suppressed" in stderr_logs[0]

    asyncio.run(_run())


def test_headless_codex_json_stream_uses_semantic_completion(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticCodexProc(
            stdout_chunks=[
                b'{"type":"thread.started","thread_id":"019d17d2-cf09-7922-8f0e-a1ac4806593f"}\n',
                b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}\n',
                b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            assert "--json" in list(args)
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless("hello")

        assert out == "OK"
        assert session.resume_token == "019d17d2-cf09-7922-8f0e-a1ac4806593f"
        assert killpg_calls == [(proc.pid, 15)]
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert ticks[-1] == "OK"

    asyncio.run(_run())


def test_headless_codex_semantic_completion_accepts_valid_structured_bundle_without_turn_completed(
    monkeypatch,
    tmp_path,
):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        bundle = json.dumps(
            {
                "final_text": "Исправленный документ",
                "closed_obligations": ["repo_step:use_cli_repo_grounding"],
                "remaining_obligations": [],
                "corrections_applied": ["Убрано неподтвержденное утверждение."],
                "claims": [],
                "evidence": [],
                "degraded_modes": [],
            },
            ensure_ascii=False,
        )
        proc = _FakeSemanticCodexProc(
            stdout_chunks=[
                b'{"type":"thread.started","thread_id":"019d17d2-cf09-7922-8f0e-a1ac4806593f"}\n',
                (json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message", "text": bundle},
                    },
                    ensure_ascii=False,
                ) + "\n").encode("utf-8"),
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            assert "--json" in list(args)
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless(
            "CLI_RESPONSE_FORMAT: spec_fix_bundle_json\nВерни structured bundle",
        )

        assert out == bundle
        assert session.resume_token == "019d17d2-cf09-7922-8f0e-a1ac4806593f"
        assert killpg_calls == [(proc.pid, 15)]

    asyncio.run(_run())


def test_headless_codex_semantic_completion_accepts_split_structured_bundle_without_turn_completed(
    monkeypatch,
    tmp_path,
):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        bundle = json.dumps(
            {
                "final_text": "Исправленный документ",
                "closed_obligations": ["repo_step:use_cli_repo_grounding"],
                "remaining_obligations": [],
                "corrections_applied": ["Убрано неподтвержденное утверждение."],
                "claims": [],
                "evidence": [],
                "degraded_modes": [],
            },
            ensure_ascii=False,
        )
        midpoint = len(bundle) // 2
        first_half = bundle[:midpoint]
        second_half = bundle[midpoint:]
        proc = _FakeSemanticCodexProc(
            stdout_chunks=[
                b'{"type":"thread.started","thread_id":"019d17d2-cf09-7922-8f0e-a1ac4806593f"}\n',
                (
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"id": "item_0", "type": "agent_message", "text": first_half},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8"),
                (
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"id": "item_1", "type": "agent_message", "text": second_half},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8"),
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            assert "--json" in list(args)
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless(
            "CLI_RESPONSE_FORMAT: spec_fix_bundle_json\nВерни structured bundle",
        )

        assert out == bundle
        assert session.resume_token == "019d17d2-cf09-7922-8f0e-a1ac4806593f"
        assert killpg_calls == [(proc.pid, 15)]

    asyncio.run(_run())


def test_headless_codex_streamed_assistant_text_is_visible_in_ticks(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticCodexProc(
            stdout_chunks=[
                b'{"type":"thread.started","thread_id":"019d17d2-cf09-7922-8f0e-a1ac4806593f"}\n',
                (
                    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
                    '"text":"Сначала проверю код"}}\n'
                ).encode("utf-8"),
                (
                    '{"type":"item.started","item":{"id":"item_1","type":"command_execution",'
                    '"command":"bash -lc \\"ls -la\\"","aggregated_output":"","exit_code":null,'
                    '"status":"in_progress"}}\n'
                ).encode("utf-8"),
                (
                    '{"type":"item.completed","item":{"id":"item_1","type":"command_execution",'
                    '"command":"bash -lc \\"ls -la\\"","aggregated_output":"Done","exit_code":0,'
                    '"status":"completed"}}\n'
                ).encode("utf-8"),
                (
                    '{"type":"item.completed","item":{"id":"item_2","type":"agent_message",'
                    '"text":"Исправление готово"}}\n'
                ).encode("utf-8"),
                b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            assert "--json" in list(args)
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless("hello")

        assert out == "Исправление готово"
        assert killpg_calls == [(proc.pid, 15)]
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert ticks == [
            "Сначала проверю код",
            'command_execution: bash -lc "ls -la"',
            "Исправление готово",
        ]

    asyncio.run(_run())


def test_headless_codex_ignores_time_only_assistant_text(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticCodexProc(
            stdout_chunks=[
                b'{"type":"thread.started","thread_id":"019d17d2-cf09-7922-8f0e-a1ac4806593f"}\n',
                (
                    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
                    '"text":"Полезный ответ"}}\n'
                ).encode("utf-8"),
                (
                    '{"type":"item.completed","item":{"id":"item_1","type":"agent_message",'
                    '"text":"04:58:45"}}\n'
                ).encode("utf-8"),
                b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            assert "--json" in list(args)
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless("hello")

        assert out == "Полезный ответ"
        assert killpg_calls == [(proc.pid, 15)]
        assert session.last_assistant_text_value == "Полезный ответ"
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert ticks == ["Полезный ответ"]

    asyncio.run(_run())


def test_headless_codex_json_stream_archive_writes_raw_and_normalized_files(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                cli_json_stream_archive_enabled=True,
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[
                    b'{"type":"thread.started","thread_id":"019d17d2-cf09-7922-8f0e-a1ac4806593f"}\n',
                    b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}\n',
                    b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")

        assert out == "OK"
        archive_root = tmp_path / ".cli-proxy" / "cli-json-stream" / "codex"
        raw_files = list(archive_root.rglob("*.raw.jsonl"))
        normalized_files = list(archive_root.rglob("*.normalized.jsonl"))
        assert len(raw_files) == 1
        assert len(normalized_files) == 1
        assert '"thread.started"' in raw_files[0].read_text(encoding="utf-8")
        assert '"kind": "completed"' in normalized_files[0].read_text(encoding="utf-8")

    asyncio.run(_run())


def test_headless_claude_resume_token_detected_from_stream(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "-p", "{prompt}"],
            headless_cmd=["claude", "-p", "{prompt}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[
                    b'{"type":"system","subtype":"init","session_id":"claude-stream-session"}\n',
                    (
                        b'{"type":"assistant","message":{"content":[{"type":"text","text":"result"}]},'
                        b'"session_id":"claude-stream-session"}\n'
                    ),
                    (
                        b'{"type":"result","subtype":"success","is_error":false,"result":"result",'
                        b'"session_id":"claude-stream-session"}\n'
                    ),
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "result"
        assert session.resume_token == "claude-stream-session"

    asyncio.run(_run())


def test_headless_claude_uses_explicit_session_id(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            calls.append(list(args))
            return _FakeProc(
                stdout_chunks=[
                    b'{"type":"system","subtype":"init","session_id":"11111111-1111-4111-8111-111111111111"}\n',
                    (
                        b'{"type":"assistant","message":{"content":[{"type":"text","text":"result"}]},'
                        b'"session_id":"11111111-1111-4111-8111-111111111111"}\n'
                    ),
                    (
                        b'{"type":"result","subtype":"success","is_error":false,"result":"result",'
                        b'"session_id":"11111111-1111-4111-8111-111111111111"}\n'
                    ),
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID("11111111-1111-4111-8111-111111111111"))

        out = await session._run_headless("hello")
        assert out == "result"
        assert session.resume_token == "11111111-1111-4111-8111-111111111111"
        assert calls
        joined = " ".join(str(x) for x in calls[0])
        assert "--verbose" in joined
        assert "--output-format stream-json" in joined
        assert "--session-id 11111111-1111-4111-8111-111111111111" in joined
        assert "--resume 11111111-1111-4111-8111-111111111111" not in joined
        assert "--continue" not in joined

    asyncio.run(_run())


def test_headless_claude_dash_prefixed_prompt_is_sent_via_stdin(monkeypatch, tmp_path):
    async def _run() -> None:
        prompt = "- starts with dash"
        resume_token = "22222222-2222-4222-8222-222222222222"
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "-p", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["claude", "-p", "{prompt}", "--resume", "{resume}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        session.resume_token = resume_token
        stdin = _FakeStdin()
        calls = []
        captured_stdin = []

        async def _fake_create_subprocess_exec(*args, **kwargs):
            calls.append(list(args))
            captured_stdin.append(kwargs.get("stdin"))
            proc = _FakeProc(
                stdout_chunks=[
                    (
                        b'{"type":"assistant","message":{"content":[{"type":"text","text":"OK"}]},'
                        b'"session_id":"22222222-2222-4222-8222-222222222222"}\n'
                    ),
                    (
                        b'{"type":"result","subtype":"success","is_error":false,"result":"OK",'
                        b'"session_id":"22222222-2222-4222-8222-222222222222"}\n'
                    ),
                ],
                stderr_chunks=[],
            )
            proc.stdin = stdin
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless(prompt)

        assert out == "OK"
        assert calls
        assert calls[0][:4] == ["su", "-", "claude-bot", "-c"]
        full_cmd = str(calls[0][-1])
        assert prompt not in full_cmd
        assert "-p" in full_cmd
        assert f"--resume {resume_token}" in full_cmd
        assert captured_stdin == [asyncio.subprocess.PIPE]
        assert stdin.writes == [b"- starts with dash\n"]
        assert stdin.closed is True

    asyncio.run(_run())


def test_headless_claude_json_stream_uses_semantic_completion(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticClaudeProc(
            stdout_chunks=[
                b'{"type":"system","subtype":"init","session_id":"33333333-3333-4333-8333-333333333333"}\n',
                (
                    b'{"type":"assistant","message":{"content":['
                    b'{"type":"tool_use","id":"tool-1","name":"Bash","input":{"command":"ls -la","description":"List files"}},'
                    b'{"type":"text","text":"OK"}'
                    b']},'
                    b'"session_id":"33333333-3333-4333-8333-333333333333"}\n'
                ),
                (
                    b'{"type":"system","subtype":"task_progress","description":"Finding **/*.py",'
                    b'"session_id":"33333333-3333-4333-8333-333333333333"}\n'
                ),
                (
                    b'{"type":"user","message":{"content":['
                    b'{"type":"tool_result","tool_use_id":"tool-1","is_error":false,"content":"Done"}'
                    b']},'
                    b'"session_id":"33333333-3333-4333-8333-333333333333"}\n'
                ),
                b'{"type":"rate_limit_event","session_id":"33333333-3333-4333-8333-333333333333"}\n',
                (
                    b'{"type":"result","subtype":"success","is_error":false,"result":"OK",'
                    b'"session_id":"33333333-3333-4333-8333-333333333333"}\n'
                ),
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            cmd = list(args)
            assert cmd[:4] == ["su", "-", "claude-bot", "-c"]
            joined = str(cmd[-1])
            assert "claude" in joined
            assert "--verbose" in joined
            assert "--output-format stream-json" in joined
            assert "--session-id 33333333-3333-4333-8333-333333333333" in joined
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID("33333333-3333-4333-8333-333333333333"))

        out = await session._run_headless("hello")

        assert out == "OK"
        assert session.resume_token == "33333333-3333-4333-8333-333333333333"
        assert proc.returncode == -15
        assert killpg_calls == [(proc.pid, 15)]
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert any("Bash: ls -la" in item for item in ticks)
        assert any("task_progress: Finding **/*.py" in item for item in ticks)
        assert any("Bash: ls -la result: Done" in item for item in ticks)
        assert ticks[-1] == "OK"

    asyncio.run(_run())


def test_headless_claude_no_session_persistence_flag_opt_in(monkeypatch, tmp_path):
    """Когда tools.claude.no_session_persistence_on_fresh=True — флаг должен
    появиться в fresh-команде."""

    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
            no_session_persistence_on_fresh=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            calls.append(list(args))
            return _FakeProc(
                stdout_chunks=[
                    b'{"type":"system","subtype":"init","session_id":"44444444-4444-4444-8444-444444444444"}\n',
                    (
                        b'{"type":"result","subtype":"success","is_error":false,"result":"OK",'
                        b'"session_id":"44444444-4444-4444-8444-444444444444"}\n'
                    ),
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID("44444444-4444-4444-8444-444444444444"))

        out = await session._run_headless("hello")
        assert out == "OK"
        assert calls
        joined = " ".join(str(x) for x in calls[0])
        assert "--no-session-persistence" in joined

    asyncio.run(_run())


def test_headless_claude_no_session_persistence_default_off(monkeypatch, tmp_path):
    """По умолчанию (флаг не выставлен) `--no-session-persistence` НЕ должен
    добавляться даже для fresh-старта. Иначе сломается session_transfer."""

    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            calls.append(list(args))
            return _FakeProc(
                stdout_chunks=[
                    b'{"type":"system","subtype":"init","session_id":"55555555-5555-4555-8555-555555555555"}\n',
                    (
                        b'{"type":"result","subtype":"success","is_error":false,"result":"OK",'
                        b'"session_id":"55555555-5555-4555-8555-555555555555"}\n'
                    ),
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID("55555555-5555-4555-8555-555555555555"))

        out = await session._run_headless("hello")
        assert out == "OK"
        assert calls
        joined = " ".join(str(x) for x in calls[0])
        assert "--no-session-persistence" not in joined
        # Sanity: но --session-id всё равно должен быть проставлен (fresh).
        assert "--session-id 55555555-5555-4555-8555-555555555555" in joined

    asyncio.run(_run())


def test_headless_claude_no_session_persistence_not_added_on_resume(monkeypatch, tmp_path):
    """Даже когда флаг включён, при resume `--no-session-persistence` НЕ должен
    добавляться: `claude --help` явно говорит, что такие сессии «cannot be
    resumed»."""

    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "-p", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["claude", "-p", "{prompt}", "--resume", "{resume}"],
            no_session_persistence_on_fresh=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        session.resume_token = "66666666-6666-4666-8666-666666666666"

        calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            calls.append(list(args))
            return _FakeProc(
                stdout_chunks=[
                    (
                        b'{"type":"result","subtype":"success","is_error":false,"result":"OK",'
                        b'"session_id":"66666666-6666-4666-8666-666666666666"}\n'
                    ),
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "OK"
        assert calls
        joined = " ".join(str(x) for x in calls[0])
        assert "--no-session-persistence" not in joined
        assert "--resume 66666666-6666-4666-8666-666666666666" in joined

    asyncio.run(_run())


def test_headless_claude_does_not_persist_failed_fresh_session(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        class _FakeFailedProc(_FakeProc):
            async def wait(self) -> int:
                await asyncio.sleep(0)
                self.returncode = 1
                return 1

            async def communicate(self):
                out, err = await super().communicate()
                self.returncode = 1
                return out, err

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeFailedProc(
                stdout_chunks=[b"Error: No conversation found with session ID\n"],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID("22222222-2222-4222-8222-222222222222"))

        out = await session._run_headless("hello")
        assert "No conversation found" in out
        assert session.resume_token is None

    asyncio.run(_run())


def test_headless_cancel_cleans_pending_tasks(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _NeverEndingProc()

        killpg_calls = []

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        task = asyncio.create_task(session._run_headless("hello"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session.current_proc is None
        assert session._headless_interrupt_flag is False
        assert len(killpg_calls) >= 1

    asyncio.run(_run())


def test_headless_gemini_token_not_extracted_from_stdout_without_list_sessions(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            cmd = list(args)
            if cmd[:2] == ["gemini", "--list-sessions"]:
                return _FakeProc(stdout_chunks=[b"Available sessions for this project (0):\n"], stderr_chunks=[])
            return _FakeProc(
                stdout_chunks=[
                    b"To resume this session, run: gemini --resume abcDEF123-token -p \"...\"\n",
                    b"result\n",
                ],
                stderr_chunks=[],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert "result" in out
        assert session.resume_token is None

    asyncio.run(_run())


def test_headless_gemini_uses_saved_resume_token(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        session.resume_token = "saved-token-42"

        captured_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            captured_calls.append(list(args))
            return _FakeProc(stdout_chunks=[b"ok\n"], stderr_chunks=[])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert "ok" in out
        args = captured_calls[0]
        assert "--output-format" in args
        assert args[args.index("--output-format") + 1] == "stream-json"
        assert "--resume" in args
        resume_idx = args.index("--resume")
        assert args[resume_idx + 1] == "saved-token-42"
        assert len(captured_calls) == 1

    asyncio.run(_run())


def test_headless_gemini_json_stream_uses_semantic_completion(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "{resume}", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "{resume}", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticGeminiProc(
            stdout_chunks=[
                b'{"type":"init","session_id":"41d92a9a-c235-4aef-afb2-5bc8f5e8bf03","model":"auto-gemini-3"}\n',
                b'{"type":"tool_use","tool_name":"read_file","tool_id":"tool-1","parameters":{"file_path":"README.md"}}\n',
                b'{"type":"tool_result","tool_id":"tool-1","status":"success","output":"Read lines 1-10"}\n',
                b'{"type":"message","role":"assistant","content":"OK","delta":true}\n',
                b'{"type":"result","status":"success"}\n',
            ],
            stderr_chunks=[
                b"YOLO mode is enabled. All tool calls will be automatically approved.\n",
            ],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            cmd = list(args)
            assert "--output-format" in cmd
            assert cmd[cmd.index("--output-format") + 1] == "stream-json"
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless("hello")

        assert out == "OK"
        assert session.resume_token == "41d92a9a-c235-4aef-afb2-5bc8f5e8bf03"
        assert killpg_calls == [(proc.pid, 15)]
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert any("read_file: README.md" in item for item in ticks)
        assert any("read_file: README.md result: Read lines 1-10" in item for item in ticks)
        assert ticks[-1] == "OK"

    asyncio.run(_run())


def test_headless_gemini_streamed_assistant_text_updates_ticks_without_final_duplicate(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "-p", "{prompt}"],
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticGeminiProc(
            stdout_chunks=[
                b'{"type":"init","session_id":"41d92a9a-c235-4aef-afb2-5bc8f5e8bf03","model":"auto-gemini-3"}\n',
                '{"type":"message","role":"assistant","content":"Я проведу анализ проекта","delta":true}\n'.encode("utf-8"),
                '{"type":"message","role":"assistant","content":" и соберу краткую сводку","delta":true}\n'.encode("utf-8"),
                b'{"type":"result","status":"success"}\n',
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            cmd = list(args)
            assert "--output-format" in cmd
            assert cmd[cmd.index("--output-format") + 1] == "stream-json"
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless("hello")

        assert out == "Я проведу анализ проекта и соберу краткую сводку"
        assert killpg_calls == [(proc.pid, 15)]
        assert session.tick_seen == 1
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert ticks == ["Я проведу анализ проекта и соберу краткую сводку"]

    asyncio.run(_run())


def test_headless_claude_single_assistant_text_is_visible_in_ticks(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "-p", "{prompt}"],
            headless_cmd=["claude", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticClaudeProc(
            stdout_chunks=[
                b'{"type":"system","subtype":"init","session_id":"claude-stream-session"}\n',
                (
                    '{"type":"assistant","message":{"content":[{"type":"text","text":"Ответ готов"}]},'
                    '"session_id":"claude-stream-session"}\n'
                ).encode("utf-8"),
                (
                    '{"type":"result","subtype":"success","is_error":false,"result":"Ответ готов",'
                    '"session_id":"claude-stream-session"}\n'
                ).encode("utf-8"),
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            cmd = list(args)
            shell_cmd = " ".join(str(part) for part in cmd)
            assert "--output-format stream-json" in shell_cmd
            assert "--verbose" in shell_cmd
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless("hello")

        assert out == "Ответ готов"
        assert proc.returncode == -15
        assert killpg_calls == [(proc.pid, 15)]
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert ticks == ["Ответ готов"]

    asyncio.run(_run())


def test_headless_gemini_recovers_resume_from_list_sessions(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        calls = {"n": 0}

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            calls["n"] += 1
            cmd = list(args)
            if cmd[:2] == ["gemini", "--list-sessions"]:
                return _FakeProc(
                    stdout_chunks=[
                        b"Available sessions for this project (3):\n",
                        b"  1. old (58 minutes ago) [c2a14d8e-0363-4acf-8045-01b641c67e65]\n",
                        b"  2. ? (5 minutes ago) [d7f26e70-c576-43c0-864d-543e93bf33c4]\n",
                        b"  3. ? (Just now) [94cd1877-4c06-4224-bf03-02195ecc3eef]\n",
                    ],
                    stderr_chunks=[],
                )
            return _FakeProc(stdout_chunks=[b"answer\n"], stderr_chunks=[b"service lines only\n"])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "answer\n"
        assert calls["n"] == 2
        assert session.resume_token == "94cd1877-4c06-4224-bf03-02195ecc3eef"

    asyncio.run(_run())


def test_headless_qwen_recovers_resume_from_latest_chat_file(monkeypatch, tmp_path):
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"qwen": tool},
            defaults=DefaultsConfig(workdir=str(workdir)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=cfg,
        )

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        key = session._qwen_project_key_candidates()[0]
        chats = home / ".qwen" / "projects" / key / "chats"
        chats.mkdir(parents=True)
        old_file = chats / "11111111-1111-1111-1111-111111111111.jsonl"
        new_file = chats / "22222222-2222-2222-2222-222222222222.jsonl"
        old_file.write_text("old\n", encoding="utf-8")
        new_file.write_text("new\n", encoding="utf-8")
        os.utime(old_file, (1000, 1000))
        os.utime(new_file, (2000, 2000))

        captured_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            captured_calls.append(list(args))
            return _FakeProc(stdout_chunks=[b"answer\n"], stderr_chunks=[])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "answer\n"
        assert session.resume_token == "22222222-2222-2222-2222-222222222222"
        args = captured_calls[0]
        assert "--output-format" in args
        assert args[args.index("--output-format") + 1] == "stream-json"

    asyncio.run(_run())


def test_headless_qwen_first_run_drops_continue_without_resume_token(monkeypatch, tmp_path):
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"qwen": tool},
            defaults=DefaultsConfig(workdir=str(workdir)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=cfg,
        )

        captured_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            captured_calls.append(list(args))
            return _FakeProc(stdout_chunks=[b"answer\n"], stderr_chunks=[])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "answer\n"
        args = captured_calls[0]
        assert "--continue" not in args
        assert "--resume" not in args

    asyncio.run(_run())


def test_headless_qwen_json_stream_uses_semantic_completion(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"qwen": tool},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        proc = _FakeSemanticQwenProc(
            stdout_chunks=[
                b'{"type":"system","subtype":"init","session_id":"3e327768-c278-4ae0-87e6-1af843f3e290"}\n',
                (
                    b'{"type":"assistant","message":{"content":[{"type":"thinking",'
                    b'"thinking":"plan next file read"}]}}\n'
                ),
                (
                    b'{"type":"assistant","message":{"content":['
                    b'{"type":"tool_use","id":"call-1","name":"read_file","input":{'
                    b'"absolute_path":"/srv/git_projects/cli-proxy/README.md"}},'
                    b'{"type":"text","text":"OK"}'
                    b']}}\n'
                ),
                (
                    b'{"type":"user","message":{"content":['
                    b'{"type":"tool_result","tool_use_id":"call-1","is_error":false,"content":"Read lines 1-10"}'
                    b']}}\n'
                ),
                b'{"type":"result","subtype":"success","is_error":false,"result":"OK"}\n',
            ],
            stderr_chunks=[],
        )
        killpg_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            cmd = list(args)
            assert "--output-format" in cmd
            assert cmd[cmd.index("--output-format") + 1] == "stream-json"
            return proc

        def _fake_killpg(pid: int, sig: int) -> None:
            killpg_calls.append((pid, sig))
            proc.returncode = -15

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(os, "killpg", _fake_killpg)

        out = await session._run_headless("hello")

        assert out == "OK"
        assert session.resume_token == "3e327768-c278-4ae0-87e6-1af843f3e290"
        assert killpg_calls == [(proc.pid, 15)]
        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert any("plan next file read" in item for item in ticks)
        assert any("read_file: README.md" in item for item in ticks)
        assert any("read_file: README.md result: Read lines 1-10" in item for item in ticks)
        assert ticks[-1] == "OK"

    asyncio.run(_run())


def test_headless_qwen_does_not_start_monitor_when_spawn_fails(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--prompt", "{prompt}"],
            headless_cmd=["qwen", "--prompt", "{prompt}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"qwen": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        marks: list[str] = []

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            raise RuntimeError("spawn failed")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(session, "_start_qwen_monitor", lambda: marks.append("start"))
        monkeypatch.setattr(session, "_stop_qwen_monitor", lambda: marks.append("stop"))

        with pytest.raises(RuntimeError, match="spawn failed"):
            await session._run_headless("hello")

        assert marks == []
        assert session.current_proc is None

    asyncio.run(_run())


def test_headless_claude_does_not_start_monitor_when_spawn_fails(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "-p", "{prompt}"],
            headless_cmd=["claude", "-p", "{prompt}"],
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"claude": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )
        marks: list[str] = []

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            raise RuntimeError("spawn failed")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        monkeypatch.setattr(session, "_start_claude_monitor", lambda *_args, **_kwargs: marks.append("start"))
        monkeypatch.setattr(session, "_stop_claude_monitor", lambda: marks.append("stop"))

        with pytest.raises(RuntimeError, match="spawn failed"):
            await session._run_headless("hello")

        assert marks == []
        assert session.current_proc is None

    asyncio.run(_run())
