"""Tests for Force Save with audit: force=true skips revision check, audit logged."""

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
# LocalFilesProvider force save
# ---------------------------------------------------------------------------


class TestLocalForceSave:
    def test_force_skips_revision_check(self, tmp_path):
        (tmp_path / "f.txt").write_text("original")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        result = prov.write(path, "forced content", "wrong_revision", force=True)
        assert result["ok"] is True
        assert result["forced"] is True
        assert result["old_revision"] is not None
        assert (tmp_path / "f.txt").read_text() == "forced content"

    def test_force_false_still_checks_revision(self, tmp_path):
        (tmp_path / "f.txt").write_text("original")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        with pytest.raises(RevisionConflictError):
            prov.write(path, "new", "bad_rev", force=False)

    def test_force_returns_old_and_new_revision(self, tmp_path):
        (tmp_path / "f.txt").write_text("v1")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        old_rev = hashlib.sha256(b"v1").hexdigest()
        result = prov.write(path, "v2", "stale", force=True)
        assert result["old_revision"] == old_rev
        assert result["revision"] == hashlib.sha256(b"v2").hexdigest()

    def test_force_without_expected_revision(self, tmp_path):
        (tmp_path / "f.txt").write_text("original")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        result = prov.write(path, "new", None, force=True)
        assert result["ok"] is True
        assert result["forced"] is True

    def test_normal_write_no_forced_key(self, tmp_path):
        (tmp_path / "f.txt").write_text("original")
        prov = LocalFilesProvider(str(tmp_path))
        path = str(tmp_path / "f.txt")
        result = prov.write(path, "new", None)
        assert result["ok"] is True
        assert "forced" not in result


# ---------------------------------------------------------------------------
# RemoteFilesProvider force save
# ---------------------------------------------------------------------------


class FakeSSHForForce:
    def __init__(self):
        self.files = {"/srv/app/f.txt": b"remote v1"}
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append(command)
        import re as re_mod
        import base64

        m = re_mod.match(r"test -f (\S+) && echo OK \|\| echo MISSING", command)
        if m:
            path = m.group(1)
            return SimpleNamespace(stdout="OK\n" if path in self.files else "MISSING\n", stderr="", exit_code=0)

        if "test -L" in command and "echo LINK" in command:
            return SimpleNamespace(stdout="OK\n", stderr="", exit_code=0)

        if "readlink" in command:
            return SimpleNamespace(stdout="NOT_LINK\n", stderr="", exit_code=0)

        m = re_mod.match(r"sha256sum (\S+)", command)
        if m:
            path = m.group(1)
            if path in self.files:
                h = hashlib.sha256(self.files[path]).hexdigest()
                return SimpleNamespace(stdout=f"{h}  {path}\n", stderr="", exit_code=0)
            return SimpleNamespace(stdout="", stderr="", exit_code=1)

        m = re_mod.match(r"base64 (\S+)$", command)
        if m:
            path = m.group(1)
            if path in self.files:
                b64 = base64.b64encode(self.files[path]).decode()
                return SimpleNamespace(stdout=b64 + "\n", stderr="", exit_code=0)
            return SimpleNamespace(stdout="", stderr="", exit_code=1)

        if "base64 -d" in command and "mv" in command:
            m = re_mod.match(r"echo '([^']*)' \| base64 -d > (\S+) && mv \S+ (\S+)", command)
            if m:
                target = m.group(3)
                self.files[target] = base64.b64decode(m.group(1))
            return SimpleNamespace(stdout="", stderr="", exit_code=0)

        return SimpleNamespace(stdout="", stderr="", exit_code=0)


class TestRemoteForceSave:
    def test_force_skips_revision_check(self):
        ssh = FakeSSHForForce()
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.write("/srv/app/f.txt", "forced", "wrong_rev", force=True))
        assert result["ok"] is True
        assert result["forced"] is True
        assert result["old_revision"] is not None
        assert ssh.files["/srv/app/f.txt"] == b"forced"

    def test_force_false_raises_conflict(self):
        ssh = FakeSSHForForce()
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(RevisionConflictError):
            asyncio.run(prov.write("/srv/app/f.txt", "new", "bad_rev", force=False))

    def test_force_returns_old_and_new_revision(self):
        ssh = FakeSSHForForce()
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        old_rev = hashlib.sha256(b"remote v1").hexdigest()
        result = asyncio.run(prov.write("/srv/app/f.txt", "v2", "stale", force=True))
        assert result["old_revision"] == old_rev

    def test_normal_write_no_forced_key(self):
        ssh = FakeSSHForForce()
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.write("/srv/app/f.txt", "new", None))
        assert result["ok"] is True
        assert "forced" not in result

    def test_force_same_semantics_as_local(self, tmp_path):
        """Both providers return same keys when force=True."""
        # Local
        (tmp_path / "f.txt").write_text("v1")
        local = LocalFilesProvider(str(tmp_path))
        local_result = local.write(str(tmp_path / "f.txt"), "v2", "bad", force=True)

        # Remote
        ssh = FakeSSHForForce()
        remote = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        remote_result = asyncio.run(remote.write("/srv/app/f.txt", "v2", "bad", force=True))

        # Same keys
        assert set(local_result.keys()) == set(remote_result.keys())
        assert local_result["ok"] is True
        assert remote_result["ok"] is True
        assert local_result["forced"] is True
        assert remote_result["forced"] is True
        assert "old_revision" in local_result
        assert "old_revision" in remote_result
