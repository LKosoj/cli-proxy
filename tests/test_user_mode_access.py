import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


def _build_app(tmp_path, *, user_modes=None):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[999],
            user_workdirs={1: [str(tmp_path)]},
            user_modes=(user_modes or {}),
        ),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
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
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


def test_non_admin_without_user_modes_has_no_modes(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={})

    allowed = app.access_policy_service.allowed_mode_ids_for_chat(1)
    assert allowed == []
    assert app.access_policy_service.is_mode_allowed_for_chat(1, "agent") is False


def test_non_admin_with_explicit_user_modes_gets_only_listed(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: ["agent", "webmaster", "direct_cli", "orchestrator"]})

    allowed = set(app.access_policy_service.allowed_mode_ids_for_chat(1))
    assert "agent" in allowed
    assert "webmaster" in allowed
    assert "direct_cli" in allowed
    assert "orchestrator" in allowed
    assert "manager" not in allowed
    assert app.access_policy_service.is_mode_allowed_for_chat(1, "agent") is True
    assert app.access_policy_service.is_mode_allowed_for_chat(1, "manager") is False
    assert app.access_policy_service.is_direct_cli_allowed_for_chat(1) is True
    assert app.access_policy_service.is_orchestrator_allowed_for_chat(1) is True


def test_non_admin_with_all_gets_all_modes(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: "all"})

    all_modes = {mid for mid, _ in app.mode_registry_service.list_modes()} | {"direct_cli", "orchestrator"}
    allowed = set(app.access_policy_service.allowed_mode_ids_for_chat(1))
    assert allowed == all_modes


def test_non_admin_without_direct_cli_cannot_run_cli_input(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: ["agent"]})
    session = app.manager.create(1, "dummy", str(tmp_path))
    sent: list[dict] = []
    started: list[dict] = []

    async def _send_message(_context, *, chat_id: int, text: str, **kwargs):
        sent.append({"chat_id": int(chat_id), "text": str(text or ""), "kwargs": dict(kwargs)})
        return True

    def _start_prompt_task(session_obj, text, dest, context, *, task_name=""):
        _ = context
        started.append(
            {
                "session_id": str(getattr(session_obj, "id", "") or ""),
                "text": str(text or ""),
                "dest": dict(dest or {}),
                "task_name": str(task_name or ""),
            }
        )

    app._send_message = _send_message  # type: ignore[method-assign]
    app.session_management.start_prompt_task = _start_prompt_task  # type: ignore[method-assign]

    asyncio.run(app.input_dispatch_service.handle_cli_input(session, "pwd", 1, object()))

    assert started == []
    assert sent == [
        {
            "chat_id": 1,
            "text": "Прямой CLI недоступен для вашего пользователя.",
            "kwargs": {},
        }
    ]


def test_group_topic_direct_cli_policy_uses_dest_user_id(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: ["agent"]})
    session = app.manager.create(1, "dummy", str(tmp_path))
    sent: list[dict] = []
    started: list[dict] = []

    async def _send_message(_context, *, chat_id: int, text: str, **kwargs):
        sent.append({"chat_id": int(chat_id), "text": str(text or ""), "kwargs": dict(kwargs)})
        return True

    def _start_prompt_task(session_obj, text, dest, context, *, task_name=""):
        del context, task_name
        started.append({"session_id": str(getattr(session_obj, "id", "") or ""), "text": str(text or ""), "dest": dict(dest or {})})

    app._send_message = _send_message  # type: ignore[method-assign]
    app.session_management.start_prompt_task = _start_prompt_task  # type: ignore[method-assign]

    asyncio.run(
        app.input_dispatch_service.handle_cli_input(
            session,
            "pwd",
            -100777000111,
            object(),
            dest={"kind": "telegram", "chat_id": -100777000111, "user_id": 1, "message_thread_id": 77},
        )
    )

    assert started == []
    assert sent == [
        {
            "chat_id": -100777000111,
            "text": "Прямой CLI недоступен для вашего пользователя.",
            "kwargs": {"message_thread_id": 77},
        }
    ]


def test_direct_cli_presence_keeps_default_without_active_mode(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: ["agent", "direct_cli"]})

    assert app.access_policy_service.default_mode_id_for_chat(1) is None


def test_multiple_allowed_modes_choose_first_available_default(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: ["agent", "webmaster"]})

    allowed = app.access_policy_service.allowed_mode_ids_for_chat(1)
    assert allowed
    assert app.access_policy_service.default_mode_id_for_chat(1) == allowed[0]


def test_direct_cli_only_does_not_force_default_mode(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: ["direct_cli"]})

    assert app.access_policy_service.default_mode_id_for_chat(1) is None


def test_orchestrator_only_does_not_force_default_mode(tmp_path) -> None:
    app = _build_app(tmp_path, user_modes={1: ["orchestrator"]})

    assert app.access_policy_service.default_mode_id_for_chat(1) is None
