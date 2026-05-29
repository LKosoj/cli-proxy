import logging
from types import SimpleNamespace

from sessions.session_management import SessionManagement


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_cli_dialog_log_includes_codex_image_path_and_two_lines() -> None:
    sm = SessionManagement(bot_app=SimpleNamespace())
    logger = logging.getLogger("bot.cli_dialog")
    handler = _ListHandler()
    old_handlers = list(logger.handlers)
    old_propagate = logger.propagate
    old_level = logger.level
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        session = SimpleNamespace(tool=SimpleNamespace(name="codex"))
        sm._log_cli_dialog(
            session,
            "опиши картинку",
            "это диаграмма",
            chat_id=777,
            image_path="/tmp/diagram.png",
        )
    finally:
        logger.handlers = old_handlers
        logger.propagate = old_propagate
        logger.setLevel(old_level)

    assert len(handler.messages) == 1
    parts = handler.messages[0].splitlines()
    assert len(parts) == 2
    assert parts[0].startswith("[")
    assert "][user:777][опиши картинку [image: /tmp/diagram.png]]" in parts[0]
    assert parts[1].startswith("[")
    assert "][codex][это диаграмма]" in parts[1]


def test_cli_dialog_log_does_not_include_image_for_non_codex() -> None:
    sm = SessionManagement(bot_app=SimpleNamespace())
    logger = logging.getLogger("bot.cli_dialog")
    handler = _ListHandler()
    old_handlers = list(logger.handlers)
    old_propagate = logger.propagate
    old_level = logger.level
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        session = SimpleNamespace(tool=SimpleNamespace(name="gemini"))
        sm._log_cli_dialog(session, "что на изображении", "ответ", image_path="/tmp/img.png")
    finally:
        logger.handlers = old_handlers
        logger.propagate = old_propagate
        logger.setLevel(old_level)

    assert len(handler.messages) == 1
    parts = handler.messages[0].splitlines()
    assert len(parts) == 2
    assert "][user][" in parts[0]
    assert "][gemini][" in parts[1]
    assert "[image:" not in parts[0]


def test_cli_dialog_log_uses_chat_id_in_user_tag() -> None:
    sm = SessionManagement(bot_app=SimpleNamespace())
    logger = logging.getLogger("bot.cli_dialog")
    handler = _ListHandler()
    old_handlers = list(logger.handlers)
    old_propagate = logger.propagate
    old_level = logger.level
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        session = SimpleNamespace(tool=SimpleNamespace(name="codex"))
        sm._log_cli_dialog(session, "привет", "ответ", chat_id=12345)
    finally:
        logger.handlers = old_handlers
        logger.propagate = old_propagate
        logger.setLevel(old_level)

    assert len(handler.messages) == 1
    parts = handler.messages[0].splitlines()
    assert len(parts) == 2
    assert "][user:12345][привет]" in parts[0]
    assert "][codex][ответ]" in parts[1]
