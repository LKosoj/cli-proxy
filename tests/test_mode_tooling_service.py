import pytest

from modes.sdk.services.tooling import ModeToolingService


@pytest.mark.asyncio
async def test_mode_tooling_service_ask_user_returns_custom_text_when_allowed() -> None:
    captured = {}

    async def _execute(name, args, ctx):
        captured["name"] = name
        captured["args"] = dict(args)
        captured["ctx"] = dict(ctx)
        return {"success": True, "output": {"selected_option": "Нужен детальный аудит API и очередей"}}

    service = ModeToolingService(execute_tool_fn=_execute)

    selected = await service.ask_user(
        question="Что именно нужно уточнить?",
        options=["web", "mobile"],
        ctx={"session_id": "s1"},
        allow_custom=True,
        system_options=False,
    )

    assert captured["name"] == "ask_user"
    assert captured["args"]["allow_custom"] is True
    assert selected == "Нужен детальный аудит API и очередей"


@pytest.mark.asyncio
async def test_mode_tooling_service_ask_user_rejects_custom_text_when_disallowed() -> None:
    async def _execute(_name, _args, _ctx):
        return {"success": True, "output": "User selected: Произвольный ответ"}

    service = ModeToolingService(execute_tool_fn=_execute)

    with pytest.raises(ValueError, match="invalid selection"):
        await service.ask_user(
            question="Выберите вариант",
            options=["Продолжить", "Отмена"],
            ctx={"session_id": "s1"},
            allow_custom=False,
            system_options=False,
        )
