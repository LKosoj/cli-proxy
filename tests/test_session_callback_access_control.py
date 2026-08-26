"""Тесты на контроль доступа в session-callback-обработчиках
(tg/callback_actions/session.py).

Дефект: SessionManager.get_by_uid(uid) резолвит сессию по ГЛОБАЛЬНОМУ индексу
всех чатов, а uid берётся из callback_data, которое приходит от клиента и
может быть подделано. Пользователь из whitelist («не-админ», но разрешённый
чат) мог послать callback_data с uid сессии ЧУЖОГО чата и выполнить над ней
действие или увидеть её содержимое.

Подход и стиль заимствованы из tests/test_session_unread.py
(test_cb_sess_unread_toggle_denies_forged_cross_chat_payload): используется
РЕАЛЬНЫЙ BotApp (а не голый MagicMock), чтобы честно проверялась настоящая
BotHandlers._is_session_visible_for_chat, а не заглушка.
"""

import os
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from app.services.ssh_config_loader import save_ssh_config
from bot import BotApp
from config import (
    AppConfig, DefaultsConfig, MCPConfig,
    SSHHostConfig, TelegramConfig, ToolConfig,
)
from miniapp.services.config_service import app_config_to_dict
from session import session_runtime_uid
from sessions.session_state_access import get_orchestrator_pending_input, set_orchestrator_pending_input
from tg.callbacks import CallbackHandler


OWNER_CHAT = 1
ATTACKER_CHAT = 2
ADMIN_CHAT = 999

DENIED_TEXT = "Сессия недоступна."


def _build_config(tmp_path):
    owner_workdir = str(tmp_path / "owner_project")
    attacker_workdir = str(tmp_path / "attacker_project")
    os.makedirs(owner_workdir, exist_ok=True)
    os.makedirs(attacker_workdir, exist_ok=True)
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="t",
            whitelist_chat_ids=[OWNER_CHAT, ATTACKER_CHAT],
            admlist_chat_ids=[ADMIN_CHAT],
            user_workdirs={OWNER_CHAT: [owner_workdir], ATTACKER_CHAT: [attacker_workdir]},
        ),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["echo"])},
        defaults=DefaultsConfig(
            workdir=owner_workdir,
            state_path=str(tmp_path / "state.db"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(app_config_to_dict(cfg), f, sort_keys=False)
    return cfg, owner_workdir, attacker_workdir


def _query(*, chat_id: int, message_id: int = 1, user_id: int = 1):
    return types.SimpleNamespace(
        message=types.SimpleNamespace(chat_id=chat_id, message_id=message_id),
        from_user=types.SimpleNamespace(id=user_id, language_code="ru"),
        answer=AsyncMock(),
    )


def _capture_edits(app):
    """Перехватывает bot_app._edit_message, чтобы не дёргать реальный Telegram API
    и видеть, какой текст показан пользователю в ответ на callback."""
    edits: list[str] = []

    async def _edit_message(_context, *, chat_id, message_id, text, reply_markup=None, md2=True):
        edits.append(str(text))
        return True

    app._edit_message = _edit_message
    return edits


@pytest.fixture
def env(tmp_path):
    cfg, owner_workdir, attacker_workdir = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(OWNER_CHAT, "dummy", owner_workdir)
    edits = _capture_edits(app)
    handler = CallbackHandler(app)
    try:
        yield types.SimpleNamespace(
            app=app,
            session=session,
            edits=edits,
            handler=handler,
            owner_workdir=owner_workdir,
            attacker_workdir=attacker_workdir,
        )
    finally:
        app.shutdown_html_process_pool()


# ---------------------------------------------------------------------------
# 1. _cb_sess_active_pick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_active_pick_denies_forged_cross_chat_payload(env):
    env.app.handlers.build_sessions_active_overview = MagicMock(return_value=("text", MagicMock()))
    data = f"sess_active_pick:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_sess_active_pick(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    env.app.handlers.build_sessions_active_overview.assert_not_called()
    assert env.edits[-1] == DENIED_TEXT


# ---------------------------------------------------------------------------
# 2. _cb_user_project_menu
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_user_project_menu_denies_forged_cross_chat_payload(env):
    env.app.handlers.build_user_project_picker = MagicMock(return_value=("text", MagicMock()))
    data = f"user_project_menu:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_user_project_menu(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    env.app.handlers.build_user_project_picker.assert_not_called()
    assert env.edits[-1] == DENIED_TEXT


# ---------------------------------------------------------------------------
# 3. _handle_user_project_pick (общий код _cb_user_project_pick и
#    _cb_user_project_pick_new — обнаружен по указанию из задания, "строка ~190")
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_user_project_pick_denies_forged_cross_chat_payload(env):
    before = len(env.app.manager.sessions_for_chat(ATTACKER_CHAT))
    data = f"user_project_pick:{session_runtime_uid(env.session)}:0"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_user_project_pick(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] == DENIED_TEXT
    # Ни одна новая сессия под чужим индексом проекта не создана.
    assert len(env.app.manager.sessions_for_chat(ATTACKER_CHAT)) == before


# ---------------------------------------------------------------------------
# 4. _cb_sess_cli
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_cli_denies_forged_cross_chat_payload(env):
    previous_active_cli = env.session.cli.active_cli
    data = f"sess_cli:{session_runtime_uid(env.session)}:codex"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_sess_cli(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] == DENIED_TEXT
    assert env.session.cli.active_cli == previous_active_cli


# ---------------------------------------------------------------------------
# 5. _cb_sess_backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_backend_denies_forged_cross_chat_payload(env):
    previous_backend = env.session._active_execution_backend
    data = f"sess_backend:{session_runtime_uid(env.session)}:tmux"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_sess_backend(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] == DENIED_TEXT
    assert env.session._active_execution_backend == previous_backend


# ---------------------------------------------------------------------------
# 6. _cb_sess_transfer_yes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_transfer_yes_denies_forged_cross_chat_payload(env):
    previous_resume_token = env.session.resume_token
    data = f"sess_transfer_yes:{session_runtime_uid(env.session)}:claude"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_sess_transfer_yes(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] == DENIED_TEXT
    assert env.session.resume_token == previous_resume_token


# ---------------------------------------------------------------------------
# 7. _cb_sess_transfer_no
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_transfer_no_denies_forged_cross_chat_payload(env):
    env.app.handlers.build_sessions_active_overview = MagicMock(return_value=("text", MagicMock()))
    data = f"sess_transfer_no:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_sess_transfer_no(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    env.app.handlers.build_sessions_active_overview.assert_not_called()
    assert env.edits[-1] == DENIED_TEXT


# ---------------------------------------------------------------------------
# 8. _cb_sess_mode_pick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_mode_pick_denies_forged_cross_chat_payload(env):
    env.app.handlers.build_sessions_active_overview = MagicMock(return_value=("text", MagicMock()))
    previous_active_mode = env.session.modes.active_mode
    data = f"sess_mode_pick:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_sess_mode_pick(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    env.app.handlers.build_sessions_active_overview.assert_not_called()
    assert env.edits[-1] == DENIED_TEXT
    assert env.session.modes.active_mode == previous_active_mode


# ---------------------------------------------------------------------------
# 9. _cb_sess_ssh_toggle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_ssh_toggle_denies_forged_cross_chat_payload(env):
    save_ssh_config(env.owner_workdir, {"prod": SSHHostConfig(host="10.0.0.1", user="deploy")})
    assert env.session.modes.ssh_remote_enabled is False
    data = f"sess_ssh_toggle:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_sess_ssh_toggle(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] == DENIED_TEXT
    assert env.session.modes.ssh_remote_enabled is False
    query.answer.assert_not_called()


# ---------------------------------------------------------------------------
# Позитивный тест: админ по-прежнему может выполнить то же действие над чужой
# сессией (в _is_session_visible_for_chat админ проходит проверку всегда) —
# защита не должна ломать легитимный админский путь.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_ssh_toggle_allows_admin_cross_chat(env):
    save_ssh_config(env.owner_workdir, {"prod": SSHHostConfig(host="10.0.0.1", user="deploy")})
    assert env.session.modes.ssh_remote_enabled is False
    data = f"sess_ssh_toggle:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ADMIN_CHAT, user_id=999)

    res = await env.handler._cb_sess_ssh_toggle(data=data, chat_id=ADMIN_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.session.modes.ssh_remote_enabled is True
    query.answer.assert_called_with("Удалённое управление включено")


# ---------------------------------------------------------------------------
# 10. _cb_orch_transition (tg/callbacks.py) — тот же дефект: session_uid из
# callback_data резолвится глобальным get_by_uid.
# ---------------------------------------------------------------------------

def _arm_orchestrator_pending(env):
    """Кладёт сессии владельца pending-переход, который и пытается продавить атакующий."""
    pending = {"text": "запусти деплой", "target_mode_id": "agent", "disable_orchestrator_on_cancel": False}
    set_orchestrator_pending_input(env.session, pending)
    dispatched: list[tuple] = []

    async def _dispatch(session, text, chat_id, context, dest=None):
        dispatched.append((session, text, chat_id))

    env.app.input_dispatch_service.handle_user_input_no_orchestration = _dispatch
    return dispatched


@pytest.mark.asyncio
async def test_cb_orch_transition_denies_forged_cross_chat_payload(env):
    dispatched = _arm_orchestrator_pending(env)
    data = f"orch_transition:cancel:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_orch_transition(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] == DENIED_TEXT
    # Чужой pending остался нетронутым, текст владельца не ушёл в исполнение.
    assert get_orchestrator_pending_input(env.session, None) is not None
    assert dispatched == []


@pytest.mark.asyncio
async def test_cb_orch_transition_allows_owner_chat(env):
    dispatched = _arm_orchestrator_pending(env)
    data = f"orch_transition:cancel:{session_runtime_uid(env.session)}"
    query = _query(chat_id=OWNER_CHAT, user_id=111)

    res = await env.handler._cb_orch_transition(data=data, chat_id=OWNER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] != DENIED_TEXT
    assert get_orchestrator_pending_input(env.session, None) is None
    assert [item[1] for item in dispatched] == ["запусти деплой"]


# ---------------------------------------------------------------------------
# 11. Позитивный кейс ещё для одного защищённого обработчика: показ обзора
# чужой сессии админу по-прежнему работает.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_sess_active_pick_allows_admin_cross_chat(env):
    builder = MagicMock(return_value=("text", MagicMock()))
    env.app.handlers.build_sessions_active_overview = builder
    data = f"sess_active_pick:{session_runtime_uid(env.session)}"
    query = _query(chat_id=ADMIN_CHAT, user_id=999)

    res = await env.handler._cb_sess_active_pick(data=data, chat_id=ADMIN_CHAT, query=query, context=MagicMock())

    assert res is True
    assert DENIED_TEXT not in env.edits
    builder.assert_called()


# ---------------------------------------------------------------------------
# 12. Сбой самой проверки видимости трактуется как отказ (fail-closed): иначе
# упавший чек превращался бы в молчаливое разрешение - ровно та дыра, которую
# закрывает этот хелпер.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_denies_when_visibility_check_raises(env):
    def _boom(_chat_id, _session):
        raise RuntimeError("visibility backend down")

    env.app.handlers._is_session_visible_for_chat = _boom
    builder = MagicMock(return_value=("text", MagicMock()))
    env.app.handlers.build_sessions_active_overview = builder
    data = f"sess_active_pick:{session_runtime_uid(env.session)}"
    # Владелец собственной сессии: даже он получает отказ, пока проверка сломана.
    query = _query(chat_id=OWNER_CHAT, user_id=111)

    res = await env.handler._cb_sess_active_pick(data=data, chat_id=OWNER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.edits[-1] == DENIED_TEXT
    builder.assert_not_called()
