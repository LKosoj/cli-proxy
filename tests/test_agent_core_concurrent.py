"""
Тесты фикса M6: гонка при мутации _sessions и промах clear_session_cache.
"""
import asyncio
import types
from typing import Any, Dict

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.runtime.agent_core import ReActAgent


# ==== Вспомогательные фабрики ====

def _make_agent() -> ReActAgent:
    """Создаёт ReActAgent без вызова реального __init__ для unit-тестов."""
    agent: ReActAgent = ReActAgent.__new__(ReActAgent)
    agent._sessions: Dict[str, Any] = {}
    agent._session_id_index: Dict[str, set] = {}
    agent._session_locks: Dict[str, asyncio.Lock] = {}
    return agent


def _make_config(tmp_path: Any) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="x", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            openai_api_key="test-key",
            openai_model="test-model",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path) + "/config.yaml",
    )


class _NoToolRegistry:
    """Минимальный stub ToolRegistry для тестов run()."""

    def list_tool_names(self) -> list:
        return []

    async def get_definitions_async(self, *args: Any, **kwargs: Any) -> list:
        return []

    async def execute_many(self, calls: list, ctx: Any) -> list:
        return []

    def record_message(self, chat_id: int, message_id: int) -> None:
        pass

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return False

    def get_definitions(self, *args: Any, **kwargs: Any) -> list:
        return []

    def get_summary_definitions(self, *args: Any, **kwargs: Any) -> list:
        return []

    def get_tool_detail(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_message_handlers(self, *args: Any, **kwargs: Any) -> list:
        return []

    def get_inline_handlers(self, *args: Any, **kwargs: Any) -> list:
        return []

    def get_missing_suggestions(self, name: str) -> list:
        return []


def _make_session_ns(session_id: str, chat_id: int, workdir: str) -> Any:
    return types.SimpleNamespace(
        id=session_id,
        chat_id=chat_id,
        workdir=workdir,
        state_root=workdir,
        scoped_key=f"{chat_id}_{session_id}",
    )


# ==== Тесты clear_session_cache (чисто unit, без run()) ====

def test_clear_direct_hit_removes_entry():
    """clear_session_cache("123_abc") удаляет запись напрямую."""
    agent = _make_agent()
    agent._sessions["123_abc"] = {"data": 1}

    agent.clear_session_cache("123_abc")

    assert "123_abc" not in agent._sessions


def test_clear_index_hit_removes_scoped_key():
    """clear_session_cache("abc") чистит scoped "123_abc" через индекс."""
    agent = _make_agent()
    agent._sessions["123_abc"] = {"data": 1}
    agent._session_id_index["abc"] = {"123_abc"}

    agent.clear_session_cache("abc")

    assert "123_abc" not in agent._sessions
    assert "abc" not in agent._session_id_index


def test_clear_index_multiple_scoped_keys():
    """Один raw_id может маппиться на несколько scoped-ключей."""
    agent = _make_agent()
    agent._sessions["100_abc"] = {"a": 1}
    agent._sessions["200_abc"] = {"b": 2}
    agent._session_id_index["abc"] = {"100_abc", "200_abc"}

    agent.clear_session_cache("abc")

    assert "100_abc" not in agent._sessions
    assert "200_abc" not in agent._sessions
    assert "abc" not in agent._session_id_index


def test_clear_no_keyerror_when_missing():
    """Нет KeyError если ключа нет."""
    agent = _make_agent()
    agent.clear_session_cache("nonexistent")  # не должен бросать


def test_clear_both_direct_and_index():
    """clear("abc"): прямое совпадение AND индексные записи удаляются вместе."""
    agent = _make_agent()
    agent._sessions["abc"] = {"direct": True}
    agent._sessions["123_abc"] = {"scoped": True}
    agent._session_id_index["abc"] = {"123_abc"}

    agent.clear_session_cache("abc")

    assert "abc" not in agent._sessions
    assert "123_abc" not in agent._sessions


# ==== Тест: индекс не копит orphan-ключи после run() ====

def test_index_no_orphan_after_run(tmp_path, monkeypatch):
    """После завершения run() обратный индекс не содержит orphan-ключей."""
    cfg = _make_config(tmp_path)
    agent = ReActAgent(cfg, _NoToolRegistry())

    session_obj = _make_session_ns("abc", chat_id=99, workdir=str(tmp_path))

    async def _fake_call_openai(self_inner: Any, messages: Any, allowed_tools: Any) -> Dict[str, Any]:
        return {"role": "assistant", "content": "done", "tool_calls": []}

    async def _fake_claims(self_inner: Any, *, text: Any, status: Any, model_name: Any) -> tuple:
        return ([], "none")

    monkeypatch.setattr(agent, "_call_openai", types.MethodType(_fake_call_openai, agent))
    monkeypatch.setattr(agent, "_extract_structured_claims", types.MethodType(_fake_claims, agent))

    asyncio.run(agent.run(
        session_id="abc",
        user_message="привет",
        session_obj=session_obj,
        bot=None,
        context=None,
        chat_id=99,
        chat_type="private",
        task_id="t1",
    ))

    assert "99_abc" not in agent._sessions
    assert "99_abc" in agent._session_locks
    bucket = agent._session_id_index.get("abc")
    assert not bucket  # None или пустое множество


# ==== Тест: конкурентные run двух разных сессий изолированы ====

def test_concurrent_runs_isolated(tmp_path, monkeypatch):
    """
    Два конкурентных run для разных сессий не мешают друг другу:
    каждый загружает свою сессию и сохраняет независимо.
    """
    cfg = _make_config(tmp_path)
    agent = ReActAgent(cfg, _NoToolRegistry())

    session_a = _make_session_ns("sessA", chat_id=10, workdir=str(tmp_path))
    session_b = _make_session_ns("sessB", chat_id=20, workdir=str(tmp_path))

    async def _fake_call_openai(self_inner: Any, messages: Any, allowed_tools: Any) -> Dict[str, Any]:
        # Уступаем event-loop, чтобы оба run перекрывались
        await asyncio.sleep(0)
        return {"role": "assistant", "content": "ok", "tool_calls": []}

    async def _fake_claims(self_inner: Any, *, text: Any, status: Any, model_name: Any) -> tuple:
        return ([], "none")

    monkeypatch.setattr(agent, "_call_openai", types.MethodType(_fake_call_openai, agent))
    monkeypatch.setattr(agent, "_extract_structured_claims", types.MethodType(_fake_claims, agent))

    async def _run_both() -> tuple:
        return await asyncio.gather(
            agent.run(
                session_id="sessA",
                user_message="msg A",
                session_obj=session_a,
                bot=None,
                context=None,
                chat_id=10,
                chat_type="private",
                task_id="tA",
            ),
            agent.run(
                session_id="sessB",
                user_message="msg B",
                session_obj=session_b,
                bot=None,
                context=None,
                chat_id=20,
                chat_type="private",
                task_id="tB",
            ),
        )

    results = asyncio.run(_run_both())

    assert len(results) == 2
    assert results[0].output == "ok"
    assert results[1].output == "ok"
    # После завершения кэш должен быть очищен
    assert "10_sessA" not in agent._sessions
    assert "20_sessB" not in agent._sessions
