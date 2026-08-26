"""Тесты на контроль доступа в mode-callback-обработчике
(modes/sdk/services/mode_callbacks.py::ModeCallbackRouterService).

Дефект: `_resolve_explicit_session` резолвит session_uid, взятый из
`callback_data` (полностью подделываемого клиентом), через
`SessionManager.get_by_uid` — это ГЛОБАЛЬНЫЙ индекс сессий всех чатов, без
проверки принадлежности чату запроса. Non-admin пользователь из whitelist мог
прислать `callback_data = "ma:agent:clean_session:s=<чужой_chat>:<чужой_sid>"`
и удалить файлы чужой agent-сессии (`modes/agent/mode.py::_cb_clean_session`),
либо `clean_all`.

Подход и стиль заимствованы из tests/test_session_callback_access_control.py:
используется РЕАЛЬНЫЙ BotApp (а не голый MagicMock), чтобы честно проверялась
настоящая цепочка bot_app.is_admin / bot_app.is_session_allowed_for_chat, а не
заглушка. Runtime-функции очистки сессии/сандбокса подменены шпионами, чтобы
не трогать реальную файловую систему и надёжно фиксировать факт (не)вызова.
"""

import os
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from miniapp.services.config_service import app_config_to_dict
from modes.agent.mode import agent_project_session_key
from modes.sdk.services.mode_callbacks import ModeCallbackRouterService
from session import session_runtime_uid, session_scoped_key
from sessions.session_state_access import set_active_mode
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
            user_modes={OWNER_CHAT: "all", ATTACKER_CHAT: "all"},
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


class _AgentRuntimeSpy:
    """Записывает вызовы деструктивных runtime-операций agent-режима вместо
    реального обращения к файловой системе/tmux."""

    def __init__(self) -> None:
        self.interrupt_calls: list[tuple] = []
        self.clear_cache_calls: list[str] = []
        self.clear_files_calls: list[str] = []
        self.clear_sandbox_calls: list[int] = []

    def interrupt_session_fn(self, session_id, chat_id, context):
        self.interrupt_calls.append((session_id, chat_id))

    def clear_session_cache_fn(self, session_id):
        self.clear_cache_calls.append(str(session_id))

    def clear_session_files_fn(self, session_id):
        self.clear_files_calls.append(str(session_id))
        return True

    def clear_sandbox_fn(self, chat_id):
        self.clear_sandbox_calls.append(int(chat_id) if chat_id is not None else 0)
        return (1, 0)

    @property
    def any_destructive_call(self) -> bool:
        return bool(
            self.interrupt_calls or self.clear_cache_calls or self.clear_files_calls or self.clear_sandbox_calls
        )


def _capture_replies(app):
    """Перехватывает bot_app._edit_message и bot_app._send_message: оба пути
    используются mode-callback слоем для ответа пользователю (edit — обычный
    ререндер меню, send — путь отказа в handle_mode_action_callback)."""
    replies: list[str] = []

    async def _edit_message(_context, *, chat_id, message_id, text, reply_markup=None, md2=True):
        replies.append(str(text))
        return True

    async def _send_message(_context, *, chat_id, text, md2=True, **_kwargs):
        replies.append(str(text))
        return None

    app._edit_message = _edit_message
    app._send_message = _send_message
    return replies


@pytest.fixture
def env(tmp_path):
    cfg, owner_workdir, attacker_workdir = _build_config(tmp_path)
    app = BotApp(cfg)
    owner_session = app.manager.create(OWNER_CHAT, "dummy", owner_workdir)
    set_active_mode(owner_session, "agent")
    attacker_session = app.manager.create(ATTACKER_CHAT, "dummy", attacker_workdir)
    set_active_mode(attacker_session, "agent")

    spy = _AgentRuntimeSpy()
    app.mode_agent_runtime.interrupt_session_fn = spy.interrupt_session_fn
    app.mode_agent_runtime.clear_session_cache_fn = spy.clear_session_cache_fn
    app.mode_agent_runtime.clear_session_files_fn = spy.clear_session_files_fn
    app.mode_agent_runtime.clear_sandbox_fn = spy.clear_sandbox_fn

    replies = _capture_replies(app)
    handler = CallbackHandler(app)
    try:
        yield types.SimpleNamespace(
            app=app,
            owner_session=owner_session,
            attacker_session=attacker_session,
            spy=spy,
            replies=replies,
            handler=handler,
        )
    finally:
        app.shutdown_html_process_pool()


# ---------------------------------------------------------------------------
# Негативные тесты: не-админ из whitelist подделывает s=<чужой_uid> в
# callback_data mode-действия.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_session_denies_forged_cross_chat_payload(env):
    data = f"ma:agent:clean_session:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_mode_action(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    # Ни владельца, ни атакующего сессия не тронута никаким деструктивным вызовом.
    assert env.spy.any_destructive_call is False
    assert env.replies
    assert env.replies[-1] == DENIED_TEXT


@pytest.mark.asyncio
async def test_clean_all_denies_forged_cross_chat_payload(env):
    data = f"ma:agent:clean_all:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=ATTACKER_CHAT, user_id=222)

    res = await env.handler._cb_mode_action(data=data, chat_id=ATTACKER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.spy.any_destructive_call is False
    assert env.replies
    assert env.replies[-1] == DENIED_TEXT


# ---------------------------------------------------------------------------
# Позитивные тесты: владелец полным uid своей сессии и админ по чужому полному
# uid по-прежнему могут выполнить действие (регрессии нет).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_session_allows_owner_full_uid(env):
    data = f"ma:agent:clean_session:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=OWNER_CHAT, user_id=111)

    res = await env.handler._cb_mode_action(data=data, chat_id=OWNER_CHAT, query=query, context=MagicMock())

    assert res is True
    expected_key = session_scoped_key(env.owner_session) or env.owner_session.id
    assert env.spy.clear_files_calls == [expected_key]
    assert DENIED_TEXT not in env.replies


@pytest.mark.asyncio
async def test_clean_session_allows_admin_cross_chat(env):
    data = f"ma:agent:clean_session:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=ADMIN_CHAT, user_id=999)

    res = await env.handler._cb_mode_action(data=data, chat_id=ADMIN_CHAT, query=query, context=MagicMock())

    assert res is True
    expected_key = session_scoped_key(env.owner_session) or env.owner_session.id
    assert env.spy.clear_files_calls == [expected_key]
    assert DENIED_TEXT not in env.replies


@pytest.mark.asyncio
async def test_clean_all_allows_admin_cross_chat(env):
    data = f"ma:agent:clean_all:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=ADMIN_CHAT, user_id=999)

    res = await env.handler._cb_mode_action(data=data, chat_id=ADMIN_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.spy.clear_sandbox_calls == [ADMIN_CHAT]
    assert DENIED_TEXT not in env.replies


# ---------------------------------------------------------------------------
# Контракт правила допуска на двойниках bot_app без методов доступа: доверяем
# только совпадению чата, а не uid, присланному клиентом.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# group thread mode: сообщение приходит из общей супергруппы, а права
# (admlist/whitelist/user_workdirs) ключуются личным id пользователя. Проверять
# доступ по чату сообщения нельзя ни в одну, ни в другую сторону.
# ---------------------------------------------------------------------------

GROUP_CHAT = -100500


def _arm_group_scope(env, *, owner_chat_id: int):
    """Имитирует group thread mode: ответ уходит в супергруппу, владелец запроса -
    личный id пользователя."""
    env.app.resolve_telegram_callback_scope = lambda _query: (
        GROUP_CHAT, None, owner_chat_id, env.owner_session,
    )


@pytest.mark.asyncio
async def test_clean_session_allows_owner_from_group_topic(env):
    _arm_group_scope(env, owner_chat_id=OWNER_CHAT)
    data = f"ma:agent:clean_session:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=GROUP_CHAT, user_id=111)

    res = await env.handler._cb_mode_action(data=data, chat_id=GROUP_CHAT, query=query, context=MagicMock())

    assert res is True
    expected_key = session_scoped_key(env.owner_session) or env.owner_session.id
    assert env.spy.clear_files_calls == [expected_key]
    assert DENIED_TEXT not in env.replies


@pytest.mark.asyncio
async def test_clean_session_denies_group_member_when_group_id_is_in_admlist(env):
    # Правдоподобная настройка: id супергруппы добавлен в admlist, чтобы
    # админ-команды работали прямо в ней. Обычный участник группы админом от
    # этого не становится.
    env.app.config.telegram.admlist_chat_ids = [ADMIN_CHAT, GROUP_CHAT]
    _arm_group_scope(env, owner_chat_id=ATTACKER_CHAT)
    data = f"ma:agent:clean_session:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=GROUP_CHAT, user_id=222)

    res = await env.handler._cb_mode_action(data=data, chat_id=GROUP_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.spy.any_destructive_call is False
    assert env.replies
    assert env.replies[-1] == DENIED_TEXT


@pytest.mark.asyncio
async def test_clean_session_denies_when_ownership_check_raises(env):
    """Сбой самой проверки владельца = отказ (fail-closed). Проверяется на
    владельце его же сессии: если бы упавший чек трактовался как разрешение,
    отказа бы не было и файлы удалились."""

    def _boom(_chat_id, _session):
        raise RuntimeError("access policy unavailable")

    env.app.is_session_allowed_for_chat = _boom
    data = f"ma:agent:clean_session:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=OWNER_CHAT, user_id=111)

    res = await env.handler._cb_mode_action(data=data, chat_id=OWNER_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.spy.any_destructive_call is False
    assert env.replies
    assert env.replies[-1] == DENIED_TEXT


def test_access_rule_without_bot_app_api_falls_back_to_chat_match():
    check = ModeCallbackRouterService._is_explicit_session_access_allowed
    own = types.SimpleNamespace(chat_id=OWNER_CHAT)
    foreign = types.SimpleNamespace(chat_id=ATTACKER_CHAT)

    no_api = types.SimpleNamespace()
    assert check(bot_app=no_api, chat_id=OWNER_CHAT, resolved=own) is True
    assert check(bot_app=no_api, chat_id=OWNER_CHAT, resolved=foreign) is False

    # Двойник с одним лишь is_admin: не-админ всё равно должен добраться до
    # своей сессии, а до чужой - нет.
    admin_only = types.SimpleNamespace(is_admin=lambda _chat_id: False)
    assert check(bot_app=admin_only, chat_id=OWNER_CHAT, resolved=own) is True
    assert check(bot_app=admin_only, chat_id=OWNER_CHAT, resolved=foreign) is False


# ---------------------------------------------------------------------------
# Тот же разрыв chat_id/owner_chat_id ниже по потоку: run-операции
# (doctor/recover/resume/apply_recommendation) проверяются RunOperationsPolicy,
# и ей тоже нужен личный id пользователя, а не чат сообщения.
# ---------------------------------------------------------------------------

class _RunOperationsSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def recover_run(self, *, session, mode_id, context, dest):
        self.calls.append("recover")
        return types.SimpleNamespace(message="recovered")


@pytest.mark.asyncio
async def test_recover_denies_group_member_when_group_id_is_in_admlist(env):
    """recover - admin-only операция. Если id супергруппы попал в admlist,
    обычный участник не должен получать её на СВОЕЙ же сессии."""
    env.app.config.telegram.admlist_chat_ids = [ADMIN_CHAT, GROUP_CHAT]
    spy = _RunOperationsSpy()
    env.app.mode_run_operations = spy
    env.app.resolve_telegram_callback_scope = lambda _query: (
        GROUP_CHAT, None, ATTACKER_CHAT, env.attacker_session,
    )
    data = f"ma:agent:recover:s={session_runtime_uid(env.attacker_session)}"
    query = _query(chat_id=GROUP_CHAT, user_id=222)

    res = await env.handler._cb_mode_action(data=data, chat_id=GROUP_CHAT, query=query, context=MagicMock())

    assert res is True
    assert spy.calls == []
    assert env.replies
    assert env.replies[-1] == "Run-операция запрещена policy: admin_required."


@pytest.mark.asyncio
async def test_recover_allows_admin_from_group_topic(env):
    """Обратная сторона: настоящий админ, пишущий из группового топика, не
    должен получать отказ из-за того, что id группы в admlist не значится."""
    spy = _RunOperationsSpy()
    env.app.mode_run_operations = spy
    _arm_group_scope(env, owner_chat_id=ADMIN_CHAT)
    data = f"ma:agent:recover:s={session_runtime_uid(env.owner_session)}"
    query = _query(chat_id=GROUP_CHAT, user_id=999)

    res = await env.handler._cb_mode_action(data=data, chat_id=GROUP_CHAT, query=query, context=MagicMock())

    assert res is True
    assert spy.calls == ["recover"]
    assert DENIED_TEXT not in env.replies


# ---------------------------------------------------------------------------
# Тот же разрыв внутри самого режима: agent-режим проверял `is_admin` по
# chat_id из ctx (чат сообщения), а не по владельцу запроса.
# ---------------------------------------------------------------------------

class _DirsFlowSpy:
    def __init__(self) -> None:
        self.starts: list[str] = []

    async def start_flow(self, *, chat_id, context, root, mode_token):
        self.starts.append(str(root))


def _arm_agent_dirs_spy(env):
    spy = _DirsFlowSpy()
    agent_mode = env.app.mode_registry.get("agent")
    agent_mode._dirs_flow = lambda: spy
    return spy


@pytest.mark.asyncio
async def test_project_connect_denies_admin_file_picker_to_group_member(env):
    """Админ выбирает каталог из всего дерева, обычный пользователь - только из
    своих user_workdirs. id группы в admlist не должен делать админом её
    участника, работающего со своей же сессией."""
    env.app.config.telegram.admlist_chat_ids = [ADMIN_CHAT, GROUP_CHAT]
    spy = _arm_agent_dirs_spy(env)
    env.app.resolve_telegram_callback_scope = lambda _query: (
        GROUP_CHAT, None, ATTACKER_CHAT, env.attacker_session,
    )
    sk = agent_project_session_key(env.attacker_session)
    data = f"ma:agent:project_connect:s={session_runtime_uid(env.attacker_session)}|sk={sk}"
    query = _query(chat_id=GROUP_CHAT, user_id=222)

    res = await env.handler._cb_mode_action(data=data, chat_id=GROUP_CHAT, query=query, context=MagicMock())

    assert res is True
    assert spy.starts == []


@pytest.mark.asyncio
async def test_project_connect_allows_admin_file_picker_from_group_topic(env):
    """Обратная сторона: настоящий админ из группового топика должен получить
    полноценный file-picker, а не список чужих user_workdirs."""
    spy = _arm_agent_dirs_spy(env)
    _arm_group_scope(env, owner_chat_id=ADMIN_CHAT)
    sk = agent_project_session_key(env.owner_session)
    data = f"ma:agent:project_connect:s={session_runtime_uid(env.owner_session)}|sk={sk}"
    query = _query(chat_id=GROUP_CHAT, user_id=999)

    res = await env.handler._cb_mode_action(data=data, chat_id=GROUP_CHAT, query=query, context=MagicMock())

    assert res is True
    assert spy.starts == [env.app.config.defaults.workdir]


@pytest.mark.asyncio
async def test_project_pick_lists_projects_of_owner_not_reply_chat(env):
    """Список каталогов для выбора берётся из user_workdirs владельца запроса.
    У супергруппы своих user_workdirs нет, и если считать их по чату ответа,
    список окажется пустым - выбор упрётся в "каталог недоступен"."""
    _arm_group_scope(env, owner_chat_id=OWNER_CHAT)
    owner_project = env.app.user_projects(OWNER_CHAT)[0]
    assert env.app.user_projects(GROUP_CHAT) == []
    sk = agent_project_session_key(env.owner_session)
    data = f"ma:agent:project_pick:s={session_runtime_uid(env.owner_session)}|sk={sk}|idx=0"
    query = _query(chat_id=GROUP_CHAT, user_id=111)

    res = await env.handler._cb_mode_action(data=data, chat_id=GROUP_CHAT, query=query, context=MagicMock())

    assert res is True
    assert env.owner_session.project_root == os.path.realpath(owner_project)


# ---------------------------------------------------------------------------
# Ререндер меню режима: набор кнопок тоже считается по владельцу запроса.
# ---------------------------------------------------------------------------

class _MenuSpy:
    def __init__(self) -> None:
        self.keyboards: list = []

    async def send_or_edit(self, *, query=None, chat_id=None, text="", md2=True, reply_markup=None, **_kwargs):
        self.keyboards.append(reply_markup)
        return None

    @property
    def last_callback_datas(self) -> list[str]:
        keyboard = self.keyboards[-1]
        return [
            str(getattr(btn, "callback_data", "") or "")
            for row in getattr(keyboard, "inline_keyboard", [])
            for btn in row
        ]


@pytest.mark.asyncio
async def test_rerender_menu_builds_buttons_for_requester_not_session_chat(env):
    """Сессия привязана к супергруппе, id которой добавлен в admlist. Обычный
    участник группы админом от этого не становится: админских кнопок режима в
    его меню быть не должно."""
    env.app.config.telegram.admlist_chat_ids = [ADMIN_CHAT, GROUP_CHAT]
    group_session = env.app.manager.create(GROUP_CHAT, "dummy", env.app.config.defaults.workdir)
    set_active_mode(group_session, "agent")
    agent_mode = env.app.mode_registry.get("agent")
    spy = _MenuSpy()
    agent_mode._messaging = lambda **_kwargs: spy

    member_query = _query(chat_id=GROUP_CHAT, user_id=ATTACKER_CHAT)
    await agent_mode._rerender_menu(env.app, group_session, GROUP_CHAT, MagicMock(), member_query)

    member_actions = spy.last_callback_datas
    assert member_actions
    assert not [d for d in member_actions if ":clean_all" in d or ":clean_session" in d or ":plugins" in d]

    # Обратная сторона: настоящему админу те же кнопки остаются доступны.
    admin_query = _query(chat_id=GROUP_CHAT, user_id=ADMIN_CHAT)
    await agent_mode._rerender_menu(env.app, group_session, GROUP_CHAT, MagicMock(), admin_query)

    admin_actions = spy.last_callback_datas
    assert [d for d in admin_actions if ":clean_session" in d]
