from __future__ import annotations

import types
from modes.manager.ui import build_manager_menu_with_back


def test_manager_menu_shows_resume_button_when_paused() -> None:
    s = types.SimpleNamespace(active_mode="manager", manager_quiet_mode=False)
    text, keyboard = build_manager_menu_with_back(s, back_callback="b", back_text="back", plan_status="paused")
    assert "пауза" in text
    # Flatten buttons and ensure resume callback exists.
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    assert any(getattr(btn, "callback_data", "") == "ma:manager:resume_paused" for btn in buttons)
