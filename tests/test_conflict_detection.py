"""Tests for conflict detection, diff generation, and revision mismatch handling."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from miniapp.services.files_service import (
    LocalFilesProvider,
    RemoteFilesProvider,
    RevisionConflictError,
)


# ---------------------------------------------------------------------------
# RevisionConflictError payload
# ---------------------------------------------------------------------------


def test_revision_conflict_error_has_payload():
    exc = RevisionConflictError(
        "mismatch",
        expected_revision="aaa",
        current_revision="bbb",
        current_content="new content",
        diff_unified="--- yours\n+++ current\n",
    )
    assert exc.expected_revision == "aaa"
    assert exc.current_revision == "bbb"
    assert exc.current_content == "new content"
    assert exc.diff_unified is not None
    assert exc.status == 409


def test_revision_conflict_error_defaults():
    exc = RevisionConflictError()
    assert exc.expected_revision is None
    assert exc.current_revision is None
    assert exc.current_content is None
    assert exc.diff_unified is None


# ---------------------------------------------------------------------------
# LocalFilesProvider conflict detection
# ---------------------------------------------------------------------------


class TestLocalConflictDetection:
    def test_write_succeeds_with_correct_revision(self, tmp_path):
        (tmp_path / "f.txt").write_text("original")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        rev = prov.read(path, "f.txt")["revision"]
        result = prov.write(path, "updated", rev)
        assert result["ok"] is True

    def test_write_raises_conflict_with_wrong_revision(self, tmp_path):
        (tmp_path / "f.txt").write_text("original")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        with pytest.raises(RevisionConflictError) as exc_info:
            prov.write(path, "my changes", "wrong_revision")
        exc = exc_info.value
        assert exc.expected_revision == "wrong_revision"
        assert exc.current_revision is not None
        assert exc.current_content == "original"
        assert exc.diff_unified is not None

    def test_conflict_diff_contains_unified_format(self, tmp_path):
        (tmp_path / "f.txt").write_text("line1\nline2\nline3\n")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        with pytest.raises(RevisionConflictError) as exc_info:
            prov.write(path, "line1\nCHANGED\nline3\n", "bad_rev")
        diff = exc_info.value.diff_unified
        assert "---" in diff
        assert "+++" in diff

    def test_write_without_expected_revision_succeeds(self, tmp_path):
        (tmp_path / "f.txt").write_text("original")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        result = prov.write(path, "new content", None)
        assert result["ok"] is True

    def test_concurrent_edit_scenario(self, tmp_path):
        """Simulate: read → external change → write → conflict."""
        (tmp_path / "f.txt").write_text("v1")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")

        # User reads file
        rev = prov.read(path, "f.txt")["revision"]

        # External change
        (tmp_path / "f.txt").write_text("v2 by someone else")

        # User tries to write with stale revision
        with pytest.raises(RevisionConflictError) as exc_info:
            prov.write(path, "v2 by user", rev)

        exc = exc_info.value
        assert exc.current_content == "v2 by someone else"
        assert exc.expected_revision == rev


# ---------------------------------------------------------------------------
# RemoteFilesProvider conflict detection
# ---------------------------------------------------------------------------


class FakeSSHForConflict:
    """Fake SSH that simulates a remote file with conflict."""

    def __init__(self):
        self.files = {"/srv/app/f.txt": b"remote content v2"}
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append(command)
        import re as re_mod
        import base64

        # test -f
        m = re_mod.match(r"test -f (\S+) && echo OK \|\| echo MISSING", command)
        if m:
            path = m.group(1)
            tag = "OK" if path in self.files else "MISSING"
            return SimpleNamespace(stdout=f"{tag}\n", stderr="", exit_code=0)

        # test -L (symlink check)
        if "test -L" in command and "echo LINK" in command:
            return SimpleNamespace(stdout="OK\n", stderr="", exit_code=0)

        # sha256sum
        m = re_mod.match(r"sha256sum (\S+)", command)
        if m:
            path = m.group(1)
            if path in self.files:
                h = hashlib.sha256(self.files[path]).hexdigest()
                return SimpleNamespace(stdout=f"{h}  {path}\n", stderr="", exit_code=0)

        # base64 (read content)
        m = re_mod.match(r"base64 (\S+)", command)
        if m:
            path = m.group(1)
            if path in self.files:
                b64 = base64.b64encode(self.files[path]).decode()
                return SimpleNamespace(stdout=b64 + "\n", stderr="", exit_code=0)

        # echo ... | base64 -d > tmp && mv (write)
        if "base64 -d" in command and "mv" in command:
            return SimpleNamespace(stdout="", stderr="", exit_code=0)

        # test -L ... readlink (symlink target check)
        if "readlink" in command:
            return SimpleNamespace(stdout="NOT_LINK\n", stderr="", exit_code=0)

        return SimpleNamespace(stdout="", stderr="", exit_code=0)


class TestRemoteConflictDetection:
    def test_remote_write_conflict_returns_payload(self):
        ssh = FakeSSHForConflict()
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(RevisionConflictError) as exc_info:
            asyncio.run(prov.write("/srv/app/f.txt", "my changes", "stale_rev"))
        exc = exc_info.value
        assert exc.expected_revision == "stale_rev"
        assert exc.current_revision is not None
        assert exc.current_content == "remote content v2"
        assert exc.diff_unified is not None

    def test_remote_write_succeeds_with_correct_revision(self):
        ssh = FakeSSHForConflict()
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        correct_rev = hashlib.sha256(b"remote content v2").hexdigest()
        result = asyncio.run(prov.write("/srv/app/f.txt", "updated", correct_rev))
        assert result["ok"] is True

    def test_remote_write_without_revision(self):
        ssh = FakeSSHForConflict()
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.write("/srv/app/f.txt", "new", None))
        assert result["ok"] is True
