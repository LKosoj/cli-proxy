import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.runtime.agent_core import AgentRunResult, ReActAgent
from modes.sdk.runtime.contracts import ExecutorRequest
from modes.sdk.runtime.events import EventType
from modes.sdk.runtime.executor import Executor
from modes.sdk.runtime.profiles import ExecutorProfile


class _ToolRegistryStub:
    async def execute(self, _name, _args, _ctx):
        return {"success": True, "output": "ok"}

    async def execute_many(self, _calls, _ctx):
        return [{"success": False, "error": "tool failed"}]

    def list_tool_names(self):
        return ["run_command"]

    def record_message(self, _chat_id, _message_id):
        return None

    def resolve_question(self, _question_id, _answer):
        return False

    def build_bot_ui(self, _allowed_tools):
        return {}


class _CtxCapturingToolRegistry(_ToolRegistryStub):
    def __init__(self) -> None:
        self.ctx_calls = []

    async def execute_many(self, _calls, ctx):
        self.ctx_calls.append(dict(ctx or {}))
        return [{"success": True, "output": "ok"}]


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(
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


@pytest.mark.asyncio
async def test_executor_emits_reactions_for_failed_runner_status(tmp_path, monkeypatch):
    executor = Executor(_cfg(tmp_path), _ToolRegistryStub())
    captured_events = []

    async def _fake_reactions_execute(event, _rules, *, ctx=None):
        captured_events.append((event, ctx))
        return [{"action": "notify_failure", "status": "queued"}]

    async def _fake_runner_run(*_args, **_kwargs):
        return AgentRunResult(output="failed result", status="error", tool_calls=[])

    monkeypatch.setattr(executor._reaction_engine, "execute", _fake_reactions_execute)
    monkeypatch.setattr(executor._runner, "run", _fake_runner_run)

    resp = await executor.run(
        session=SimpleNamespace(id="s1"),
        request=ExecutorRequest(task_id="t1", goal="do work", context="", corr_id="corr-1"),
        bot=None,
        context=None,
        dest={"chat_id": 1, "chat_type": "private"},
        profile=ExecutorProfile(name="default", allowed_tools=["run_command"], timeout_ms=5000, max_retries=0),
    )

    assert resp.status == "error"
    assert captured_events
    assert captured_events[0][0].event_type is EventType.STEP_FAILED
    assert any(call.get("tool") == "reactions_v2" for call in resp.tool_calls)


@pytest.mark.asyncio
async def test_executor_logs_exception_with_logger_exception(tmp_path, monkeypatch):
    executor = Executor(_cfg(tmp_path), _ToolRegistryStub())
    executor._log.exception = MagicMock()

    async def _fake_runner_run(*_args, **_kwargs):
        raise RuntimeError("boom")

    async def _fake_reactions_execute(_event, _rules, *, ctx=None):
        return [{"action": "notify_failure", "status": "queued"}]

    monkeypatch.setattr(executor._runner, "run", _fake_runner_run)
    monkeypatch.setattr(executor._reaction_engine, "execute", _fake_reactions_execute)

    resp = await executor.run(
        session=SimpleNamespace(id="s1"),
        request=ExecutorRequest(task_id="t1", goal="do work", context="", corr_id="corr-2"),
        bot=None,
        context=None,
        dest={"chat_id": 1, "chat_type": "private"},
        profile=ExecutorProfile(name="default", allowed_tools=["run_command"], timeout_ms=5000, max_retries=0),
    )

    assert resp.status == "error"
    assert executor._log.exception.called


@pytest.mark.asyncio
async def test_executor_extracts_multiple_claims_from_runner_output(tmp_path, monkeypatch):
    executor = Executor(_cfg(tmp_path), _ToolRegistryStub())

    async def _fake_runner_run(*_args, **_kwargs):
        return AgentRunResult(
            output=(
                "- Меню содержит login CTA.\n"
                "- В header есть account dropdown.\n"
                "- Форма регистрации подключена на отдельном экране."
            ),
            status="ok",
            tool_calls=[],
        )

    monkeypatch.setattr(executor._runner, "run", _fake_runner_run)

    resp = await executor.run(
        session=SimpleNamespace(id="s1"),
        request=ExecutorRequest(task_id="t-claims", goal="collect facts", context="", corr_id="corr-claims"),
        bot=None,
        context=None,
        dest={"chat_id": 1, "chat_type": "private"},
        profile=ExecutorProfile(name="default", allowed_tools=["run_command"], timeout_ms=5000, max_retries=0),
    )

    assert resp.status == "ok"
    claim_texts = [str(item.get("text") or "") for item in resp.claims]
    assert "Меню содержит login CTA." in claim_texts
    assert "В header есть account dropdown." in claim_texts
    assert "Форма регистрации подключена на отдельном экране." in claim_texts


@pytest.mark.asyncio
async def test_executor_preserves_full_summary_from_runner_output(tmp_path, monkeypatch):
    executor = Executor(_cfg(tmp_path), _ToolRegistryStub())
    long_output = "A" * 1200

    async def _fake_runner_run(*_args, **_kwargs):
        return AgentRunResult(output=long_output, status="ok", tool_calls=[])

    monkeypatch.setattr(executor._runner, "run", _fake_runner_run)

    resp = await executor.run(
        session=SimpleNamespace(id="s1"),
        request=ExecutorRequest(task_id="t-summary", goal="collect facts", context="", corr_id="corr-summary"),
        bot=None,
        context=None,
        dest={"chat_id": 1, "chat_type": "private"},
        profile=ExecutorProfile(name="default", allowed_tools=["run_command"], timeout_ms=5000, max_retries=0),
    )

    assert resp.status == "ok"
    assert resp.summary == long_output


@pytest.mark.asyncio
async def test_agent_core_returns_native_structured_claims(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    react = ReActAgent(cfg, _ToolRegistryStub())

    llm_messages = [
        {
            "role": "assistant",
            "content": "- Меню содержит login CTA.\n- В header есть account dropdown.",
            "tool_calls": [],
        }
    ]

    async def _fake_call_openai(_messages, _allowed_tools):
        return llm_messages.pop(0)

    async def _fake_claim_completion(*_args, **_kwargs):
        payload = {
            "claims": [
                {
                    "claim_id": "c1",
                    "status": "confirmed",
                    "text": "Меню содержит login CTA.",
                    "component_scope": "header",
                    "allowed_final_usage": "fact",
                    "evidence": [],
                },
                {
                    "claim_id": "c2",
                    "status": "confirmed",
                    "text": "В header есть account dropdown.",
                    "component_scope": "header",
                    "allowed_final_usage": "fact",
                    "evidence": [],
                },
            ]
        }
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr("modes.sdk.runtime.agent_core.runtime_chat_completion", _fake_claim_completion)

    result = await react.run(
        session_id="s1",
        user_message="collect facts",
        session_obj=SimpleNamespace(id="s1", workdir=str(tmp_path), state_root=str(tmp_path / "state")),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-native-claims",
        allowed_tools=["run_command"],
        request_context=None,
        constraints=None,
        corr_id="corr-native-claims",
    )

    claim_texts = [str(item.get("text") or "") for item in result.claims]
    assert "Меню содержит login CTA." in claim_texts
    assert "В header есть account dropdown." in claim_texts
    assert result.claims_source == "llm_json"


@pytest.mark.asyncio
async def test_agent_core_persists_context_summary_artifacts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    react = ReActAgent(cfg, _ToolRegistryStub())

    async def _fake_working_summary(working, **kwargs):
        del kwargs
        return ([{"role": "assistant", "content": "[Суммаризация рабочего контекста]\nsummary of working context"}], True)

    async def _fake_context_summary(messages, **kwargs):
        del kwargs
        return ([*messages[:1], {"role": "assistant", "content": "[Контекст суммаризирован]\nsummary of historical context"}], True)

    async def _fake_call_openai(_messages, _allowed_tools):
        return {"role": "assistant", "content": "final text", "tool_calls": []}

    monkeypatch.setattr("modes.sdk.runtime.context_summarizer.summarize_working_context", _fake_working_summary)
    monkeypatch.setattr("modes.sdk.runtime.context_summarizer.summarize_context", _fake_context_summary)
    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr("modes.sdk.runtime.agent_core.runtime_chat_completion", lambda *_a, **_k: asyncio.sleep(0, result='{"claims": []}'))

    result = await react.run(
        session_id="s1",
        user_message="collect facts",
        session_obj=SimpleNamespace(id="s1", workdir=str(tmp_path), state_root=str(tmp_path / "state")),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-context-artifacts",
        allowed_tools=["run_command"],
        request_context=None,
        constraints=None,
        corr_id="corr-context-artifacts",
    )

    assert result.output == "final text"
    artifact_dir = tmp_path / "_orchestrator"
    working_artifact = artifact_dir / "s1_working_context_summary_iter_1.md"
    history_artifact = artifact_dir / "s1_historical_context_summary_iter_1.md"
    assert working_artifact.exists()
    assert history_artifact.exists()
    assert "summary of working context" in working_artifact.read_text(encoding="utf-8")
    assert "summary of historical context" in history_artifact.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_agent_core_persists_context_summaries_into_analyst_run_artifacts_dir(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    react = ReActAgent(cfg, _ToolRegistryStub())
    artifacts_dir = tmp_path / ".cli-proxy" / "runs" / "desktop:s1" / "analyst" / "run_20260410T121000Z_ctx" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    async def _fake_working_summary(working, **kwargs):
        del kwargs
        return ([{"role": "assistant", "content": "[Суммаризация рабочего контекста]\nsummary of working context"}], True)

    async def _fake_context_summary(messages, **kwargs):
        del kwargs
        return ([*messages[:1], {"role": "assistant", "content": "[Контекст суммаризирован]\nsummary of historical context"}], True)

    async def _fake_call_openai(_messages, _allowed_tools):
        return {"role": "assistant", "content": "final text", "tool_calls": []}

    monkeypatch.setattr("modes.sdk.runtime.context_summarizer.summarize_working_context", _fake_working_summary)
    monkeypatch.setattr("modes.sdk.runtime.context_summarizer.summarize_context", _fake_context_summary)
    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr("modes.sdk.runtime.agent_core.runtime_chat_completion", lambda *_a, **_k: asyncio.sleep(0, result='{"claims": []}'))

    result = await react.run(
        session_id="s1",
        user_message="collect facts",
        session_obj=SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            state_root=str(tmp_path / "state"),
            tool_session=SimpleNamespace(
                analyst_run_artifact_handle=SimpleNamespace(artifacts_dir=str(artifacts_dir))
            ),
        ),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-context-artifacts-run-scoped",
        allowed_tools=["run_command"],
        request_context=None,
        constraints=None,
        corr_id="corr-context-artifacts-run-scoped",
    )

    assert result.output == "final text"
    working_artifact = artifacts_dir / "s1_working_context_summary_iter_1.md"
    history_artifact = artifacts_dir / "s1_historical_context_summary_iter_1.md"
    legacy_dir = tmp_path / "_orchestrator"
    assert working_artifact.exists()
    assert history_artifact.exists()
    assert legacy_dir.exists() is False
    assert "summary of working context" in working_artifact.read_text(encoding="utf-8")
    assert "summary of historical context" in history_artifact.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_agent_core_compacts_large_request_context_before_first_call(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    react = ReActAgent(cfg, _ToolRegistryStub())
    captured_messages = []
    large_context = "C" * 60_000

    async def _fake_call_openai(messages, _allowed_tools):
        captured_messages.append(messages)
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr(
        "modes.sdk.runtime.agent_core.runtime_chat_completion",
        lambda *_a, **_k: asyncio.sleep(0, result='{"claims": []}'),
    )

    result = await react.run(
        session_id="s1",
        user_message="collect facts",
        session_obj=SimpleNamespace(id="s1", workdir=str(tmp_path), state_root=str(tmp_path / "state")),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-large-request-context",
        allowed_tools=["run_command"],
        request_context=large_context,
        constraints=None,
        corr_id="corr-large-request-context",
    )

    assert result.status == "ok"
    assert captured_messages
    system_blocks = [
        str(item.get("content") or "")
        for item in captured_messages[0]
        if item.get("role") == "system" and "<REQUEST_CONTEXT>" in str(item.get("content") or "")
    ]
    assert system_blocks
    assert "[context-trim: request_context;" in system_blocks[0]
    assert len(system_blocks[0]) < len(large_context)


@pytest.mark.asyncio
async def test_agent_core_compacts_large_working_payloads_between_iterations(tmp_path, monkeypatch):
    class _LargeOutputRegistry(_ToolRegistryStub):
        async def execute_many(self, _calls, _ctx):
            return [{"success": True, "output": "O" * 50_000}]

    cfg = _cfg(tmp_path)
    react = ReActAgent(cfg, _LargeOutputRegistry())
    captured_messages = []
    llm_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps(
                            {"command": "echo ok", "notes": "N" * 20_000},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "done", "tool_calls": []},
    ]

    async def _fake_call_openai(messages, _allowed_tools):
        captured_messages.append(messages)
        return llm_messages.pop(0)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr(
        "modes.sdk.runtime.agent_core.runtime_chat_completion",
        lambda *_a, **_k: asyncio.sleep(0, result='{"claims": []}'),
    )

    result = await react.run(
        session_id="s1",
        user_message="run task",
        session_obj=SimpleNamespace(id="s1", workdir=str(tmp_path), state_root=str(tmp_path / "state")),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-large-working-payloads",
        allowed_tools=["run_command"],
        request_context=None,
        constraints=None,
        corr_id="corr-large-working-payloads",
    )

    assert result.status == "ok"
    assert len(captured_messages) == 2
    followup_messages = captured_messages[1]
    assistant_with_tool = next(item for item in followup_messages if item.get("tool_calls"))
    compact_args = assistant_with_tool["tool_calls"][0]["function"]["arguments"]
    assert "[context-trim: tool_arguments;" in compact_args
    assert len(compact_args) < 10_000
    tool_message = next(item for item in followup_messages if item.get("role") == "tool")
    compact_output = str(tool_message.get("content") or "")
    assert "[context-trim: working_message;" in compact_output
    assert len(compact_output) < 15_000


@pytest.mark.asyncio
async def test_executor_marks_text_claim_fallback_when_runner_has_no_claims(tmp_path, monkeypatch):
    executor = Executor(_cfg(tmp_path), _ToolRegistryStub())

    async def _fake_runner_run(*_args, **_kwargs):
        return AgentRunResult(
            output="- Меню содержит login CTA.\n- В header есть account dropdown.",
            status="ok",
            tool_calls=[],
            claims=[],
            claims_source="",
        )

    monkeypatch.setattr(executor._runner, "run", _fake_runner_run)

    resp = await executor.run(
        session=SimpleNamespace(id="s1"),
        request=ExecutorRequest(task_id="t-claims-source", goal="collect facts", context="", corr_id="corr-claims-source"),
        bot=None,
        context=None,
        dest={"chat_id": 1, "chat_type": "private"},
        profile=ExecutorProfile(name="default", allowed_tools=["run_command"], timeout_ms=5000, max_retries=0),
    )

    assert resp.status == "ok"
    assert resp.claims
    assert resp.claims_source == "text_fallback"


@pytest.mark.asyncio
async def test_agent_core_emits_failure_event_callback_on_tool_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    react = ReActAgent(cfg, _ToolRegistryStub())
    emitted = []

    async def _cb(payload):
        emitted.append(payload)

    llm_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "run_command", "arguments": "{\"cmd\":\"false\"}"},
                }
            ],
        },
        {"role": "assistant", "content": "done", "tool_calls": []},
    ]

    async def _fake_call_openai(_messages, _allowed_tools):
        return llm_messages.pop(0)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)

    result = await react.run(
        session_id="s1",
        user_message="run task",
        session_obj=SimpleNamespace(id="s1", workdir=str(tmp_path), state_root=str(tmp_path / "state")),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t1",
        allowed_tools=["run_command"],
        request_context=None,
        constraints=None,
        corr_id="corr-3",
        failure_event_callback=_cb,
    )

    assert result.status == "partial"
    assert emitted
    assert emitted[0]["source"] == "agent_core.tool_call"
    assert emitted[0]["tool_name"] == "run_command"


@pytest.mark.asyncio
async def test_agent_core_exposes_raw_session_id_and_scoped_key_in_tool_ctx(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    registry = _CtxCapturingToolRegistry()
    react = ReActAgent(cfg, registry)

    llm_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "manage_tasks", "arguments": "{\"action\":\"list\"}"},
                }
            ],
        },
        {"role": "assistant", "content": "done", "tool_calls": []},
    ]

    async def _fake_call_openai(_messages, _allowed_tools):
        return llm_messages.pop(0)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)

    result = await react.run(
        session_id="s1",
        user_message="inspect tasks",
        session_obj=SimpleNamespace(
            id="s1",
            scoped_key="1_s1",
            workdir=str(tmp_path),
            state_root=str(tmp_path / "state"),
        ),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-scoped",
        allowed_tools=["manage_tasks"],
        request_context=None,
        constraints=None,
        corr_id="corr-scoped",
    )

    assert result.status == "ok"
    assert registry.ctx_calls
    assert registry.ctx_calls[0]["session_id"] == "s1"
    assert registry.ctx_calls[0]["session_scoped_key"] == "1_s1"


@pytest.mark.asyncio
async def test_agent_core_exposes_run_scoped_manage_tasks_key_in_tool_ctx(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    registry = _CtxCapturingToolRegistry()
    react = ReActAgent(cfg, registry)

    llm_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "manage_tasks", "arguments": "{\"action\":\"list\"}"},
                }
            ],
        },
        {"role": "assistant", "content": "done", "tool_calls": []},
    ]

    async def _fake_call_openai(_messages, _allowed_tools):
        return llm_messages.pop(0)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)

    result = await react.run(
        session_id="s1",
        user_message="inspect tasks",
        session_obj=SimpleNamespace(
            id="s1",
            scoped_key="1_s1",
            workdir=str(tmp_path),
            state_root=str(tmp_path / "state"),
        ),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-scoped",
        allowed_tools=["manage_tasks"],
        request_context=None,
        constraints=None,
        corr_id="corr-scoped",
        run_handle=SimpleNamespace(run_id="run-1"),
    )

    assert result.status == "ok"
    assert registry.ctx_calls
    assert registry.ctx_calls[0]["run_id"] == "run-1"
    assert registry.ctx_calls[0]["manage_tasks_scope_key"] == "1_s1:manage_tasks:run-1"


@pytest.mark.asyncio
async def test_agent_core_uses_real_tool_session_from_proxy_session(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    registry = _CtxCapturingToolRegistry()
    react = ReActAgent(cfg, registry)

    llm_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "manage_tasks", "arguments": "{\"action\":\"list\"}"},
                }
            ],
        },
        {"role": "assistant", "content": "done", "tool_calls": []},
    ]

    async def _fake_call_openai(_messages, _allowed_tools):
        return llm_messages.pop(0)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)

    real_session = SimpleNamespace(id="real-s1", run_prompt=MagicMock())
    proxy_session = SimpleNamespace(
        id="proxy-s1",
        scoped_key="1_proxy-s1",
        workdir=str(tmp_path),
        state_root=str(tmp_path / "state"),
        tool_session=real_session,
    )

    result = await react.run(
        session_id="proxy-s1",
        user_message="inspect tasks",
        session_obj=proxy_session,
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-tool-session",
        allowed_tools=["manage_tasks"],
        request_context=None,
        constraints=None,
        corr_id="corr-tool-session",
    )

    assert result.status == "ok"
    assert registry.ctx_calls
    assert registry.ctx_calls[0]["session"] is real_session
    assert registry.ctx_calls[0]["session_id"] == "proxy-s1"
