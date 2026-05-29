from __future__ import annotations

from types import SimpleNamespace

from app.services import SessionMutationService


class _RecordingManager:
    def __init__(self) -> None:
        self.persist_all_calls = 0
        self.persist_session_calls: list[tuple[int, str]] = []

    def _persist_sessions(self) -> None:
        self.persist_all_calls += 1

    def persist_session(self, chat_id: int, session_id: str) -> bool:
        self.persist_session_calls.append((chat_id, session_id))
        return True


def test_session_mutation_service_contract_mutates_and_persists_session() -> None:
    manager = _RecordingManager()
    service = SessionMutationService(manager)
    session = SimpleNamespace(
        id="s1",
        chat_id=101,
        modes=SimpleNamespace(active_mode=None),
        cli=SimpleNamespace(cli_work_type=None),
        queue=[],
    )

    for method_name in (
        "persist_all",
        "persist_session",
        "set_active_mode",
        "set_cli_work_type",
        "append_queue_item",
    ):
        assert callable(getattr(service, method_name))

    assert service.persist_all() is True
    assert manager.persist_all_calls == 1

    assert service.persist_session(session) is True
    assert manager.persist_session_calls == [(101, "s1")]

    assert service.set_active_mode(session, "manager") is True
    assert session.modes.active_mode == "manager"
    assert manager.persist_session_calls[-1] == (101, "s1")

    assert service.set_cli_work_type(session, "review") is True
    assert session.cli.cli_work_type == "review"
    assert manager.persist_session_calls[-1] == (101, "s1")

    assert service.append_queue_item(
        session,
        {
            "text": "queued",
            "dest": {"kind": "telegram", "chat_id": 101, "thread": {"id": 7}},
            "image_paths": ["/tmp/a.png"],
            "attachments": ["/tmp/source.txt"],
        },
    ) is True

    assert session.queue == [
        {
            "text": "queued",
            "dest": {"kind": "telegram", "chat_id": 101, "thread": {"id": 7}},
            "image_paths": ["/tmp/a.png"],
            "attachments": ["/tmp/source.txt"],
        }
    ]
    assert manager.persist_session_calls[-1] == (101, "s1")


def test_session_mutation_service_supports_fake_session_without_manager() -> None:
    service = SessionMutationService()
    fallback_dest = {"kind": "desktop", "session_uid": "desktop:fake"}
    session = SimpleNamespace(id="fake", queue=[])

    assert service.persist_all() is False
    assert service.persist_session(session) is False

    assert service.set_active_mode(session, "agent") is True
    assert session.active_mode == "agent"

    assert service.set_cli_work_type(session, "agent-run") is True
    assert session.cli_work_type == "agent-run"

    assert service.append_queue_item(session, "legacy prompt", fallback_dest=fallback_dest) is True
    assert session.queue == [
        {
            "text": "legacy prompt",
            "dest": {"kind": "desktop", "session_uid": "desktop:fake"},
        }
    ]
    fallback_dest["session_uid"] = "changed"
    assert session.queue[0]["dest"]["session_uid"] == "desktop:fake"


def test_session_mutation_service_falls_back_to_persist_all_for_fake_owner() -> None:
    manager = _RecordingManager()
    service = SessionMutationService(manager)
    session = SimpleNamespace(id="desktop-s1", chat_id="desktop", queue=[])

    assert service.persist_session(session) is True

    assert manager.persist_session_calls == []
    assert manager.persist_all_calls == 1
