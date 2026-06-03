"""Tests for Desktop i18n (T5): locale keys, widget retranslate_ui, facade.ui_language."""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from i18n import t, FALLBACK_LANG


# ---------------------------------------------------------------------------
# QApplication fixture (offscreen)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _close_coro(coro, **_kwargs):
    """ensure_async stand-in: close the coroutine instead of scheduling it.

    Tests that only assert the synchronous side effects of _on_language_save
    don't need to run the async save; closing the coroutine avoids a
    'coroutine was never awaited' RuntimeWarning at GC time.
    """
    coro.close()
    return None


# ---------------------------------------------------------------------------
# 6.1  Базовые тесты i18n
# ---------------------------------------------------------------------------

LOCALES_DIR = Path(__file__).parent.parent / "locales"


def _load_flat(lang: str) -> set:
    """Flatten nested JSON keys to dot-separated paths."""
    data = json.loads((LOCALES_DIR / f"{lang}.json").read_text())

    def _flatten(d, prefix=""):
        keys = set()
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys |= _flatten(v, full)
            else:
                keys.add(full)
        return keys

    return _flatten(data)


def test_desktop_keys_present_in_all_catalogs():
    """All desktop.* keys from ru.json are present in en/zh/de."""
    ru_keys = {k for k in _load_flat("ru") if k.startswith("desktop")}
    assert ru_keys, "No desktop.* keys found in ru.json"
    for lang in ("en", "zh", "de"):
        lang_keys = {k for k in _load_flat(lang) if k.startswith("desktop")}
        missing = ru_keys - lang_keys
        assert not missing, f"Keys missing in {lang}.json: {missing}"


def test_desktop_t_returns_translation():
    """i18n.t('desktop.btn.save', 'en') returns non-empty string, not the key."""
    result = t("desktop.btn.save", "en")
    assert result and result != "desktop.btn.save"


def test_desktop_t_fallback_to_ru():
    """i18n.t('desktop.btn.save', 'fr') falls back to ru variant."""
    result_fr = t("desktop.btn.save", "fr")
    result_ru = t("desktop.btn.save", "ru")
    assert result_fr == result_ru


_TECH_WHITELIST = {"Git", "SSH", "CLI", "Mode", "YAML", "JSON", "URL", "API", "ID", "OK"}


def _has_cyrillic(s: str) -> bool:
    return any("Ѐ" <= c <= "ӿ" for c in s)


def _get_nested(d, path):
    parts = path.split(".")
    v = d
    for p in parts:
        if not isinstance(v, dict):
            return None
        v = v.get(p)
    return v


def test_zh_translations_not_russian():
    """zh.json desktop keys with Cyrillic source are not Russian copies."""
    ru_data = json.loads((LOCALES_DIR / "ru.json").read_text())
    zh_data = json.loads((LOCALES_DIR / "zh.json").read_text())

    checked = 0
    for key in sorted(_load_flat("ru")):
        if not key.startswith("desktop"):
            continue
        ru_val = _get_nested(ru_data, key)
        zh_val = _get_nested(zh_data, key)
        if isinstance(ru_val, str) and isinstance(zh_val, str) and _has_cyrillic(ru_val):
            # Pure-tech tokens that are language-neutral are allowed to be identical
            stripped = ru_val.strip()
            if stripped in _TECH_WHITELIST:
                continue
            assert zh_val != ru_val, f"zh.json key {key!r} is a Russian copy: {zh_val!r}"
            checked += 1
    assert checked > 0, "No Cyrillic source keys found to compare"


def test_de_translations_not_russian():
    """de.json desktop keys with Cyrillic source are not Russian copies."""
    ru_data = json.loads((LOCALES_DIR / "ru.json").read_text())
    de_data = json.loads((LOCALES_DIR / "de.json").read_text())

    checked = 0
    for key in sorted(_load_flat("ru")):
        if not key.startswith("desktop"):
            continue
        ru_val = _get_nested(ru_data, key)
        de_val = _get_nested(de_data, key)
        if isinstance(ru_val, str) and isinstance(de_val, str) and _has_cyrillic(ru_val):
            stripped = ru_val.strip()
            if stripped in _TECH_WHITELIST:
                continue
            assert de_val != ru_val, f"de.json key {key!r} is a Russian copy: {de_val!r}"
            checked += 1
    assert checked > 0, "No Cyrillic source keys found to compare"


# ---------------------------------------------------------------------------
# 6.4  facade.ui_language
# ---------------------------------------------------------------------------

def _make_facade_with_lang(lang):
    from desktop.services.application_facade import ApplicationFacade
    facade = ApplicationFacade(
        config_service=MagicMock(),
        session_service=MagicMock(),
        task_service=MagicMock(),
    )
    defaults = SimpleNamespace(default_language=lang)
    facade.config = SimpleNamespace(defaults=defaults)
    return facade


def test_facade_ui_language_reads_defaults():
    """facade.ui_language returns config.defaults.default_language."""
    facade = _make_facade_with_lang("en")
    assert facade.ui_language == "en"


def test_facade_ui_language_fallback_on_unsupported():
    """facade.ui_language returns FALLBACK_LANG if value not in SUPPORTED_LANGS."""
    facade = _make_facade_with_lang("fr")
    assert facade.ui_language == FALLBACK_LANG


def test_facade_ui_language_fallback_on_none():
    """facade.ui_language returns FALLBACK_LANG if default_language is None."""
    facade = _make_facade_with_lang(None)
    assert facade.ui_language == FALLBACK_LANG


def test_facade_ui_language_no_config():
    """facade.ui_language returns FALLBACK_LANG when config is None."""
    from desktop.services.application_facade import ApplicationFacade
    facade = ApplicationFacade(
        config_service=MagicMock(),
        session_service=MagicMock(),
        task_service=MagicMock(),
    )
    facade.config = None
    assert facade.ui_language == FALLBACK_LANG


# ---------------------------------------------------------------------------
# 6.2  SessionSettingsWidget — combo + Save
# ---------------------------------------------------------------------------

def _make_settings_widget():
    facade = MagicMock()
    facade.config.tools = {}
    facade.ui_language = "ru"
    from desktop.widgets.session_settings import SessionSettingsWidget
    return SessionSettingsWidget(facade), facade


def test_session_settings_language_combo_items():
    """QComboBox contains 4 languages: ru, en, zh, de."""
    widget, _ = _make_settings_widget()
    codes = [widget.lang_combo.itemData(i) for i in range(widget.lang_combo.count())]
    assert set(codes) == {"ru", "en", "zh", "de"}


def test_session_settings_language_combo_shows_current():
    """combo shows the language from facade.ui_language when set_session is called."""
    widget, facade = _make_settings_widget()
    facade.ui_language = "en"
    session = MagicMock()
    session.name = "test"
    session.busy = False
    session.workdir = "/tmp"
    with patch("desktop.widgets.session_settings.is_ssh_remote_enabled", return_value=False), \
         patch("app.services.ssh_config_loader.ssh_remote_available", return_value=False), \
         patch("desktop.widgets.session_settings.is_orchestrator_enabled", return_value=False):
        with patch("builtins.__import__", side_effect=lambda name, *args, **kwargs: (
            type("m", (), {"ssh_remote_available": lambda p: False, "load_ssh_config": lambda p: {}})()
            if name == "app.services.ssh_config_loader" else __import__(name, *args, **kwargs)
        )):
            pass
        # Simplest approach: patch the imported module directly
        import app.services.ssh_config_loader as ssh_loader
        orig = ssh_loader.ssh_remote_available
        ssh_loader.ssh_remote_available = lambda *a, **kw: False
        try:
            widget.set_session(session)
        finally:
            ssh_loader.ssh_remote_available = orig
    assert widget.lang_combo.currentData() == "en"


def test_session_settings_save_calls_set_default_language():
    """_on_language_save triggers config_service.set_default_language with selected code."""
    widget, facade = _make_settings_widget()
    facade.config_service.set_default_language = AsyncMock()
    facade.notify = MagicMock()

    idx = widget.lang_combo.findData("de")
    widget.lang_combo.setCurrentIndex(idx)

    with patch("desktop.widgets.session_settings.ensure_async", side_effect=_close_coro) as mock_ensure:
        widget._on_language_save()
        assert mock_ensure.called


def test_session_settings_save_patches_config_in_memory():
    """After _on_language_save, facade.config.defaults.default_language equals selected lang."""
    widget, facade = _make_settings_widget()
    facade.config = SimpleNamespace(defaults=SimpleNamespace(default_language="ru"))
    facade.config_service.set_default_language = AsyncMock()
    facade.notify = MagicMock()

    idx = widget.lang_combo.findData("zh")
    widget.lang_combo.setCurrentIndex(idx)

    with patch("desktop.widgets.session_settings.ensure_async", side_effect=_close_coro):
        widget._on_language_save()

    assert facade.config.defaults.default_language == "zh"


def test_session_settings_retranslate_ui():
    """retranslate_ui('en') sets lang_label and lang_save_btn to English strings."""
    widget, _ = _make_settings_widget()
    widget.retranslate_ui("en")
    assert widget.lang_label.text() == t("desktop.settings.lang_label", "en")
    assert widget.lang_save_btn.text() == t("desktop.btn.save", "en")
    assert widget.name_label.text() == t("desktop.settings.session_name", "en")


# ---------------------------------------------------------------------------
# 6.3  MainWindow._retranslate_all
# ---------------------------------------------------------------------------

def _make_main_window_mock():
    """Build a minimal mock-based MainWindow for _retranslate_all testing."""
    from desktop.main_window import MainWindow

    facade = MagicMock()
    facade.ui_language = "ru"
    facade.theme_service.get_theme_colors.return_value = {}
    facade.theme_service.get_main_stylesheet.return_value = ""

    ui_state = MagicMock()
    ui_state.state = SimpleNamespace(
        active_tab="chat",
        session_panel_visible=True,
        command_palette_last_query="",
        command_palette_recent=[],
        theme="dark",
        window_geometry=None,
        window_state=None,
        context_panel_visible=False,
        context_panel_tool="git",
    )

    with patch.object(MainWindow, "_setup_ui", lambda self: None), \
         patch.object(MainWindow, "_restore_state", MagicMock()):
        w = MainWindow.__new__(MainWindow)
        w.facade = facade
        w.ui_state_service = ui_state
        w.logger = MagicMock()
        w._nav_buttons = {}

    return w, facade


def test_main_window_retranslate_all_calls_widget_retranslate():
    """_retranslate_all(lang) calls retranslate_ui(lang) for each widget with the method."""
    from desktop.main_window import MainWindow

    child1 = MagicMock()
    child1.retranslate_ui = MagicMock()
    child2 = MagicMock(spec=[])  # no retranslate_ui

    # Build a plain namespace that mimics MainWindow's attributes
    w = types.SimpleNamespace(
        logger=MagicMock(),
        _nav_buttons={},
        admin_page=None,
        session_manager=child1,
        session_settings_panel=child2,
        session_settings_page=child2,
        git_panel=child2,
        chat_view=child2,
        files_page=child2,
        log_viewer=child2,
        status_page=child2,
        scheduler_page=child2,
        reports_page=child2,
        plugins_page=child2,
        task_progress=child2,
        mode_panel=child2,
        mode_menu=child2,
        context_task_queue=child2,
        context_run_operations=child2,
        toggle_sessions_btn=MagicMock(),
        toggle_git_btn=MagicMock(),
        toggle_tasks_btn=MagicMock(),
        toggle_runs_btn=MagicMock(),
        toggle_session_settings_btn=MagicMock(),
        open_palette_btn=MagicMock(),
        statusBar=MagicMock(return_value=MagicMock()),
        _refresh_command_palette=MagicMock(),
    )

    with patch("desktop.main_window.t", return_value="x"):
        MainWindow._retranslate_all(w, "en")

    child1.retranslate_ui.assert_called_once_with("en")


def test_main_window_retranslate_all_does_not_raise_on_widget_error():
    """If one widget throws in retranslate_ui, others still get called."""
    from desktop.main_window import MainWindow

    w = MagicMock(spec=MainWindow)
    w.logger = MagicMock()
    w._nav_buttons = {}
    w.admin_page = None

    bad_widget = MagicMock()
    bad_widget.retranslate_ui = MagicMock(side_effect=RuntimeError("boom"))
    good_widget = MagicMock()
    good_widget.retranslate_ui = MagicMock()

    for attr in ("session_manager", "session_settings_panel", "session_settings_page",
                 "git_panel", "chat_view", "files_page", "log_viewer", "status_page",
                 "scheduler_page", "reports_page", "plugins_page", "task_progress",
                 "mode_panel", "mode_menu", "context_task_queue", "context_run_operations"):
        setattr(w, attr, bad_widget if attr == "session_manager" else good_widget)

    w.toggle_sessions_btn = MagicMock()
    w.toggle_git_btn = MagicMock()
    w.toggle_tasks_btn = MagicMock()
    w.toggle_runs_btn = MagicMock()
    w.toggle_session_settings_btn = MagicMock()
    w.open_palette_btn = MagicMock()
    w.statusBar = MagicMock(return_value=MagicMock())
    w._refresh_command_palette = MagicMock()

    with patch("desktop.main_window.t", return_value="x"):
        MainWindow._retranslate_all(w, "de")

    good_widget.retranslate_ui.assert_called()
    w.logger.exception.assert_called()


# ---------------------------------------------------------------------------
# 6.5  Widget-specific retranslate_ui
# ---------------------------------------------------------------------------

def test_git_panel_retranslate_ui_changes_tab_titles():
    """GitPanelWidget.retranslate_ui('en') changes tab texts to English strings."""
    facade = MagicMock()
    facade.git_service = MagicMock()
    from desktop.widgets.git_panel import GitPanelWidget
    widget = GitPanelWidget(facade)
    widget.retranslate_ui("en")
    assert widget.tabs.tabText(0) == t("desktop.git.tab.status", "en")
    assert widget.tabs.tabText(1) == t("desktop.git.tab.history", "en")
    assert widget.tabs.tabText(2) == t("desktop.git.tab.commit", "en")
    assert widget.tabs.tabText(3) == t("desktop.git.tab.operations", "en")


def test_files_panel_retranslate_ui_changes_buttons():
    """FilesPanelWidget.retranslate_ui('en') changes button texts."""
    facade = MagicMock()
    from desktop.widgets.files_panel import FilesPanelWidget
    widget = FilesPanelWidget(facade)
    widget.retranslate_ui("en")
    assert widget.up_button.text() == t("desktop.btn.up", "en")
    assert widget.refresh_button.text() == t("desktop.btn.refresh", "en")
    assert widget.save_button.text() == t("desktop.btn.save", "en")


def test_session_manager_retranslate_ui_changes_new_button():
    """SessionManagerWidget.retranslate_ui('en') changes New button text."""
    facade = MagicMock()
    facade.session_service.list_desktop_sessions.return_value = []
    from desktop.widgets.session_manager import SessionManagerWidget
    widget = SessionManagerWidget(facade, actor_id="1")
    widget.retranslate_ui("en")
    assert widget.btn_new.text() == t("desktop.btn.new", "en")


def test_log_viewer_retranslate_ui_changes_tab_titles():
    """LogViewerWidget.retranslate_ui('en') changes Main Log/Filters/Tasks."""
    task_service = MagicMock()
    task_service.list_active.return_value = []
    task_service.log_bus = None
    from desktop.widgets.log_viewer import LogViewerWidget
    widget = LogViewerWidget(task_service)
    widget.retranslate_ui("en")
    assert widget.tabs.tabText(0) == t("desktop.log.tab.main", "en")
    assert widget.tabs.tabText(1) == t("desktop.log.tab.filters", "en")
    assert widget.tabs.tabText(2) == t("desktop.log.tab.tasks", "en")


def test_chat_view_retranslate_ui_changes_send_button():
    """ChatViewWidget.retranslate_ui('en') changes Send button text."""
    from desktop.widgets.chat_view import ChatViewWidget
    widget = ChatViewWidget()
    widget.retranslate_ui("en")
    assert widget.send_button.text() == t("desktop.btn.send", "en")


def test_admin_panel_retranslate_ui_exists():
    """AdminPanel has retranslate_ui method."""
    from desktop.widgets.admin_panel import AdminPanel
    assert hasattr(AdminPanel, "retranslate_ui")
    assert callable(AdminPanel.retranslate_ui)


def test_admin_locale_keys_parity():
    """desktop.admin.* keys are identical across all 4 locale files."""
    ru_admin = {k for k in _load_flat("ru") if k.startswith("desktop.admin")}
    assert ru_admin, "No desktop.admin.* keys found in ru.json"
    for lang in ("en", "zh", "de"):
        lang_admin = {k for k in _load_flat(lang) if k.startswith("desktop.admin")}
        missing = ru_admin - lang_admin
        assert not missing, f"desktop.admin.* keys missing in {lang}.json: {missing}"


def test_admin_panel_retranslate_ui_changes_tab_titles():
    """AdminPanel.retranslate_ui('en') changes tab titles to English."""
    facade = MagicMock()
    facade.session_service.list_desktop_sessions.return_value = []
    facade.subscribe.return_value = MagicMock()
    facade.get_admin_status_payload = None
    from desktop.widgets.admin_panel import AdminPanel
    panel = AdminPanel(facade)
    panel.retranslate_ui("en")
    assert panel.admin_tabs.tabText(0) == t("desktop.admin.tab.overview", "en")
    assert panel.admin_tabs.tabText(1) == t("desktop.admin.tab.operations", "en")
    assert panel.admin_tabs.tabText(2) == t("desktop.admin.tab.monitor", "en")
    assert panel.admin_tabs.tabText(3) == t("desktop.admin.tab.config", "en")
    assert panel.admin_tabs.tabText(4) == t("desktop.admin.tab.chat", "en")
    assert panel.admin_tabs.tabText(5) == t("desktop.admin.tab.autonomy", "en")
    assert panel.admin_tabs.tabText(6) == t("desktop.admin.tab.scheduler", "en")
    assert panel.enable_button.text() == t("desktop.admin.btn.enable", "en")
    assert panel.disable_button.text() == t("desktop.admin.btn.disable", "en")


# ---------------------------------------------------------------------------
# 6.6  New tests required by FIX 6
# ---------------------------------------------------------------------------

def test_session_settings_save_notifies_ui_language_changed():
    """After _on_language_save, the async inner coroutine calls set_default_language AND
    facade.notify('ui:language_changed', lang=...). This also avoids 'coroutine never awaited'."""
    widget, facade = _make_settings_widget()
    facade.config = SimpleNamespace(defaults=SimpleNamespace(default_language="ru"))
    facade.config_service.set_default_language = AsyncMock()
    facade.notify = MagicMock()

    idx = widget.lang_combo.findData("en")
    widget.lang_combo.setCurrentIndex(idx)

    captured = []

    def fake_ensure_async(coro, **_kwargs):
        captured.append(coro)
        return None

    with patch("desktop.widgets.session_settings.ensure_async", side_effect=fake_ensure_async):
        widget._on_language_save()

    assert captured, "ensure_async was not called"
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(captured[0])
    finally:
        loop.close()

    facade.config_service.set_default_language.assert_awaited_once_with("en")
    facade.notify.assert_called_once_with("ui:language_changed", lang="en")


def test_main_window_handles_ui_language_changed_event():
    """_on_facade_notification with ui:language_changed calls _retranslate_all with the lang."""
    from desktop.main_window import MainWindow

    w, facade = _make_main_window_mock()
    w._retranslate_all = MagicMock()

    note = SimpleNamespace(event="ui:language_changed", payload={"lang": "en"})
    MainWindow._on_facade_notification(w, note)

    w._retranslate_all.assert_called_once_with("en")


def test_zh_desktop_keys_have_cjk():
    """zh.json desktop keys with Cyrillic ru source actually contain CJK characters."""
    ru_data = json.loads((LOCALES_DIR / "ru.json").read_text())
    zh_data = json.loads((LOCALES_DIR / "zh.json").read_text())

    has_cjk_keys = 0
    for key in sorted(_load_flat("ru")):
        if not key.startswith("desktop"):
            continue
        ru_val = _get_nested(ru_data, key)
        zh_val = _get_nested(zh_data, key)
        if isinstance(ru_val, str) and isinstance(zh_val, str) and _has_cyrillic(ru_val):
            if any("一" <= c <= "鿿" for c in zh_val):
                has_cjk_keys += 1
    assert has_cjk_keys > 0, "No zh.json desktop keys contain CJK characters"
