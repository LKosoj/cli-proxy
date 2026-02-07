import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from agent.orchestrator import OrchestratorRunner
from agent.contracts import ExecutorResponse, PlanStep


class _FakeBot:
    def __init__(self):
        self.events = []
        self.sent_outputs = []
        self.sent_docs = []
        self.send_output_called = asyncio.Event()
        self.doc_called = asyncio.Event()

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        self.events.append(("msg", chat_id, text))

    async def send_output(self, _session, _dest, output: str, _context, **kwargs):
        self.events.append(("send_output", output, kwargs))
        self.sent_outputs.append((output, kwargs))
        self.send_output_called.set()

    async def _send_document(self, _context, *, chat_id: int, document, **_kwargs):
        # document is a file-like object
        self.events.append(("doc", chat_id, getattr(document, "name", "")))
        self.sent_docs.append(getattr(document, "name", ""))
        self.doc_called.set()
        return True


def test_orchestrator_compose_final_answer_sends_ready_and_output(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(id="step1", title="s1", instruction="do 1"),
                PlanStep(id="step2", title="s2", instruction="do 2", depends_on=["step1"]),
            ]

        monkeypatch.setattr("agent.orchestrator.plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, _session, _bot, _context, _dest, _orchestrator_context):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"out {step.id}"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _noop_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch, "_maybe_update_memory", _noop_memory)

        async def _fake_chat_completion(_cfg, _system, _user):
            return "FINAL ANSWER"

        monkeypatch.setattr("agent.orchestrator.chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        assert "FINAL ANSWER" in out

        # Ready message + one send_output (HTML+summary is handled inside send_output itself)
        assert fakebot.events[0][0] == "msg"
        assert "Готово" in fakebot.events[0][2]
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        assert any(e[0] == "send_output" for e in fakebot.events)
        sent = fakebot.sent_outputs[0]
        assert sent[0] == "FINAL ANSWER"
        assert sent[1].get("send_header") is False
        assert sent[1].get("force_html") is True

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_does_not_block_on_send_output(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr("agent.orchestrator.plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, _session, _bot, _context, _dest, _orchestrator_context):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(*_a, **_k):
            return "FINAL"

        monkeypatch.setattr("agent.orchestrator.chat_completion", _fake_chat_completion)

        gate = asyncio.Event()

        class _Bot(_FakeBot):
            async def send_output(self, _session, _dest, output: str, _context, **kwargs):
                # Block until gate is opened; orchestrator.run must not await this.
                await gate.wait()
                return await super().send_output(_session, _dest, output, _context, **kwargs)

        fakebot = _Bot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        t0 = asyncio.get_running_loop().time()
        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        dt = asyncio.get_running_loop().time() - t0
        assert out == "FINAL"
        assert dt < 0.5
        # Let background send finish cleanly.
        gate.set()
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_sends_artifacts(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        artifact_path = tmp_path / "a.txt"
        artifact_path.write_text("hello", encoding="utf-8")

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr("agent.orchestrator.plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, _session, _bot, _context, _dest, _orchestrator_context):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[
                    {"type": "file", "path": str(artifact_path), "name": "a.txt"},
                    {"type": "text", "content": "out"},
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _noop_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch, "_maybe_update_memory", _noop_memory)

        async def _fake_chat_completion(_cfg, _system, _user):
            return "FINAL"

        monkeypatch.setattr("agent.orchestrator.chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        await asyncio.wait_for(fakebot.doc_called.wait(), timeout=1.0)
        assert fakebot.sent_docs
        assert any(str(artifact_path) == p for p in fakebot.sent_docs)

    asyncio.run(_run())
