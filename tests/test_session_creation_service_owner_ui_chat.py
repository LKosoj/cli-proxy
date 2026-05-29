from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.session_creation_service import SessionCreationService
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import ChatUiState


class _Manager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, chat_id: int, tool_name: str | None, workdir: str):
        session = SimpleNamespace(
            id=f"s{len(self.calls) + 1}",
            chat_id=int(chat_id),
            tool=SimpleNamespace(name=tool_name),
        )
        self.calls.append(
            {
                "chat_id": int(chat_id),
                "tool_name": tool_name,
                "workdir": str(workdir),
                "session": session,
            }
        )
        return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_chat_id", "ui_chat_id", "message_thread_id"),
    [
        (42, -100777000111, 101),
        (21, 21, None),
    ],
)
async def test_session_creation_service_separates_owner_and_ui_scope(
    tmp_path,
    owner_chat_id: int,
    ui_chat_id: int,
    message_thread_id: int | None,
) -> None:
    manager = _Manager()
    ui_state = ChatUiState()
    ui_key = TelegramUiKey.from_parts(ui_chat_id, message_thread_id)
    bot_app = SimpleNamespace(
        config=SimpleNamespace(
            tools={"dummy": object()},
            defaults=SimpleNamespace(workdir=str(tmp_path)),
        ),
        _is_tool_available=lambda _tool: True,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
        ui_state=ui_state,
        is_within_root=lambda path, root: True,
        manager=manager,
    )
    service = SessionCreationService(bot_app)

    error = service.begin_new_session_flow(
        owner_chat_id,
        "dummy",
        message_thread_id=message_thread_id,
        ui_chat_id=ui_chat_id,
    )

    assert error is None
    assert ui_state.pending_new_tool[ui_key] == "dummy"

    service.mark_git_clone_pending(
        owner_chat_id,
        str(tmp_path),
        message_thread_id=message_thread_id,
        ui_chat_id=ui_chat_id,
    )
    assert service.pop_git_clone_pending(
        owner_chat_id,
        message_thread_id=message_thread_id,
        ui_chat_id=ui_chat_id,
    ) == str(tmp_path)

    ui_state.dirs_mode[ui_key] = "new_session"

    session, error = await service.create_from_pending_tool(
        owner_chat_id,
        str(tmp_path),
        root=str(tmp_path),
        clear_dirs_mode=True,
        message_thread_id=message_thread_id,
        ui_chat_id=ui_chat_id,
    )

    assert error is None
    assert session.chat_id == owner_chat_id
    assert manager.calls == [
        {
            "chat_id": owner_chat_id,
            "tool_name": "dummy",
            "workdir": str(tmp_path),
            "session": session,
        }
    ]
    assert ui_state.pending_new_tool == {}
    assert ui_state.dirs_mode == {}
