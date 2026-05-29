from types import SimpleNamespace

import asyncio

from agent.plugins.brainstorm import BrainstormTool


class _FakeCompletions:
    def __init__(self, client):
        self._client = client

    async def create(self, *, model, messages, temperature, max_tokens):
        return await self._client._create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )


class _FakeChat:
    def __init__(self, client):
        self.completions = _FakeCompletions(client)


class FakeOpenAIClient:
    def __init__(self, *, fail_first: bool = False):
        self.fail_first = fail_first
        self.calls: list[str] = []
        self._n = 0
        self.chat = _FakeChat(self)

    async def _create(self, *, model, messages, temperature, max_tokens):
        self.calls.append(model)
        self._n += 1
        if self.fail_first and self._n == 1:
            raise RuntimeError("boom")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="report"))]
        )


def test_synthesize_uses_big_model_first(monkeypatch):
    async def _run():
        tool = BrainstormTool()

        # Make model selection explicit so we can assert which one was used.
        monkeypatch.setattr(
            tool,
            "_get_model",
            lambda model_type="standard": ("BIG" if model_type == "big" else "STD"),
        )

        client = FakeOpenAIClient()
        monkeypatch.setattr(tool, "_get_client", lambda: client)

        report = await tool._synthesize(
            "topic",
            [
                {
                    "success": True,
                    "method": "M",
                    "method_key": "m",
                    "description": "D",
                    "model": "STD",
                    "model_type": "standard",
                    "temperature": 0.8,
                    "content": "ideas",
                }
            ],
        )

        assert report == "report"
        assert client.calls[:1] == ["BIG"]

    asyncio.run(_run())


def test_synthesize_falls_back_to_standard_model(monkeypatch):
    async def _run():
        tool = BrainstormTool()

        monkeypatch.setattr(
            tool,
            "_get_model",
            lambda model_type="standard": ("BIG" if model_type == "big" else "STD"),
        )

        client = FakeOpenAIClient(fail_first=True)
        monkeypatch.setattr(tool, "_get_client", lambda: client)

        report = await tool._synthesize(
            "topic",
            [
                {
                    "success": True,
                    "method": "M",
                    "method_key": "m",
                    "description": "D",
                    "model": "STD",
                    "model_type": "standard",
                    "temperature": 0.8,
                    "content": "ideas",
                }
            ],
        )

        assert report == "report"
        assert client.calls == ["BIG", "STD"]

    asyncio.run(_run())
