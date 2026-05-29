import asyncio
import types
from unittest.mock import patch

import pytest

from desktop.widgets.files_panel import FilesPanelWidget


def _session(session_uid: str = "desktop:s1"):
    return types.SimpleNamespace(
        id="s1",
        workdir="/tmp/project",
        conversation_scope=types.SimpleNamespace(session_uid=session_uid),
    )


def _ensure_async(coro, parent=None):
    loop = asyncio.get_running_loop()
    task = loop.create_task(coro)
    if parent is not None and hasattr(parent, "_background_tasks"):
        parent._background_tasks.add(task)
        task.add_done_callback(lambda t: parent._background_tasks.discard(t))
    return task


class _Facade:
    def __init__(self):
        self.write_calls = []

    async def files_execution_context(self, _session_uid):
        return {"execution_target": "local"}

    async def files_tree(self, _session_uid, path="."):
        return {
            "path": path or ".",
            "items": [
                {"name": "notes.txt", "path": "notes.txt", "is_dir": False, "size": 5, "mtime": 1},
                {"name": "src", "path": "src", "is_dir": True, "size": 0, "mtime": 1},
            ],
        }

    async def files_read(self, _session_uid, path):
        return {
            "content": "hello",
            "revision": "rev1",
            "meta": {"path": path, "size": 5, "mtime": 1},
        }

    async def files_write(self, session_uid, path, content, expected_revision=None, *, force=False):
        self.write_calls.append(
            {
                "session_uid": session_uid,
                "path": path,
                "content": content,
                "expected_revision": expected_revision,
                "force": force,
            }
        )
        return {"ok": True, "revision": "rev2"}


@pytest.mark.asyncio
async def test_files_panel_loads_tree_opens_and_saves_file(qtbot):
    facade = _Facade()

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        widget = FilesPanelWidget(facade)
        qtbot.addWidget(widget)

        widget.set_session(_session())
        await asyncio.sleep(0)

        assert widget.file_list.count() == 2
        assert widget.path_label.text() == "Path: ."

        item = widget.file_list.item(0)
        widget._on_item_activated(item)
        await asyncio.sleep(0)

        assert widget.editor_path_label.text() == "notes.txt"
        assert widget.editor.toPlainText() == "hello"

        widget.editor.setPlainText("updated")
        widget.save_open_file()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert facade.write_calls == [
        {
            "session_uid": "desktop:s1",
            "path": "notes.txt",
            "content": "updated",
            "expected_revision": "rev1",
            "force": False,
        }
    ]
    assert widget._open_revision == "rev2"
