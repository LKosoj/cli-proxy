import pytest
import asyncio
from unittest.mock import MagicMock, patch
from desktop.widgets.git_panel import GitPanelWidget


@pytest.fixture
def mock_facade():
    facade = MagicMock()
    facade.git_service = MagicMock()
    return facade


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.id = "test-session"
    session.workdir = "/tmp/test-repo"
    return session


@pytest.mark.asyncio
async def test_git_panel_init(qtbot, mock_facade):
    """Проверка инициализации GitPanelWidget."""
    widget = GitPanelWidget(mock_facade)
    qtbot.addWidget(widget)

    assert widget.isEnabled() is False


@pytest.mark.asyncio
async def test_git_panel_set_session(qtbot, mock_facade, mock_session):
    """Проверка активации панели при выборе сессии."""
    async def mock_status(*args, **kwargs):
        return "OK"

    mock_facade.git_service.status_text = mock_status
    widget = GitPanelWidget(mock_facade)
    qtbot.addWidget(widget)

    widget.set_session(mock_session)
    assert widget.isEnabled() is True
    assert widget._active_session == mock_session

    # Ждем завершения фоновой задачи
    for _ in range(20):
        if widget.status_display.toPlainText() == "OK":
            break
        await asyncio.sleep(0.1)

    assert widget.status_display.toPlainText() == "OK"


@pytest.mark.asyncio
async def test_git_panel_refresh_status(qtbot, mock_facade, mock_session):
    """Проверка обновления статуса."""
    status_text = "On branch main\nClean"

    async def mock_status(*args, **kwargs):
        return status_text

    mock_facade.git_service.status_text = mock_status
    widget = GitPanelWidget(mock_facade)
    qtbot.addWidget(widget)
    widget.set_session(mock_session)

    # Ждем обновления UI
    for _ in range(20):
        if widget.status_display.toPlainText() == status_text:
            break
        await asyncio.sleep(0.1)

    assert widget.status_display.toPlainText() == status_text


@pytest.mark.asyncio
async def test_git_panel_on_operation_finished_success(qtbot, mock_facade):
    """Проверка обработки успешного завершения операции."""
    widget = GitPanelWidget(mock_facade)
    qtbot.addWidget(widget)
    widget.commit_msg_input.setText("test commit")

    with patch('PySide6.QtWidgets.QMessageBox.information') as mock_msg:
        with patch.object(widget, 'refresh_status') as mock_refresh:
            # Прямой вызов слота имитирует завершение операции
            widget._on_operation_finished((0, "Commit successful"))
            qtbot.waitUntil(lambda: mock_msg.called, timeout=1000)
            qtbot.waitUntil(lambda: mock_refresh.called, timeout=1000)
            assert widget.commit_msg_input.text() == ""


@pytest.mark.asyncio
async def test_git_panel_on_operation_finished_error(qtbot, mock_facade):
    """Проверка обработки ошибки операции."""
    widget = GitPanelWidget(mock_facade)
    qtbot.addWidget(widget)

    with patch('PySide6.QtWidgets.QMessageBox.critical') as mock_msg:
        widget._on_operation_finished((1, "Error details"))
        qtbot.waitUntil(lambda: mock_msg.called, timeout=1000)
        # QMessageBox.critical(parent, title, text)
        args, kwargs = mock_msg.call_args
        assert args[1] == "Git Error"
