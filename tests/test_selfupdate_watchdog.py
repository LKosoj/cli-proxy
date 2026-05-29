from app.services import selfupdate_watchdog as w


def test_watchdog_send_message_returns_false_for_empty_token() -> None:
    assert w._send_telegram_message("", chat_id=1, text="x") is False
