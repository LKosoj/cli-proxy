import asyncio
import os
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
    ui_language = "ru"

    def __init__(self):
        self.write_calls = []
        self.delete_calls = []
        self.rename_calls = []
        self.upload_calls = []
        self.download_calls = []

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

    async def files_delete(self, session_uid, path, *, recursive=False):
        self.delete_calls.append({"session_uid": session_uid, "path": path, "recursive": recursive})
        return {"ok": True}

    async def files_rename(self, session_uid, rel_path, new_name):
        self.rename_calls.append({"session_uid": session_uid, "rel_path": rel_path, "new_name": new_name})
        return {"ok": True, "new_name": new_name}

    async def files_upload(self, session_uid, target_dir_rel, local_src_path):
        self.upload_calls.append({
            "session_uid": session_uid,
            "target_dir_rel": target_dir_rel,
            "local_src_path": local_src_path,
        })
        return {"ok": True, "filename": os.path.basename(local_src_path), "rel_path": local_src_path}

    async def files_download_bytes(self, session_uid, path):
        self.download_calls.append({"session_uid": session_uid, "path": path})
        return {"content": b"file content", "filename": os.path.basename(path)}

    async def files_create(self, session_uid, path, kind):
        return {"ok": True}


@pytest.mark.asyncio
async def test_files_panel_loads_tree_opens_and_saves_file(qtbot):
    facade = _Facade()

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        widget = FilesPanelWidget(facade)
        qtbot.addWidget(widget)

        widget.set_session(_session())
        await asyncio.sleep(0)

        assert widget.file_list.count() == 2
        assert widget.path_label.text() == "Путь: ."

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


@pytest.mark.asyncio
async def test_rename_selected_calls_facade(qtbot):
    """Rename triggers facade.files_rename with correct args."""
    facade = _Facade()

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        with patch("desktop.widgets.files_panel.QInputDialog.getText", return_value=("notes_v2.txt", True)):
            widget = FilesPanelWidget(facade)
            qtbot.addWidget(widget)
            widget.set_session(_session())
            await asyncio.sleep(0)

            # Select the first item (notes.txt)
            widget.file_list.setCurrentRow(0)
            widget._on_selection_changed()
            widget.rename_selected()
            await asyncio.sleep(0)

    assert len(facade.rename_calls) == 1
    assert facade.rename_calls[0]["rel_path"] == "notes.txt"
    assert facade.rename_calls[0]["new_name"] == "notes_v2.txt"
    assert facade.rename_calls[0]["session_uid"] == "desktop:s1"


@pytest.mark.asyncio
async def test_rename_updates_open_path_when_open_file_renamed(qtbot):
    """If the currently open file is renamed, _open_path is updated."""
    facade = _Facade()

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        widget = FilesPanelWidget(facade)
        qtbot.addWidget(widget)
        widget.set_session(_session())
        await asyncio.sleep(0)

        # Open notes.txt
        item = widget.file_list.item(0)
        widget._on_item_activated(item)
        await asyncio.sleep(0)
        assert widget._open_path == "notes.txt"

        # Rename it
        widget.file_list.setCurrentRow(0)
        widget._on_selection_changed()
        with patch("desktop.widgets.files_panel.QInputDialog.getText", return_value=("renamed.txt", True)):
            widget.rename_selected()
            await asyncio.sleep(0)

    assert widget._open_path == "renamed.txt"


@pytest.mark.asyncio
async def test_upload_file_calls_facade(qtbot, tmp_path):
    """Upload triggers facade.files_upload with the selected local path."""
    facade = _Facade()
    src = tmp_path / "testfile.txt"
    src.write_text("data")

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        with patch("desktop.widgets.files_panel.QFileDialog.getOpenFileName", return_value=(str(src), "")):
            widget = FilesPanelWidget(facade)
            qtbot.addWidget(widget)
            widget.set_session(_session())
            await asyncio.sleep(0)

            widget.upload_file()
            await asyncio.sleep(0)

    assert len(facade.upload_calls) == 1
    assert facade.upload_calls[0]["local_src_path"] == str(src)
    assert facade.upload_calls[0]["session_uid"] == "desktop:s1"
    assert facade.upload_calls[0]["target_dir_rel"] == "."


@pytest.mark.asyncio
async def test_download_selected_writes_file(qtbot, tmp_path):
    """Download saves file bytes to the user-chosen destination path."""
    facade = _Facade()
    dst = tmp_path / "output.txt"

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        with patch("desktop.widgets.files_panel.QFileDialog.getSaveFileName", return_value=(str(dst), "")):
            widget = FilesPanelWidget(facade)
            qtbot.addWidget(widget)
            widget.set_session(_session())
            await asyncio.sleep(0)

            # Select notes.txt (file, not dir) via context menu path selection
            widget._selected_path = "notes.txt"
            widget.download_selected()
            await asyncio.sleep(0)

    assert dst.exists()
    assert dst.read_bytes() == b"file content"
    assert len(facade.download_calls) == 1
    assert facade.download_calls[0]["path"] == "notes.txt"


@pytest.mark.asyncio
async def test_delete_directory_uses_recursive(qtbot):
    """Deleting a directory calls files_delete with recursive=True after confirmation."""
    facade = _Facade()

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        with patch("desktop.widgets.files_panel.QMessageBox.question", return_value=0x4000):  # Yes
            with patch("desktop.widgets.files_panel.QMessageBox.warning", return_value=0x4000):  # Yes
                widget = FilesPanelWidget(facade)
                qtbot.addWidget(widget)
                widget.set_session(_session())
                await asyncio.sleep(0)

                # Select 'src' directory (second item)
                widget.file_list.setCurrentRow(1)
                widget._on_selection_changed()
                widget.delete_selected()
                await asyncio.sleep(0)

    assert len(facade.delete_calls) == 1
    assert facade.delete_calls[0]["path"] == "src"
    assert facade.delete_calls[0]["recursive"] is True


@pytest.mark.asyncio
async def test_delete_file_not_recursive(qtbot):
    """Deleting a file calls files_delete without recursive flag (False)."""
    facade = _Facade()

    with patch("desktop.widgets.files_panel.ensure_async", side_effect=_ensure_async):
        with patch("desktop.widgets.files_panel.QMessageBox.question", return_value=0x4000):  # Yes
            widget = FilesPanelWidget(facade)
            qtbot.addWidget(widget)
            widget.set_session(_session())
            await asyncio.sleep(0)

            # Select notes.txt (file, not dir)
            widget.file_list.setCurrentRow(0)
            widget._on_selection_changed()
            widget.delete_selected()
            await asyncio.sleep(0)

    assert len(facade.delete_calls) == 1
    assert facade.delete_calls[0]["path"] == "notes.txt"
    assert facade.delete_calls[0]["recursive"] is False
