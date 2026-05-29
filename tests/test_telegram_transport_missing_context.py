import logging

import pytest

from app.services.telegram_transport import TelegramTransportContext
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig


def _build_app(tmp_path) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
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
    app.notification_queue_service = None
    return app


def _restore_caplog_handler(caplog) -> None:
    root = logging.getLogger()
    if caplog.handler not in root.handlers:
        root.addHandler(caplog.handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("md2", [True, False])
async def test_send_message_missing_context_is_contract_noop(tmp_path, caplog, md2) -> None:
    app = _build_app(tmp_path)
    _restore_caplog_handler(caplog)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.telegram_transport"):
            result = await app._send_message(None, chat_id=1, text="hello", md2=md2)

        assert result is None
        assert app._last_delivery_error == "send_message contract error: Telegram bot context is required"
        assert any("reason=missing_bot_context" in record.getMessage() for record in caplog.records)
        assert not any("Не удалось отправить сообщение в Telegram" in record.getMessage() for record in caplog.records)
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_send_message_transport_context_missing_raw_context_is_contract_noop(tmp_path, caplog) -> None:
    app = _build_app(tmp_path)
    _restore_caplog_handler(caplog)
    transport_context = TelegramTransportContext(
        raw_context=None,
        chat_id=123,
        session_uid="telegram:session:123",
    )
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.telegram_transport"):
            result = await app.transport_service.send_message(
                transport_context,
                text="hello",
                md2=True,
            )

        assert result is None
        assert app._last_delivery_error == "send_message contract error: Telegram bot context is required"
        assert any("chat_id=123" in record.getMessage() for record in caplog.records)
        assert any("session_uid=telegram:session:123" in record.getMessage() for record in caplog.records)
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_send_document_missing_context_is_contract_noop(tmp_path, caplog) -> None:
    app = _build_app(tmp_path)
    _restore_caplog_handler(caplog)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.telegram_transport"):
            result = await app._send_document(None, chat_id=1, document="fake-document")

        assert result is False
        assert app._last_delivery_error == "send_document contract error: Telegram bot context is required"
        assert any("operation=send_document" in record.getMessage() for record in caplog.records)
        assert not any("Не удалось отправить файл в Telegram" in record.getMessage() for record in caplog.records)
    finally:
        app.shutdown_html_process_pool()
