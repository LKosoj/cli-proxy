"""Unit tests for LocalFilesProvider — the refactored local file operations provider."""

import os

from miniapp.services.files_service import (
    FileTypeError,
    LocalFilesProvider,
    PathValidationError,
    RevisionConflictError,
)

import pytest


@pytest.fixture
def workdir(tmp_path):
    """Create a temporary workdir with some test files."""
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    return str(tmp_path)


@pytest.fixture
def provider(workdir):
    return LocalFilesProvider(workdir)


# ---------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------


def test_tree_root(provider, workdir):
    result = provider.tree(workdir)
    assert result["root"] == os.path.realpath(workdir)
    assert result["path"] == "."
    names = [i["name"] for i in result["items"]]
    assert "hello.txt" in names
    assert "sub" in names


def test_tree_subdir(provider, workdir):
    sub = os.path.join(workdir, "sub")
    result = provider.tree(sub)
    assert result["path"] == "sub"
    names = [i["name"] for i in result["items"]]
    assert "nested.txt" in names


def test_tree_dirs_first(provider, workdir):
    result = provider.tree(workdir)
    items = result["items"]
    dirs = [i for i in items if i["is_dir"]]
    files = [i for i in items if not i["is_dir"]]
    assert items == dirs + files


def test_tree_not_found(provider, workdir):
    with pytest.raises(PathValidationError, match="path not found"):
        provider.tree(os.path.join(workdir, "nope"))


def test_tree_not_dir(provider, workdir):
    with pytest.raises(PathValidationError, match="not a directory"):
        provider.tree(os.path.join(workdir, "hello.txt"))


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_file(provider, workdir):
    path = os.path.join(workdir, "hello.txt")
    result = provider.read(path, "hello.txt")
    assert result["content"] == "hello world"
    assert "revision" in result
    assert result["meta"]["path"] == "hello.txt"
    assert result["meta"]["size"] > 0


def test_read_not_found(provider, workdir):
    with pytest.raises(PathValidationError, match="file not found"):
        provider.read(os.path.join(workdir, "nope.txt"), "nope.txt")


def test_read_binary(provider, workdir):
    path = os.path.join(workdir, "bin.dat")
    with open(path, "wb") as f:
        f.write(b"\x00\x01\x02")
    with pytest.raises(FileTypeError, match="binary"):
        provider.read(path, "bin.dat")


def test_read_too_large(workdir):
    provider = LocalFilesProvider(workdir, max_file_size_bytes=5)
    path = os.path.join(workdir, "hello.txt")
    with pytest.raises(FileTypeError, match="too large"):
        provider.read(path, "hello.txt")


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_file(provider, workdir):
    path = os.path.join(workdir, "hello.txt")
    rev_before = provider.read(path, "hello.txt")["revision"]
    result = provider.write(path, "updated", rev_before)
    assert result["ok"] is True
    assert result["revision"] != rev_before
    assert open(path).read() == "updated"


def test_write_revision_mismatch(provider, workdir):
    path = os.path.join(workdir, "hello.txt")
    with pytest.raises(RevisionConflictError, match="revision mismatch"):
        provider.write(path, "x", "bad_revision")


def test_write_not_found(provider, workdir):
    with pytest.raises(PathValidationError, match="file not found"):
        provider.write(os.path.join(workdir, "nope.txt"), "x", None)


def test_write_too_large(workdir):
    provider = LocalFilesProvider(workdir, max_file_size_bytes=3)
    path = os.path.join(workdir, "hello.txt")
    with pytest.raises(FileTypeError, match="exceeds"):
        provider.write(path, "long content", None)


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_file(provider, workdir):
    path = os.path.join(workdir, "hello.txt")
    result = provider.download(path)
    assert result["content"] == b"hello world"
    assert result["filename"] == "hello.txt"
    assert result["size"] > 0


def test_download_not_found(provider, workdir):
    with pytest.raises(PathValidationError, match="file not found"):
        provider.download(os.path.join(workdir, "nope.txt"))


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_file(provider, workdir):
    path = os.path.join(workdir, "new.txt")
    result = provider.create(path, "file")
    assert result["ok"] is True
    assert os.path.isfile(path)


def test_create_dir(provider, workdir):
    path = os.path.join(workdir, "newdir")
    result = provider.create(path, "dir")
    assert result["ok"] is True
    assert os.path.isdir(path)


def test_create_already_exists(provider, workdir):
    path = os.path.join(workdir, "hello.txt")
    with pytest.raises(PathValidationError, match="already exists"):
        provider.create(path, "file")


def test_create_invalid_kind(provider, workdir):
    with pytest.raises(PathValidationError, match="kind must be"):
        provider.create(os.path.join(workdir, "x"), "symlink")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_file(provider, workdir):
    path = os.path.join(workdir, "hello.txt")
    result = provider.delete(path)
    assert result["ok"] is True
    assert not os.path.exists(path)


def test_delete_dir(provider, workdir):
    empty_dir = os.path.join(workdir, "empty")
    os.makedirs(empty_dir)
    result = provider.delete(empty_dir)
    assert result["ok"] is True
    assert not os.path.exists(empty_dir)


def test_delete_not_found(provider, workdir):
    with pytest.raises(PathValidationError, match="path not found"):
        provider.delete(os.path.join(workdir, "nope"))


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def test_meta_file(provider, workdir):
    path = os.path.join(workdir, "hello.txt")
    result = provider.meta(path, "hello.txt")
    assert result["path"] == "hello.txt"
    assert result["exists"] is True
    assert result["is_dir"] is False
    assert result["size"] > 0


def test_meta_dir(provider, workdir):
    path = os.path.join(workdir, "sub")
    result = provider.meta(path, "sub")
    assert result["is_dir"] is True


def test_meta_not_found(provider, workdir):
    with pytest.raises(PathValidationError, match="path not found"):
        provider.meta(os.path.join(workdir, "nope"), "nope")


# ---------------------------------------------------------------------------
# _safe_path traversal protection
# ---------------------------------------------------------------------------


def test_safe_path_blocks_traversal(provider):
    with pytest.raises(PathValidationError, match="escapes root"):
        provider._safe_path("../../etc/passwd")


def test_safe_path_allows_normal(provider, workdir):
    result = provider._safe_path("hello.txt")
    assert result == os.path.realpath(os.path.join(workdir, "hello.txt"))


# ---------------------------------------------------------------------------
# No dependency on RemoteControlService
# ---------------------------------------------------------------------------


def test_no_remote_control_import():
    """LocalFilesProvider does not import RemoteControlService."""
    import inspect
    source = inspect.getsource(LocalFilesProvider)
    assert "RemoteControlService" not in source
    assert "remote_control" not in source


# ---------------------------------------------------------------------------
# Hidden files (dotfiles) support
# ---------------------------------------------------------------------------


def test_tree_includes_dotfiles_by_default(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / ".hidden").write_text("y")
    (tmp_path / ".env").write_text("SECRET=1")
    provider = LocalFilesProvider(str(tmp_path))
    result = provider.tree(str(tmp_path))
    names = [i["name"] for i in result["items"]]
    assert ".hidden" in names
    assert ".env" in names
    assert "file.txt" in names


def test_tree_excludes_dotfiles_when_disabled(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / ".hidden").write_text("y")
    (tmp_path / ".config").mkdir()
    provider = LocalFilesProvider(str(tmp_path))
    result = provider.tree(str(tmp_path), show_hidden=False)
    names = [i["name"] for i in result["items"]]
    assert ".hidden" not in names
    assert ".config" not in names
    assert "file.txt" in names


def test_tree_show_hidden_true_explicit(tmp_path):
    (tmp_path / ".dot").write_text("x")
    provider = LocalFilesProvider(str(tmp_path))
    result = provider.tree(str(tmp_path), show_hidden=True)
    names = [i["name"] for i in result["items"]]
    assert ".dot" in names
