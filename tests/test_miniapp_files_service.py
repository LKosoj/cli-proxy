import asyncio
from types import SimpleNamespace

import pytest

from app.security import SecurityFacade
from app.security.interfaces import PathValidationResult
from app.services.session_files_service import (
    SessionFilesService,
    PathDeniedError,
    PathValidationError,
    SessionNotFoundError,
    SessionUidRequiredError,
)
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from miniapp.services.files_service import FilesService


class _Manager:
    def __init__(self, session=None, sessions_by_chat=None, active_by_chat=None):
        self._session = session
        self._sessions_by_chat = sessions_by_chat or {}
        self._active_by_chat = active_by_chat or {}

    def active(self, _chat_id):
        if self._session is not None:
            return self._session
        active_id = self._active_by_chat.get(_chat_id)
        if not active_id:
            return None
        return (self._sessions_by_chat.get(_chat_id) or {}).get(active_id)

    def sessions_for_chat(self, chat_id):
        return dict(self._sessions_by_chat.get(chat_id) or {})

    def set_active(self, chat_id, session_id):
        sessions = self._sessions_by_chat.get(chat_id) or {}
        if session_id not in sessions:
            return False
        self._active_by_chat[chat_id] = session_id
        return True

    def get(self, chat_id, session_id):
        return (self._sessions_by_chat.get(chat_id) or {}).get(session_id)


def _build_app(tmp_path, session=None):
    cfg = AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(enabled=True),
    )
    return SimpleNamespace(config=cfg, manager=_Manager(session), security=SecurityFacade())


def test_miniapp_files_service_reexports_shared_session_files_service() -> None:
    assert FilesService is SessionFilesService


def test_session_files_service_contract_without_miniapp_route_context(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    file_path = root / "a.txt"
    file_path.write_text("hello needle", encoding="utf-8")
    session = SimpleNamespace(workdir=str(root))
    app = _build_app(tmp_path, session=session)
    app.manager = _Manager(session=None, sessions_by_chat={1: {"s1": session}})
    svc = SessionFilesService(app)

    tree = svc.tree(1, "1:s1", ".")
    read = svc.read(1, "1:s1", "a.txt")
    written = svc.write(1, "1:s1", "a.txt", "world needle", read["revision"])
    downloaded = svc.download(1, "1:s1", "a.txt")
    search = asyncio.run(svc.search(1, "1:s1", "needle", "."))
    created_dir = svc.create(1, "1:s1", "notes", "dir")
    created_file = svc.create(1, "1:s1", "notes/new.txt", "file")
    meta = svc.meta(1, "1:s1", "notes/new.txt")
    deleted_file = svc.delete(1, "1:s1", "notes/new.txt")
    deleted_dir = svc.delete(1, "1:s1", "notes")

    assert not hasattr(app, "route_context")
    assert [item["name"] for item in tree["items"]] == ["a.txt"]
    assert written["ok"] is True
    assert downloaded["content"] == b"world needle"
    assert search["execution_target"] == "local"
    assert search["matches"][0]["file"] == "a.txt"
    assert created_dir == {"ok": True}
    assert created_file == {"ok": True}
    assert meta["exists"] is True
    assert deleted_file == {"ok": True}
    assert deleted_dir == {"ok": True}


def test_files_service_requires_explicit_session_uid(tmp_path) -> None:
    app = _build_app(tmp_path, session=None)
    svc = FilesService(app)
    with pytest.raises(SessionUidRequiredError):
        svc.tree(1, "", ".")


def test_files_service_resolves_session_by_explicit_session_uid(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("ok", encoding="utf-8")
    restored_session = SimpleNamespace(workdir=str(root), state_updated_at=123.0)
    manager = _Manager(
        session=None,
        sessions_by_chat={1: {"s1": restored_session}},
        active_by_chat={1: None},
    )
    app = _build_app(tmp_path, session=None)
    app.manager = manager
    svc = FilesService(app)

    payload = svc.tree(1, "1:s1", ".")

    assert payload["path"] == "."
    assert manager.active(1) is None


def test_files_service_rejects_unknown_session_uid(tmp_path) -> None:
    app = _build_app(tmp_path, session=None)
    svc = FilesService(app)
    with pytest.raises(SessionNotFoundError):
        svc.tree(1, "1:s404", ".")


def test_files_service_uses_requested_session_uid_without_state_leak_between_calls(tmp_path) -> None:
    root_one = tmp_path / "work-one"
    root_two = tmp_path / "work-two"
    root_one.mkdir()
    root_two.mkdir()
    (root_one / "a.txt").write_text("one", encoding="utf-8")
    (root_two / "b.txt").write_text("two", encoding="utf-8")
    manager = _Manager(
        session=None,
        sessions_by_chat={
            1: {
                "s1": SimpleNamespace(workdir=str(root_one), state_updated_at=1.0),
                "s2": SimpleNamespace(workdir=str(root_two), state_updated_at=2.0),
            }
        },
        active_by_chat={1: None},
    )
    app = _build_app(tmp_path, session=None)
    app.manager = manager
    svc = FilesService(app)

    first = svc.tree(1, "1:s1", ".")
    second = svc.tree(1, "1:s2", ".")

    assert [item["name"] for item in first["items"]] == ["a.txt"]
    assert [item["name"] for item in second["items"]] == ["b.txt"]


def test_files_service_tree_lists_directories_before_files(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "z-dir").mkdir()
    (root / "a-dir").mkdir()
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")
    session = SimpleNamespace(workdir=str(root))
    app = _build_app(tmp_path, session=session)
    app.manager = _Manager(session=None, sessions_by_chat={1: {"s1": session}})
    svc = FilesService(app)

    payload = svc.tree(1, "1:s1", ".")

    assert [(item["name"], item["is_dir"]) for item in payload["items"]] == [
        ("a-dir", True),
        ("z-dir", True),
        ("a.txt", False),
        ("b.txt", False),
    ]


def test_files_service_blocks_traversal_and_config(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    session = SimpleNamespace(workdir=str(root))
    app = _build_app(tmp_path, session=session)
    app.manager = _Manager(session=None, sessions_by_chat={1: {"s1": session}})
    svc = FilesService(app)

    with pytest.raises(PathValidationError):
        svc.read(1, "1:s1", "../etc/passwd")

    cfg_path = root / "config.yaml"
    cfg_path.write_text("x: 1", encoding="utf-8")
    with pytest.raises(PathDeniedError):
        svc.read(1, "1:s1", "config.yaml")


def test_files_service_read_write_revision(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    p = root / "a.txt"
    p.write_text("hello", encoding="utf-8")
    session = SimpleNamespace(workdir=str(root))
    app = _build_app(tmp_path, session=session)
    app.manager = _Manager(session=None, sessions_by_chat={1: {"s1": session}})
    svc = FilesService(app)

    payload = svc.read(1, "1:s1", "a.txt")
    rev = payload["revision"]
    out = svc.write(1, "1:s1", "a.txt", "world", rev)
    assert out["ok"] is True
    again = svc.read(1, "1:s1", "a.txt")
    assert again["content"] == "world"


def test_files_service_uses_security_facade_for_path_resolution(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    file_path = root / "a.txt"
    file_path.write_text("hello", encoding="utf-8")
    session = SimpleNamespace(workdir=str(root))

    calls = []

    def _resolve_path(root_arg, rel_path, **kwargs):
        calls.append(
            {
                "root": str(root_arg),
                "path": str(rel_path),
                "context": dict(kwargs.get("context") or {}),
                "deny_names": tuple(kwargs.get("deny_names") or ()),
                "deny_extensions": tuple(kwargs.get("deny_extensions") or ()),
            }
        )
        return PathValidationResult(
            root=str(root),
            input_path=str(rel_path),
            resolved_path=str(file_path),
            relative_path="a.txt",
        )

    app = _build_app(tmp_path, session=session)
    app.manager = _Manager(session=None, sessions_by_chat={1: {"s1": session}})
    app.security = SimpleNamespace(resolve_path=_resolve_path)
    svc = FilesService(app)

    payload = svc.read(1, "1:s1", "a.txt")

    assert payload["content"] == "hello"
    assert len(calls) == 1
    assert calls[0]["root"] == str(root)
    assert calls[0]["path"] == "a.txt"
    assert "config.yaml" in calls[0]["deny_names"]
    assert ".pem" in calls[0]["deny_extensions"]
    assert calls[0]["context"]["user_id"] == 1
    assert calls[0]["context"]["session_uid"] == "1:s1"
    assert str(app.config.path) in tuple(calls[0]["context"]["protected_paths"])
