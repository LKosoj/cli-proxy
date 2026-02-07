import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
import summary as summary_mod


def test_summary_uses_openai_big_model(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
                openai_api_key="k",
                openai_model="small-model",
                openai_big_model="big-model",
                openai_base_url="https://api.openai.com",
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        seen = {"model": None}

        class _FakeCompletions:
            async def create(self, *, model, messages, max_tokens=None, temperature=None, **_kwargs):
                seen["model"] = model
                # minimal OpenAI-like response shape
                msg = types.SimpleNamespace(content="ok")
                choice = types.SimpleNamespace(message=msg)
                return types.SimpleNamespace(choices=[choice])

        class _FakeChat:
            def __init__(self):
                self.completions = _FakeCompletions()

        class _FakeClient:
            def __init__(self):
                self.chat = _FakeChat()

        monkeypatch.setattr(summary_mod, "_get_openai_client", lambda *_args, **_kwargs: _FakeClient())

        text = "x" * 5000
        out, err = await summary_mod.summarize_text_with_reason(text, max_chars=500, config=cfg)
        assert err is None
        assert out
        assert seen["model"] == "big-model"

    asyncio.run(_run())
