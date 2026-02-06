import asyncio

from telegram.error import NetworkError


def test_send_message_retries_and_records(monkeypatch):
    from telegram_io import TelegramIO

    calls = {"n": 0}
    recorded = []

    async def _fast_sleep(_sec: float):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    class _Bot:
        async def send_message(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise NetworkError("tmp")

            class _Msg:
                message_id = 123

            return _Msg()

    class _Ctx:
        bot = _Bot()

    io = TelegramIO(record_message=lambda chat_id, msg_id: recorded.append((chat_id, msg_id)))

    out = asyncio.run(io.send_message(_Ctx(), chat_id=777, text="hi"))
    assert out is not None
    assert calls["n"] == 3
    assert recorded == [(777, 123)]

