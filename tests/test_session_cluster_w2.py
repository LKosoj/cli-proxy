"""Tests for audit fixes H1, H3, M7, M9-uid."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager, _CONFIG_SAVE_LOCK, session_runtime_uid
from sessions.conversation_scope import ConversationScope, DesktopScope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_config(tmp_path, *, intent: str = "w2") -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def _fake_session(sid: str, chat_id: int = 1) -> SimpleNamespace:
    scope = ConversationScope.from_parts(chat_id)
    return SimpleNamespace(
        id=sid,
        chat_id=chat_id,
        conversation_scope=scope,
    )


# ---------------------------------------------------------------------------
# M9-uid: _session_by_uid index
# ---------------------------------------------------------------------------

class TestGetByUidIndex:
    def test_index_populated_on_direct_insert(self, tmp_path) -> None:
        mgr = SessionManager(_build_config(tmp_path))
        session = _fake_session("s1")
        mgr._ensure_chat(1)
        mgr.sessions_by_chat[1]["s1"] = session
        mgr._index_session(session)

        uid = session_runtime_uid(session)
        assert mgr._session_by_uid.get(uid) is session

    def test_get_by_uid_uses_index_fast_path(self, tmp_path) -> None:
        mgr = SessionManager(_build_config(tmp_path))
        session = _fake_session("s1")
        mgr._ensure_chat(1)
        mgr.sessions_by_chat[1]["s1"] = session
        mgr._index_session(session)

        uid = session_runtime_uid(session)
        assert mgr.get_by_uid(uid) is session

    def test_get_by_uid_returns_none_for_unknown(self, tmp_path) -> None:
        mgr = SessionManager(_build_config(tmp_path))
        assert mgr.get_by_uid("chat:9999:s99") is None

    def test_index_cleared_after_unindex(self, tmp_path) -> None:
        mgr = SessionManager(_build_config(tmp_path))
        session = _fake_session("s1")
        mgr._ensure_chat(1)
        mgr.sessions_by_chat[1]["s1"] = session
        mgr._index_session(session)

        uid = session_runtime_uid(session)
        assert mgr._session_by_uid.get(uid) is session

        mgr._unindex_session(session)
        assert uid not in mgr._session_by_uid

    def test_get_by_uid_fallback_after_manual_insert_without_index(self, tmp_path) -> None:
        """If session was added without indexing, linear fallback still resolves it."""
        mgr = SessionManager(_build_config(tmp_path))
        session = _fake_session("s2")
        mgr._ensure_chat(1)
        # Bypass index deliberately
        mgr.sessions_by_chat[1]["s2"] = session

        uid = session_runtime_uid(session)
        # Index doesn't have it
        assert mgr._session_by_uid.get(uid) is None
        # But fallback scan finds it
        assert mgr.get_by_uid(uid) is session

    def test_close_removes_session_from_index(self, tmp_path) -> None:
        """close() must call _unindex_session so index stays in sync."""
        mgr = SessionManager(_build_config(tmp_path))
        session = _fake_session("s1")
        mgr._ensure_chat(1)
        mgr.sessions_by_chat[1]["s1"] = session
        mgr._index_session(session)
        # Patch session.close() to avoid real teardown
        session.close = lambda: None

        uid = session_runtime_uid(session)
        assert uid in mgr._session_by_uid

        mgr.close(1, "s1")
        assert uid not in mgr._session_by_uid
        assert mgr.get_by_uid(uid) is None

    def test_multiple_sessions_indexed_independently(self, tmp_path) -> None:
        mgr = SessionManager(_build_config(tmp_path))
        s1 = _fake_session("s1", chat_id=1)
        s2 = _fake_session("s2", chat_id=1)
        mgr._ensure_chat(1)
        mgr.sessions_by_chat[1]["s1"] = s1
        mgr._index_session(s1)
        mgr.sessions_by_chat[1]["s2"] = s2
        mgr._index_session(s2)

        assert mgr.get_by_uid(session_runtime_uid(s1)) is s1
        assert mgr.get_by_uid(session_runtime_uid(s2)) is s2

    def test_get_by_uid_rejects_stale_index_entry_after_scope_change(self, tmp_path) -> None:
        mgr = SessionManager(_build_config(tmp_path))
        session = _fake_session("s1", chat_id=0)
        mgr._ensure_chat("desktop")
        mgr.sessions_by_chat["desktop"]["s1"] = session
        stale_uid = session_runtime_uid(session)
        mgr._index_session(session)

        session.conversation_scope = DesktopScope("desktop", "s1")
        current_uid = session_runtime_uid(session)

        assert stale_uid == "chat:0:s1"
        assert current_uid == "desktop:s1"
        assert mgr.get_by_uid(stale_uid) is None
        assert stale_uid not in mgr._session_by_uid
        assert mgr.get_by_uid(current_uid) is session
        assert mgr._session_by_uid.get(current_uid) is session


# ---------------------------------------------------------------------------
# M7: _signal_process_group signals the process group directly
#
# Вызывающий код (_signal_headless_process_tree) уже резолвит group id через
# _process_group_id и передаёт сюда pgid; корневой headless-процесс стартует с
# start_new_session=True, поэтому для него pid == pgid. Повторный os.getpgid
# здесь был бы багом: для уже-резолвнутого group_id он трактовал бы pgid как
# pid и при мёртвом лидере группы (ProcessLookupError) терял бы живые члены
# группы, не отправив им сигнал.
# ---------------------------------------------------------------------------

class TestSignalProcessGroup:
    def test_calls_killpg_with_pid_directly(self) -> None:
        """_signal_process_group must call os.killpg(pid, sig) directly (pid уже pgid)."""
        with patch("os.killpg") as mock_killpg:
            import signal as _signal
            from session import Session
            result = Session._signal_process_group(100, _signal.SIGTERM)

        mock_killpg.assert_called_once_with(100, _signal.SIGTERM)
        assert result is True

    def test_returns_true_when_group_already_gone_on_killpg(self) -> None:
        with patch("os.killpg", side_effect=ProcessLookupError):
            import signal as _signal
            from session import Session
            result = Session._signal_process_group(200, _signal.SIGTERM)
        assert result is True

    def test_returns_false_on_unexpected_error_in_killpg(self) -> None:
        with patch("os.killpg", side_effect=OSError("perm")):
            import signal as _signal
            from session import Session
            result = Session._signal_process_group(300, _signal.SIGTERM)
        assert result is False

    def test_returns_false_when_pid_is_none(self) -> None:
        import signal as _signal
        from session import Session
        assert Session._signal_process_group(None, _signal.SIGTERM) is False


# ---------------------------------------------------------------------------
# H3: _CONFIG_SAVE_LOCK is a module-level singleton
# ---------------------------------------------------------------------------

class TestConfigSaveLock:
    def test_lock_is_threading_lock(self) -> None:
        assert isinstance(_CONFIG_SAVE_LOCK, type(threading.Lock()))

    def test_lock_is_module_singleton(self) -> None:
        import session as session_mod
        assert session_mod._CONFIG_SAVE_LOCK is _CONFIG_SAVE_LOCK

    def test_save_path_acquires_module_lock_not_fresh_lock(self, tmp_path) -> None:
        """H3-контракт: путь автосохранения config входит именно в module-level
        _CONFIG_SAVE_LOCK, а не создаёт свежий threading.Lock() (который не давал бы
        взаимного исключения между конкурентными сессиями, пишущими общий config).

        Проверяем через реальный save-path _maybe_autoset_resume_regex: подменяем
        module-level lock на мок и убеждаемся, что save-path вошёл/вышел именно из него.
        """
        import session as session_mod
        from session import Session

        cfg = _build_config(tmp_path)
        tool = cfg.tools["dummy"]
        tool.resume_regex = ""  # пусто → save-path не уйдёт в ранний return
        session = Session(id="s1", tool=tool, workdir=str(tmp_path), idle_timeout_sec=10, config=cfg)

        fake_lock = MagicMock()
        # MagicMock.__exit__ и так по умолчанию возвращает False (исключения внутри
        # with не подавляются); фиксируем явно, чтобы тест не маскировал ошибки.
        fake_lock.__exit__.return_value = False
        with (
            patch.object(session_mod, "_CONFIG_SAVE_LOCK", fake_lock),
            patch.object(session_mod, "detect_resume_regex", return_value=r"resume:(\S+)"),
            patch.object(session_mod, "save_config") as mock_save,
        ):
            session._maybe_autoset_resume_regex("some output with resume token")

        fake_lock.__enter__.assert_called_once()
        fake_lock.__exit__.assert_called_once()
        mock_save.assert_called_once()
        assert session.tool.resume_regex == r"resume:(\S+)"


# ---------------------------------------------------------------------------
# H1: async _persist_session_async off-loads to thread
# ---------------------------------------------------------------------------

class TestPersistSessionAsync:
    @pytest.mark.asyncio
    async def test_handlers_persist_session_async_calls_to_thread(self) -> None:
        """_persist_session_async must delegate to asyncio.to_thread."""
        called_with: list[tuple] = []

        class _FakeHandlers:
            def _persist_session(self, chat_id: int, session_id: str) -> None:
                called_with.append((chat_id, session_id))

            async def _persist_session_async(self, chat_id: int, session_id: str) -> None:
                await asyncio.to_thread(self._persist_session, chat_id, session_id)

        h = _FakeHandlers()
        await h._persist_session_async(7, "s1")
        assert called_with == [(7, "s1")]

    @pytest.mark.asyncio
    async def test_callbacks_persist_session_async_imported(self) -> None:
        """tg.callbacks.CallbackHandler must expose _persist_session_async."""
        from tg.callbacks import CallbackHandler
        assert hasattr(CallbackHandler, "_persist_session_async")
        assert asyncio.iscoroutinefunction(CallbackHandler._persist_session_async)

    @pytest.mark.asyncio
    async def test_handlers_persist_session_async_imported(self) -> None:
        """tg.handlers.BotHandlers must expose _persist_session_async."""
        from tg.handlers import BotHandlers
        assert hasattr(BotHandlers, "_persist_session_async")
        assert asyncio.iscoroutinefunction(BotHandlers._persist_session_async)
