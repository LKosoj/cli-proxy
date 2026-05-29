import asyncio
import json
import pytest
from desktop.services.application_facade import ApplicationFacade
from desktop.services.desktop_state_service import DesktopUiStateService
from app.services.config_service import AppRuntimeParams


class MockConfigService:
    async def load(self):
        return None

    async def validate_required_secrets(self, cfg):
        return []

    async def resolve_runtime_params(self, cfg):
        return None


class MockSessionService:
    pass


class MockTaskService:
    pass


@pytest.mark.asyncio
async def test_desktop_state_service_loading_flow(tmp_path):
    # Setup paths
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    state_file = state_dir / "desktop_state.json"

    # Pre-create state file
    initial_data = {
        "active_tab": "settings",
        "sidebar_collapsed": True,
        "recent_sessions": ["session1", "session2"]
    }
    with open(state_file, "w") as f:
        json.dump(initial_data, f)

    # Setup facade
    facade = ApplicationFacade(
        config_service=MockConfigService(),
        session_service=MockSessionService(),
        task_service=MockTaskService()
    )
    # Inject runtime params manually for test
    facade.runtime_params = AppRuntimeParams(
        config_path="config.yaml",
        workdir=str(tmp_path),
        state_path=str(state_dir / "state.json"),
        desktop_state_path=str(state_file),
        toolhelp_path="toolhelp.json",
        log_path="bot.log"
    )

    service = DesktopUiStateService(facade)

    # Check that it's not ready yet
    assert not service.is_ready

    # Simulate facade startup:ready
    facade.notify("startup:ready")

    # Wait for service to load
    await asyncio.wait_for(service.wait_ready(), timeout=1.0)

    assert service.is_ready
    assert service.state.active_tab == "settings"
    assert service.state.sidebar_collapsed is True
    assert service.state.recent_sessions == ["session1", "session2"]


@pytest.mark.asyncio
async def test_desktop_state_service_save(tmp_path):
    state_file = tmp_path / "desktop_state.json"
    facade = ApplicationFacade(
        config_service=MockConfigService(),
        session_service=MockSessionService(),
        task_service=MockTaskService()
    )
    facade.runtime_params = AppRuntimeParams(
        config_path="config.yaml",
        workdir=str(tmp_path),
        state_path=str(tmp_path / "state.json"),
        desktop_state_path=str(state_file),
        toolhelp_path="toolhelp.json",
        log_path="bot.log"
    )

    service = DesktopUiStateService(facade)
    # Manually load (bypass notification for controlled test)
    await service.load()

    await service.save(active_tab="git", theme="dark")

    # Verify file content
    with open(state_file, "r") as f:
        data = json.load(f)
        assert data["active_tab"] == "git"
        assert data["theme"] == "dark"

    assert service.state.active_tab == "git"
    assert service.state.theme == "dark"


@pytest.mark.asyncio
async def test_desktop_state_service_optimization_and_filtering(tmp_path):
    state_file = tmp_path / "desktop_state.json"
    # File with extra unknown fields
    initial_data = {
        "active_tab": "chat",
        "unknown_field": "should be ignored",
        "window_geometry": "fake-base64"
    }
    with open(state_file, "w") as f:
        json.dump(initial_data, f)

    facade = ApplicationFacade(
        config_service=MockConfigService(),
        session_service=MockSessionService(),
        task_service=MockTaskService()
    )
    facade.runtime_params = AppRuntimeParams(
        config_path="config.yaml",
        workdir=str(tmp_path),
        state_path=str(tmp_path / "state.json"),
        desktop_state_path=str(state_file),
        toolhelp_path="toolhelp.json",
        log_path="bot.log"
    )

    service = DesktopUiStateService(facade)
    await service.load()

    assert service.state.active_tab == "chat"
    assert service.state.window_geometry == "fake-base64"
    # No error happened despite unknown_field
