import types

from modes.webmaster.ui import build_webmaster_menu


def test_build_webmaster_menu_enabled():
    session = types.SimpleNamespace(modes=types.SimpleNamespace(active_mode="webmaster", analyst_mode="spec"))
    text, markup = build_webmaster_menu(session)
    assert "Режим: включен" in text
    assert len(markup.inline_keyboard) == 6
    assert markup.inline_keyboard[0][0].text == "🔴 Выключить вебмастер"
    assert any(
        button.callback_data == "ma:webmaster:promote_skills"
        for row in markup.inline_keyboard
        for button in row
    )


def test_build_webmaster_menu_disabled():
    session = types.SimpleNamespace(modes=types.SimpleNamespace(active_mode="other_mode", analyst_mode="spec"))
    text, markup = build_webmaster_menu(session)
    assert "Режим: выключен" in text
    assert len(markup.inline_keyboard) == 2
    assert markup.inline_keyboard[0][0].text == "🟢 Включить вебмастер"
