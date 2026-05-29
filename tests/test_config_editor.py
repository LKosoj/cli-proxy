import pytest
import asyncio
import inspect
import os
import re
import shutil
import tempfile
from unittest.mock import MagicMock, AsyncMock, patch
import desktop.widgets.config_editor as config_editor_module
from desktop.widgets.config_editor import (
    DESKTOP_EDITABLE_CONFIG_FIELDS,
    DESKTOP_UNSUPPORTED_CONFIG_FIELDS,
    ConfigEditorWidget,
    DiffDialog,
)
from app.config_runtime.field_paths import RUNTIME_CONFIG_FIELD_PATHS
from app.services.config_apply_policy import classify_config_path
from app.services.config_service import ConfigDraftSaveResult, ConfigService
from config import AppConfig, TelegramConfig, DefaultsConfig, MCPConfig, MiniAppConfig
from qasync import QEventLoop


@pytest.fixture
def event_loop(qapp):  # noqa: F811  # intentional override for qasync Qt integration
    """Provide a QEventLoop for async tests that need Qt event processing.

    pytest-asyncio >= 0.23 warns about overriding ``event_loop``.  The canonical
    replacement (``event_loop_policy``) cannot inject a per-app QEventLoop, so
    the override is required here.  The deprecation warning is scoped to this
    file via pytestmark below.
    """
    loop = QEventLoop(qapp)
    asyncio.set_event_loop(loop)
    yield loop
    pending = asyncio.all_tasks(loop)
    if pending:
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()


# Scoped suppression: the event_loop override above is the only viable
# approach for qasync integration; silence the known pytest-asyncio warning.
pytestmark = pytest.mark.filterwarnings(
    "ignore:The event_loop fixture provided by pytest-asyncio:DeprecationWarning"
)


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture
def mock_config(temp_dir):
    cfg_path = os.path.join(temp_dir, "config.yaml")
    # Начальное содержимое для проверки diff и бэкапа
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("telegram:\n  token: old_token\n")

    cfg = MagicMock(spec=AppConfig)
    cfg.path = cfg_path
    cfg.telegram = TelegramConfig(token="old_token", whitelist_chat_ids=[1])
    cfg.defaults = DefaultsConfig(
        workdir="/tmp",
        pending_input_confirmation_enabled=True,
        cli_json_stream_archive_enabled=True,
        assistant_preview_enabled=True,
    )
    cfg.tools = {}
    cfg.mcp = MCPConfig(enabled=False)
    cfg.mcp_clients = []
    cfg.presets = []
    cfg.miniapp = MiniAppConfig(enabled=False)
    return cfg


@pytest.fixture
def mock_config_service(mock_config):
    service = MagicMock(spec=ConfigService)
    service.load = AsyncMock(return_value=mock_config)
    service.current_revision = AsyncMock(return_value="loaded-revision")
    service.serialize_config = AsyncMock(return_value="serialized_content_v_final")
    service.diff_against_disk = AsyncMock(return_value="--- config.yaml\n+++ config.yaml\n+new_diff_evidence")

    async def fake_save_config_draft_with_revision(cfg, *, expected_revision=None):
        # Эмуляция реального создания бэкапа для подтверждения критерия
        assert expected_revision == "loaded-revision"
        if os.path.exists(cfg.path):
            shutil.copy2(cfg.path, f"{cfg.path}.bak")
        restart_required, reloadable = _split_policy_fields([
            "defaults.assistant_preview_enabled",
            "defaults.cli_json_stream_archive_enabled",
            "defaults.pending_input_confirmation_enabled",
            "miniapp.bind_host",
            "miniapp.bind_port",
            "telegram.token",
        ])
        return ConfigDraftSaveResult(
            ok=True,
            revision="saved-revision",
            diff="diff",
            changed=True,
            restart_required=restart_required,
            reloadable=reloadable,
            errors=[],
            backup_path=f"{cfg.path}.bak",
        )

    service.save_config_draft_with_revision = AsyncMock(side_effect=fake_save_config_draft_with_revision)
    service.save_atomic = AsyncMock()
    return service


async def _wait_until(predicate):
    for _ in range(50):
        if predicate():
            return
        await asyncio.sleep(0.1)
    assert predicate()


def _successful_result(
    *,
    revision: str = "saved-revision",
    changed: bool = True,
    backup_path: str | None = "/tmp/config.yaml.bak",
    restart_required: list[str] | None = None,
    reloadable: list[str] | None = None,
) -> ConfigDraftSaveResult:
    return ConfigDraftSaveResult(
        ok=True,
        revision=revision,
        diff="diff" if changed else "",
        changed=changed,
        restart_required=restart_required if restart_required is not None else ["telegram.token"],
        reloadable=reloadable if reloadable is not None else ["defaults.pending_input_confirmation_enabled"],
        errors=[],
        backup_path=backup_path,
    )


def _failed_result(*, revision: str = "current-revision", errors: list[str] | None = None) -> ConfigDraftSaveResult:
    return ConfigDraftSaveResult(
        ok=False,
        revision=revision,
        diff="",
        changed=False,
        restart_required=[],
        reloadable=[],
        errors=errors or ["revision mismatch"],
        backup_path=None,
    )


async def _prepare_loaded_widget(qtbot, mock_config_service):
    widget = ConfigEditorWidget(mock_config_service)
    qtbot.addWidget(widget)
    widget.load_config()
    await _wait_until(lambda: widget._widgets.get("telegram.token") is not None)
    return widget


def _accept_diff_dialog(mock_dialog_class):
    mock_dialog_instance = mock_dialog_class.return_value
    mock_dialog_instance.exec.return_value = 1  # QDialog.Accepted


def _message_text(mock_box):
    return mock_box.call_args[0][2]


def _message_title(mock_box):
    return mock_box.call_args[0][1]


def _split_policy_fields(paths: list[str]) -> tuple[list[str], list[str]]:
    restart_required: list[str] = []
    reloadable: list[str] = []
    for path in sorted(paths):
        policy = classify_config_path(path)
        if policy.apply_mode == "restart_required":
            restart_required.append(path)
        elif policy.apply_mode == "hot_reload":
            reloadable.append(path)
    return restart_required, reloadable


@pytest.mark.asyncio
async def test_config_editor_load(qtbot, mock_config_service):
    """Проверка корректности загрузки данных в типизированные поля UI."""
    widget = ConfigEditorWidget(mock_config_service)
    widget.load_config()

    # Ожидаем асинхронную загрузку
    await _wait_until(lambda: widget._widgets.get("telegram.token") is not None)

    assert widget._widgets["telegram.token"].text() == "old_token"
    assert mock_config_service.current_revision.called
    # Проверка, что поле секретное ( EchoMode.Password )
    assert widget._widgets["telegram.token"].echoMode().name == "Password"
    assert widget._widgets["defaults.pending_input_confirmation_enabled"].isChecked() is True
    assert widget._widgets["defaults.cli_json_stream_archive_enabled"].isChecked() is True
    assert widget._widgets["defaults.assistant_preview_enabled"].isChecked() is True
    assert widget._widgets["miniapp.bind_host"].text() == "127.0.0.1"
    assert widget._widgets["miniapp.bind_port"].value() == 8088


@pytest.mark.asyncio
async def test_config_editor_does_not_expose_or_save_defaults_theme(qtbot, mock_config_service, mock_config):
    setattr(mock_config.defaults, "theme", "dark")

    widget = await _prepare_loaded_widget(qtbot, mock_config_service)

    assert "defaults.theme" not in widget._widgets
    collected = widget._collect_config()
    assert collected is not None
    assert not hasattr(collected.defaults, "theme")


@pytest.mark.asyncio
async def test_config_editor_save_flow(qtbot, mock_config_service, mock_config):
    """Проверка полного цикла: редактирование -> diff -> draft-result save."""
    widget = await _prepare_loaded_widget(qtbot, mock_config_service)

    # Редактирование через типизированное поле
    widget._widgets["telegram.token"].setText("new_token")
    widget._widgets["defaults.pending_input_confirmation_enabled"].setChecked(False)
    widget._widgets["defaults.cli_json_stream_archive_enabled"].setChecked(False)
    widget._widgets["defaults.assistant_preview_enabled"].setChecked(False)
    widget._widgets["miniapp.bind_host"].setText("0.0.0.0")
    widget._widgets["miniapp.bind_port"].setValue(8099)

    with patch("desktop.widgets.config_editor.DiffDialog") as mock_dialog_class, \
         patch("PySide6.QtWidgets.QMessageBox.information") as mock_info:
        _accept_diff_dialog(mock_dialog_class)

        widget._on_save_clicked()

        # 1. Проверка отображения diff перед подтверждением (Критерий 2)
        await _wait_until(lambda: mock_dialog_class.called)

        assert mock_dialog_class.called
        passed_diff = mock_dialog_class.call_args[0][0]
        assert "+++ config.yaml" in passed_diff
        assert "+new_diff_evidence" in passed_diff

        # 2. Ожидаем завершения сохранения
        await _wait_until(lambda: mock_config_service.save_config_draft_with_revision.called)

        assert mock_config_service.save_config_draft_with_revision.called
        assert not mock_config_service.save_atomic.called
        saved_cfg = mock_config_service.save_config_draft_with_revision.call_args[0][0]
        assert mock_config_service.save_config_draft_with_revision.call_args.kwargs == {
            "expected_revision": "loaded-revision",
        }
        assert saved_cfg.defaults.pending_input_confirmation_enabled is False
        assert saved_cfg.defaults.cli_json_stream_archive_enabled is False
        assert saved_cfg.defaults.assistant_preview_enabled is False
        assert saved_cfg.miniapp.bind_host == "0.0.0.0"
        assert saved_cfg.miniapp.bind_port == 8099

        # 3. ФИЗИЧЕСКАЯ ПРОВЕРКА создания .bak файла (Критерий 3)
        bak_path = f"{mock_config.path}.bak"
        assert os.path.exists(bak_path), f"Backup file {bak_path} must be created on disk"

        # 4. Проверка уведомления пользователя
        assert mock_info.called
        message = _message_text(mock_info)
        assert _message_title(mock_info) == "Success"
        assert "Changed: yes" in message
        assert "Backup created" in message
        assert "Restart required: miniapp.bind_host, miniapp.bind_port, telegram.token" in message
        assert (
            "Reloadable: defaults.assistant_preview_enabled, "
            "defaults.cli_json_stream_archive_enabled, "
            "defaults.pending_input_confirmation_enabled"
        ) in message
        assert widget._current_revision == "saved-revision"


@pytest.mark.asyncio
async def test_config_editor_save_result_reports_no_change(qtbot, mock_config_service):
    """No-change save still uses the shared draft-result contract."""
    widget = await _prepare_loaded_widget(qtbot, mock_config_service)
    mock_config_service.diff_against_disk = AsyncMock(return_value="")
    mock_config_service.save_config_draft_with_revision = AsyncMock(
        return_value=_successful_result(
            revision="same-revision",
            changed=False,
            backup_path=None,
            restart_required=[],
            reloadable=[],
        )
    )

    with patch("desktop.widgets.config_editor.DiffDialog") as mock_dialog_class, \
         patch("PySide6.QtWidgets.QMessageBox.information") as mock_info:
        widget._on_save_clicked()
        await _wait_until(lambda: mock_config_service.save_config_draft_with_revision.called)

        assert not mock_dialog_class.called
        assert not mock_config_service.save_atomic.called
        assert _message_title(mock_info) == "No Changes"
        message = _message_text(mock_info)
        assert "Changed: no" in message
        assert "Restart required: none" in message
        assert "Reloadable: none" in message
        assert widget._current_revision == "same-revision"


@pytest.mark.asyncio
async def test_config_editor_save_result_reports_errors(qtbot, mock_config_service):
    """Failed draft-result save exposes contract errors in Desktop UI."""
    widget = await _prepare_loaded_widget(qtbot, mock_config_service)
    mock_config_service.save_config_draft_with_revision = AsyncMock(
        return_value=_failed_result(errors=["revision mismatch"])
    )

    with patch("desktop.widgets.config_editor.DiffDialog") as mock_dialog_class, \
         patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warning:
        _accept_diff_dialog(mock_dialog_class)

        widget._on_save_clicked()
        await _wait_until(lambda: mock_config_service.save_config_draft_with_revision.called)

        assert not mock_config_service.save_atomic.called
        assert _message_title(mock_warning) == "Save Failed"
        assert "Configuration was not saved." in _message_text(mock_warning)
        assert "revision mismatch" in _message_text(mock_warning)
        assert widget._current_revision == "loaded-revision"


def test_diff_dialog_readonly_content(qapp):
    """Проверка, что DiffDialog отображает текст корректно и только для чтения."""
    content = "Unified Diff Content Evidence"
    dialog = DiffDialog(content)
    assert dialog.diff_view.toPlainText() == content
    assert dialog.diff_view.isReadOnly()


def test_desktop_config_policy_matches_widget_keys():
    """Desktop policy must list every config path edited by ConfigEditorWidget."""
    source = inspect.getsource(ConfigEditorWidget)
    literal_keys = set(re.findall(r'self\._widgets\["([^"]+)"\]\s*=', source))
    tool_suffixes = set(re.findall(r'self\._widgets\[f"\{prefix\}([^"]+)"\]\s*=', source))
    widget_keys = literal_keys | {f"tools.*.{suffix}" for suffix in tool_suffixes}

    assert widget_keys <= DESKTOP_EDITABLE_CONFIG_FIELDS
    assert DESKTOP_EDITABLE_CONFIG_FIELDS.isdisjoint(DESKTOP_UNSUPPORTED_CONFIG_FIELDS)


def test_desktop_config_policy_uses_runtime_dot_paths():
    """Desktop policy uses the shared runtime path contract."""
    assert DESKTOP_UNSUPPORTED_CONFIG_FIELDS <= RUNTIME_CONFIG_FIELD_PATHS
    assert DESKTOP_EDITABLE_CONFIG_FIELDS <= RUNTIME_CONFIG_FIELD_PATHS
    assert "defaults.theme" not in DESKTOP_EDITABLE_CONFIG_FIELDS


def test_config_editor_does_not_own_restart_policy():
    """Desktop save summary renders backend ConfigDraftSaveResult metadata."""
    source = inspect.getsource(config_editor_module)

    assert "RESTART_REQUIRED_FIELDS" not in source
    assert "RELOADABLE_FIELDS" not in source
    assert "AppRuntimeService" not in source
