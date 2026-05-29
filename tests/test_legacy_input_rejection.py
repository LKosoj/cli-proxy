from unittest.mock import MagicMock

import pytest
from desktop.services.application_facade import ApplicationFacade


def _build_facade() -> ApplicationFacade:
    return ApplicationFacade(
        config_service=MagicMock(),
        session_service=MagicMock(),
        task_service=MagicMock(),
        git_service=None,
        mode_registry_service=None,
    )


@pytest.mark.asyncio
async def test_show_mode_menu_requires_session_uid_only_signature() -> None:
    facade = _build_facade()

    with pytest.raises(TypeError, match=r"show_mode_menu expects \(session_uid\)"):
        await facade.show_mode_menu("desktop:chat", "desktop:sess-1")


@pytest.mark.asyncio
async def test_run_session_input_requires_session_uid_only_signature() -> None:
    facade = _build_facade()

    with pytest.raises(TypeError, match=r"run_session_input expects \(session_uid, text\)"):
        await facade.run_session_input("desktop:chat", "desktop:sess-1", "hello")
