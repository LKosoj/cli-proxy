import asyncio
import types

from modes.analyst.mode import AnalystMode
from modes.sdk.services.tooling import ModeToolingService


def test_analyst_gate_builds_meaningful_options_before_ask_user(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        captured = {}

        async def _fake_execute(name, args, ctx):
            captured["name"] = name
            captured["args"] = dict(args)
            captured["ctx"] = dict(ctx)
            return {"success": True, "output": {"selected_option": "Только bot"}}

        async def _unexpected_chat_completion(*_args, **_kwargs):
            raise AssertionError("gate ask_user should not require a rewrite LLM call for a valid seed question")

        monkeypatch.setattr("modes.analyst.mode.chat_completion", _unexpected_chat_completion)

        mode = AnalystMode()
        mode.initialize(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(
                    openai_api_key="k",
                    openai_model="m",
                )
            ),
            services={"tooling": ModeToolingService(execute_tool_fn=_fake_execute)},
        )
        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            analyst_source_user_text_runtime="Подготовь ТЗ по доработке backend-обработчика",
        )
        bot_app = types.SimpleNamespace()

        ask_fn = mode._build_ask_fn(
            session=session,
            bot_app=bot_app,
            context=object(),
            dest={"chat_id": 1, "chat_type": "private"},
        )
        answer = await ask_fn("Есть ли ограничения по совместимости или desktop-пути, которые нельзя нарушать?")

        assert answer == "Только bot"
        assert captured["name"] == "ask_user"
        assert captured["args"]["question"] == "Есть ли ограничения по совместимости или desktop-пути, которые нельзя нарушать?"
        assert captured["args"]["options"] == ["Только bot", "Bot и desktop", "Все клиенты"]
        assert captured["args"]["allow_custom"] is True
        assert captured["args"]["system_options"] is False

    asyncio.run(_run())
