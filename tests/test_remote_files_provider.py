"""Unit tests for RemoteFilesProvider — SSH/SFTP-based file operations."""

import asyncio
import base64
import hashlib
import posixpath
import re
import shlex
from types import SimpleNamespace
from typing import Dict

import pytest

from miniapp.services.files_service import (
    BINARY_EXTENSIONS,
    FileTypeError,
    PathDeniedError,
    PathValidationError,
    RemoteFilesProvider,
    RevisionConflictError,
    _is_binary_by_extension,
)


# ---------------------------------------------------------------------------
# Fake SSH service that simulates a remote filesystem in memory
# ---------------------------------------------------------------------------


class FakeRemoteFS:
    """In-memory remote filesystem for testing."""

    def __init__(self):
        self.files: Dict[str, bytes] = {}
        self.dirs: set = set()
        self.gitignored: set = set()  # basenames that git check-ignore would report
        self.symlinks: Dict[str, str] = {}  # path -> target

    def add_file(self, path: str, content: str) -> None:
        self.files[path] = content.encode("utf-8")
        # auto-create parent dirs
        parts = path.rsplit("/", 1)
        if len(parts) == 2 and parts[0]:
            self.dirs.add(parts[0])

    def add_dir(self, path: str) -> None:
        self.dirs.add(path)

    def add_symlink(self, path: str, target: str) -> None:
        self.symlinks[path] = target

    def exists(self, path: str) -> bool:
        resolved = self.resolve(path)
        return (
            path in self.files
            or path in self.dirs
            or path in self.symlinks
            or resolved in self.files
            or resolved in self.dirs
        )

    def is_dir(self, path: str) -> bool:
        return path in self.dirs or self.resolve(path) in self.dirs

    def is_file(self, path: str) -> bool:
        resolved = self.resolve(path)
        return path in self.files or resolved in self.files

    def is_symlink(self, path: str) -> bool:
        return path in self.symlinks

    def resolve(self, path: str) -> str:
        """Resolve symlinks to get actual file path."""
        parts = [part for part in str(path or "").split("/") if part]
        current = ""
        for idx, part in enumerate(parts):
            current = f"{current}/{part}" if current else f"/{part}"
            if current in self.symlinks:
                rest = "/".join(parts[idx + 1:])
                target = self.symlinks[current]
                return posixpath.normpath(f"{target}/{rest}" if rest else target)
        return path

    def file_content(self, path: str) -> bytes:
        """Get file content, resolving symlinks."""
        resolved = self.resolve(path)
        return self.files[resolved]


class FakeSSHService:
    """Fake SSHService that interprets shell commands against FakeRemoteFS."""

    def __init__(self, fs: FakeRemoteFS):
        self._fs = fs
        self.calls = []

    def _ok(self, stdout="", exit_code=0):
        return SimpleNamespace(stdout=stdout, stderr="", exit_code=exit_code)

    def _fail(self, stderr="", exit_code=1):
        return SimpleNamespace(stdout="", stderr=stderr, exit_code=exit_code)

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append(command)
        return self._dispatch(command)

    def _dispatch(self, cmd: str):
        # test -d PATH && echo DIR || echo MISSING
        m = re.match(r"test -d (\S+) && echo DIR \|\| echo MISSING", cmd)
        if m:
            path = m.group(1)
            return self._ok("DIR\n") if self._fs.is_dir(path) else self._ok("MISSING\n")

        # test -f PATH && echo OK || echo MISSING
        m = re.match(r"test -f (\S+) && echo OK \|\| echo MISSING", cmd)
        if m:
            path = m.group(1)
            return self._ok("OK\n") if self._fs.is_file(path) else self._ok("MISSING\n")

        # test -e PATH && echo EXISTS || echo OK
        m = re.match(r"test -e (\S+) && echo EXISTS \|\| echo OK", cmd)
        if m:
            path = m.group(1)
            return self._ok("EXISTS\n") if self._fs.exists(path) else self._ok("OK\n")

        # test -e PATH && echo OK || echo MISSING
        m = re.match(r"test -e (\S+) && echo OK \|\| echo MISSING", cmd)
        if m:
            path = m.group(1)
            return self._ok("OK\n") if self._fs.exists(path) else self._ok("MISSING\n")

        # stat -c '%s %Y' PATH
        m = re.match(r"stat -c '%s %Y' (\S+)", cmd)
        if m:
            path = m.group(1)
            if self._fs.is_file(path):
                return self._ok(f"{len(self._fs.file_content(path))} 1700000000\n")
            return self._fail("not found")

        # stat -c '%s' PATH
        m = re.match(r"stat -c '%s' (\S+)", cmd)
        if m:
            path = m.group(1)
            if self._fs.is_file(path):
                return self._ok(f"{len(self._fs.file_content(path))}\n")
            return self._ok("0\n")

        # readlink -f PATH
        m = re.match(r"readlink -f (\S+)$", cmd)
        if m:
            path = m.group(1)
            return self._ok(self._fs.resolve(path) + "\n")

        # stat -c '%F|%s|%Y|%n' PATH/* PATH/.* (tree listing)
        if "stat -c '%F|%s|%Y|%n'" in cmd:
            # Extract the dir from the glob pattern
            parts = cmd.split("'")[-1].strip().split()
            dir_path = parts[0].replace("/*", "") if parts else ""
            lines = []
            for fpath, content in self._fs.files.items():
                parent = fpath.rsplit("/", 1)[0] if "/" in fpath else ""
                if parent == dir_path:
                    ftype = "regular empty file" if not content else "regular file"
                    lines.append(f"{ftype}|{len(content)}|1700000000|{fpath}")
            for dpath in self._fs.dirs:
                parent = dpath.rsplit("/", 1)[0] if "/" in dpath else ""
                if parent == dir_path and dpath != dir_path:
                    lines.append(f"directory|4096|1700000000|{dpath}")
            return self._ok("\n".join(lines) + "\n")

        # stat -c '%F|%s|%Y' PATH 2>/dev/null (meta)
        m = re.match(r"stat -c '%F\|%s\|%Y' (\S+)", cmd)
        if m:
            path = m.group(1)
            if self._fs.is_file(path):
                return self._ok(f"regular file|{len(self._fs.file_content(path))}|1700000000\n")
            if self._fs.is_dir(path):
                return self._ok("directory|4096|1700000000\n")
            return self._fail("not found")

        # base64 PATH
        m = re.match(r"base64 (\S+)$", cmd)
        if m:
            path = m.group(1)
            if self._fs.is_file(path):
                b64 = base64.b64encode(self._fs.file_content(path)).decode("ascii")
                return self._ok(b64 + "\n")
            return self._fail("not found")

        # sha256sum PATH
        m = re.match(r"sha256sum (\S+)", cmd)
        if m:
            path = m.group(1)
            if self._fs.is_file(path):
                h = hashlib.sha256(self._fs.file_content(path)).hexdigest()
                return self._ok(f"{h}  {path}\n")
            return self._fail("not found")

        # echo '...' | base64 -d > TMP && mv TMP PATH (write)
        m = re.match(r"echo '([^']*)' \| base64 -d > (\S+) && mv \S+ (\S+)", cmd)
        if m:
            b64_data, _, target = m.group(1), m.group(2), m.group(3)
            self._fs.files[target] = base64.b64decode(b64_data)
            return self._ok()

        # mkdir -p PARENT && touch PATH (create file)
        m = re.match(r"mkdir -p \S+ && touch (\S+)", cmd)
        if m:
            path = m.group(1)
            self._fs.files[path] = b""
            return self._ok()

        # mkdir PATH (create dir, no -p)
        m = re.match(r"mkdir (\S+)$", cmd)
        if m:
            path = m.group(1)
            self._fs.dirs.add(path)
            return self._ok()

        # test -d PATH && rmdir PATH || rm PATH (delete)
        m = re.match(r"test -d (\S+) && rmdir \S+ \|\| rm (\S+)", cmd)
        if m:
            path = m.group(1)
            if self._fs.is_dir(path):
                self._fs.dirs.discard(path)
            elif self._fs.is_symlink(path):
                del self._fs.symlinks[path]
            elif path in self._fs.files:
                del self._fs.files[path]
            return self._ok()

        # cd DIR && ls -A | xargs -r git check-ignore -- (gitignore)
        if "git check-ignore" in cmd and "ls -A" in cmd:
            ignored = [name for name in self._fs.gitignored]
            return self._ok("\n".join(ignored) + "\n" if ignored else "")

        # test -L PATH && readlink -f PATH || echo NOT_LINK (symlink target check)
        m = re.match(r"test -L (\S+) && readlink -f \S+ \|\| echo NOT_LINK", cmd)
        if m:
            path = m.group(1)
            if self._fs.is_symlink(path):
                return self._ok(self._fs.symlinks[path] + "\n")
            return self._ok("NOT_LINK\n")

        # test -L PATH && echo LINK || echo OK (symlink write deny check)
        m = re.match(r"test -L (\S+) && echo LINK \|\| echo OK", cmd)
        if m:
            path = m.group(1)
            tag = "LINK" if self._fs.is_symlink(path) else "OK"
            return self._ok(f"{tag}\n")

        return self._fail(f"unknown command: {cmd}", exit_code=127)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_fs():
    fs = FakeRemoteFS()
    fs.add_dir("/srv/app")
    fs.add_dir("/srv/app/sub")
    fs.add_dir("/srv/app/.hidden_dir")
    fs.add_file("/srv/app/hello.txt", "hello world")
    fs.add_file("/srv/app/.env", "SECRET=1")
    fs.add_file("/srv/app/.gitignore", "node_modules/")
    fs.add_file("/srv/app/sub/nested.txt", "nested")
    fs.add_file("/srv/app/.hidden_dir/secret.txt", "secret")
    return fs


@pytest.fixture
def ssh(remote_fs):
    return FakeSSHService(remote_fs)


@pytest.fixture
def provider(ssh):
    return RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")


# ---------------------------------------------------------------------------
# Interface parity with LocalFilesProvider
# ---------------------------------------------------------------------------


def test_has_all_methods():
    methods = {"tree", "read", "write", "download", "create", "delete", "meta"}
    actual = {m for m in dir(RemoteFilesProvider) if not m.startswith("_") and callable(getattr(RemoteFilesProvider, m))}
    assert methods.issubset(actual)


def test_root_property(provider):
    assert provider.root == "/srv/app"


def test_max_file_size_bytes(provider):
    assert provider.max_file_size_bytes == 5 * 1024 * 1024


def test_remote_shell_paths_are_quoted_before_exec() -> None:
    class RecordingSSH:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def exec(self, _workdir, _host_alias, command, *, timeout_sec=30, chat_id=None):
            del timeout_sec, chat_id
            self.calls.append(str(command))
            return SimpleNamespace(stdout="NOT_LINK\n", stderr="", exit_code=0)

    ssh = RecordingSSH()
    provider = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")

    asyncio.run(provider._check_symlink_target("/srv/app/a; touch /tmp/pwn"))

    tokens = shlex.split(ssh.calls[0])
    assert tokens[:3] == ["test", "-L", "/srv/app/a; touch /tmp/pwn"]


def test_safe_path_blocks_traversal(provider):
    with pytest.raises(PathValidationError, match="escapes root"):
        provider._safe_path("../../etc/passwd")


def test_safe_path_allows_normal(provider):
    assert provider._safe_path("hello.txt") == "/srv/app/hello.txt"


# ---------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------


def test_tree(provider):
    result = asyncio.run(provider.tree("/srv/app"))
    assert result["root"] == "/srv/app"
    assert result["path"] == "."
    names = [i["name"] for i in result["items"]]
    assert "hello.txt" in names
    assert "sub" in names


def test_tree_subdir(provider):
    result = asyncio.run(provider.tree("/srv/app/sub"))
    assert result["path"] == "sub"
    names = [i["name"] for i in result["items"]]
    assert "nested.txt" in names


def test_tree_not_found(provider):
    with pytest.raises(PathValidationError, match="path not found"):
        asyncio.run(provider.tree("/srv/app/nope"))


def test_tree_parses_gnu_stat_regular_empty_file_type(provider, remote_fs):
    remote_fs.add_file("/srv/app/empty.txt", "")

    result = asyncio.run(provider.tree("/srv/app"))

    empty = next(item for item in result["items"] if item["name"] == "empty.txt")
    assert empty["is_dir"] is False
    assert empty["size"] == 0


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read(provider):
    result = asyncio.run(provider.read("/srv/app/hello.txt", "hello.txt"))
    assert result["content"] == "hello world"
    assert "revision" in result
    assert result["meta"]["path"] == "hello.txt"
    assert result["meta"]["size"] > 0


def test_read_not_found(provider):
    with pytest.raises(PathValidationError, match="file not found"):
        asyncio.run(provider.read("/srv/app/nope.txt", "nope.txt"))


def test_read_too_large(ssh):
    prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=3)
    with pytest.raises(FileTypeError, match="too large"):
        asyncio.run(prov.read("/srv/app/hello.txt", "hello.txt"))


def test_read_rejects_symlink_ancestor_that_escapes_root(provider, remote_fs):
    remote_fs.add_dir("/outside")
    remote_fs.add_file("/outside/secret.txt", "secret")
    remote_fs.add_symlink("/srv/app/link", "/outside")

    with pytest.raises(PathDeniedError, match="escapes remote_project_root"):
        asyncio.run(provider.read("/srv/app/link/secret.txt", "link/secret.txt"))


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write(provider, remote_fs):
    rev = asyncio.run(provider.read("/srv/app/hello.txt", "hello.txt"))["revision"]
    result = asyncio.run(provider.write("/srv/app/hello.txt", "updated", rev))
    assert result["ok"] is True
    assert result["revision"]
    assert remote_fs.files["/srv/app/hello.txt"] == b"updated"


def test_write_revision_mismatch(provider):
    with pytest.raises(RevisionConflictError, match="revision mismatch"):
        asyncio.run(provider.write("/srv/app/hello.txt", "x", "bad_rev"))


def test_write_not_found(provider):
    with pytest.raises(PathValidationError, match="file not found"):
        asyncio.run(provider.write("/srv/app/nope.txt", "x", None))


def test_write_too_large(ssh):
    prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=3)
    with pytest.raises(FileTypeError, match="exceeds"):
        asyncio.run(prov.write("/srv/app/hello.txt", "long content", None))


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download(provider):
    result = asyncio.run(provider.download("/srv/app/hello.txt"))
    assert result["content"] == b"hello world"
    assert result["filename"] == "hello.txt"
    assert result["size"] > 0


def test_download_not_found(provider):
    with pytest.raises(PathValidationError, match="file not found"):
        asyncio.run(provider.download("/srv/app/nope.txt"))


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_file(provider, remote_fs):
    result = asyncio.run(provider.create("/srv/app/new.txt", "file"))
    assert result["ok"] is True
    assert "/srv/app/new.txt" in remote_fs.files


def test_create_dir(provider, remote_fs):
    result = asyncio.run(provider.create("/srv/app/newdir", "dir"))
    assert result["ok"] is True
    assert "/srv/app/newdir" in remote_fs.dirs


def test_create_already_exists(provider):
    with pytest.raises(PathValidationError, match="already exists"):
        asyncio.run(provider.create("/srv/app/hello.txt", "file"))


def test_create_invalid_kind(provider):
    with pytest.raises(PathValidationError, match="kind must be"):
        asyncio.run(provider.create("/srv/app/x", "symlink"))


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_file(provider, remote_fs):
    result = asyncio.run(provider.delete("/srv/app/hello.txt"))
    assert result["ok"] is True
    assert "/srv/app/hello.txt" not in remote_fs.files


def test_delete_not_found(provider):
    with pytest.raises(PathValidationError, match="path not found"):
        asyncio.run(provider.delete("/srv/app/nope"))


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def test_meta_file(provider):
    result = asyncio.run(provider.meta("/srv/app/hello.txt", "hello.txt"))
    assert result["path"] == "hello.txt"
    assert result["exists"] is True
    assert result["is_dir"] is False
    assert result["size"] > 0


def test_meta_dir(provider):
    result = asyncio.run(provider.meta("/srv/app/sub", "sub"))
    assert result["is_dir"] is True


def test_meta_not_found(provider):
    with pytest.raises(PathValidationError, match="path not found"):
        asyncio.run(provider.meta("/srv/app/nope", "nope"))


# ---------------------------------------------------------------------------
# Path validation: _validate_resolved_path
# ---------------------------------------------------------------------------


class TestValidateResolvedPath:
    def test_root_itself_allowed(self, provider):
        assert provider._validate_resolved_path("/srv/app") == "/srv/app"

    def test_child_allowed(self, provider):
        assert provider._validate_resolved_path("/srv/app/hello.txt") == "/srv/app/hello.txt"

    def test_nested_child_allowed(self, provider):
        assert provider._validate_resolved_path("/srv/app/sub/nested.txt") == "/srv/app/sub/nested.txt"

    def test_traversal_dotdot_rejected(self, provider):
        with pytest.raises(PathValidationError, match="escapes root"):
            provider._validate_resolved_path("/srv/app/../etc/passwd")

    def test_traversal_absolute_rejected(self, provider):
        with pytest.raises(PathValidationError, match="escapes root"):
            provider._validate_resolved_path("/etc/passwd")

    def test_traversal_prefix_trick_rejected(self, provider):
        with pytest.raises(PathValidationError, match="escapes root"):
            provider._validate_resolved_path("/srv/app_evil/file")

    def test_normalization_removes_double_slash(self, provider):
        assert provider._validate_resolved_path("/srv/app//hello.txt") == "/srv/app/hello.txt"

    def test_normalization_removes_dot(self, provider):
        assert provider._validate_resolved_path("/srv/app/./hello.txt") == "/srv/app/hello.txt"

    def test_normalization_collapses_dotdot_within_root(self, provider):
        assert provider._validate_resolved_path("/srv/app/sub/../hello.txt") == "/srv/app/hello.txt"

    def test_dotdot_escaping_root(self, provider):
        with pytest.raises(PathValidationError, match="escapes root"):
            provider._validate_resolved_path("/srv/app/../../etc/shadow")


# ---------------------------------------------------------------------------
# Path validation: _safe_path (relative → absolute)
# ---------------------------------------------------------------------------


class TestSafePath:
    def test_simple_relative(self, provider):
        assert provider._safe_path("hello.txt") == "/srv/app/hello.txt"

    def test_nested_relative(self, provider):
        assert provider._safe_path("sub/nested.txt") == "/srv/app/sub/nested.txt"

    def test_dot_resolves_to_root(self, provider):
        assert provider._safe_path(".") == "/srv/app"

    def test_traversal_rejected(self, provider):
        with pytest.raises(PathValidationError, match="escapes root"):
            provider._safe_path("../etc/passwd")

    def test_deep_traversal_rejected(self, provider):
        with pytest.raises(PathValidationError, match="escapes root"):
            provider._safe_path("sub/../../etc/passwd")

    def test_double_dot_at_root_rejected(self, provider):
        with pytest.raises(PathValidationError, match="escapes root"):
            provider._safe_path("..")


# ---------------------------------------------------------------------------
# Path traversal rejection in all public methods
# ---------------------------------------------------------------------------


class TestTraversalRejectedByMethods:
    """Every public method rejects paths outside root before hitting SSH."""

    def test_tree_rejects_traversal(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.tree("/etc"))
        assert len(ssh.calls) == 0

    def test_read_rejects_traversal(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.read("/etc/passwd", "x"))
        assert len(ssh.calls) == 0

    def test_write_rejects_traversal(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.write("/etc/passwd", "x", None))
        assert len(ssh.calls) == 0

    def test_download_rejects_traversal(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.download("/etc/passwd"))
        assert len(ssh.calls) == 0

    def test_create_rejects_traversal(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.create("/tmp/evil", "file"))
        assert len(ssh.calls) == 0

    def test_delete_rejects_traversal(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.delete("/etc/passwd"))
        assert len(ssh.calls) == 0

    def test_meta_rejects_traversal(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.meta("/etc/passwd", "x"))
        assert len(ssh.calls) == 0

    def test_dotdot_traversal_rejected_before_ssh(self, provider, ssh):
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(provider.tree("/srv/app/../../etc"))
        assert len(ssh.calls) == 0


# ---------------------------------------------------------------------------
# Hidden files (dotfiles) support
# ---------------------------------------------------------------------------


class TestTreeDotfiles:
    """Hidden files are included by default and can be filtered out."""

    def test_dotfiles_included_by_default(self, provider):
        result = asyncio.run(provider.tree("/srv/app"))
        names = [i["name"] for i in result["items"]]
        assert ".env" in names
        assert ".gitignore" in names
        assert ".hidden_dir" in names
        assert "hello.txt" in names

    def test_dotfiles_excluded_when_show_hidden_false(self, provider):
        result = asyncio.run(provider.tree("/srv/app", show_hidden=False))
        names = [i["name"] for i in result["items"]]
        assert ".env" not in names
        assert ".gitignore" not in names
        assert ".hidden_dir" not in names
        assert "hello.txt" in names
        assert "sub" in names

    def test_show_hidden_true_explicit(self, provider):
        result = asyncio.run(provider.tree("/srv/app", show_hidden=True))
        names = [i["name"] for i in result["items"]]
        assert ".env" in names

    def test_dotfiles_in_subdir(self, provider, remote_fs):
        remote_fs.add_file("/srv/app/sub/.hidden_sub", "x")
        result = asyncio.run(provider.tree("/srv/app/sub"))
        names = [i["name"] for i in result["items"]]
        assert ".hidden_sub" in names

    def test_dotfiles_in_subdir_excluded(self, provider, remote_fs):
        remote_fs.add_file("/srv/app/sub/.hidden_sub", "x")
        result = asyncio.run(provider.tree("/srv/app/sub", show_hidden=False))
        names = [i["name"] for i in result["items"]]
        assert ".hidden_sub" not in names
        assert "nested.txt" in names


# ---------------------------------------------------------------------------
# .gitignore support
# ---------------------------------------------------------------------------


class TestTreeGitignore:
    """Entries matched by git check-ignore are filtered when respect_gitignore=True."""

    def test_gitignore_not_applied_by_default(self, provider, remote_fs):
        remote_fs.add_file("/srv/app/node_modules/.keep", "")
        remote_fs.add_dir("/srv/app/node_modules")
        remote_fs.gitignored.add("node_modules")
        result = asyncio.run(provider.tree("/srv/app"))
        names = [i["name"] for i in result["items"]]
        assert "node_modules" in names

    def test_gitignore_filters_when_enabled(self, provider, remote_fs):
        remote_fs.add_dir("/srv/app/node_modules")
        remote_fs.gitignored.add("node_modules")
        result = asyncio.run(provider.tree("/srv/app", respect_gitignore=True))
        names = [i["name"] for i in result["items"]]
        assert "node_modules" not in names
        assert "hello.txt" in names

    def test_gitignore_and_hidden_combined(self, provider, remote_fs):
        remote_fs.add_dir("/srv/app/node_modules")
        remote_fs.gitignored.add("node_modules")
        result = asyncio.run(provider.tree(
            "/srv/app", show_hidden=False, respect_gitignore=True,
        ))
        names = [i["name"] for i in result["items"]]
        assert "node_modules" not in names
        assert ".env" not in names
        assert "hello.txt" in names

    def test_gitignore_no_git_repo_is_safe(self, ssh, remote_fs):
        """If git is not available, respect_gitignore silently returns no ignored files."""
        # Empty gitignored set means git check-ignore returns nothing
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.tree("/srv/app", respect_gitignore=True))
        names = [i["name"] for i in result["items"]]
        assert "hello.txt" in names


# ---------------------------------------------------------------------------
# Symlink policy tests
# ---------------------------------------------------------------------------


class TestSymlinkPolicy:
    """Symlink follow is allowed only inside root; write through symlink is denied."""

    def test_read_symlink_inside_root_allowed(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/link.txt", "/srv/app/hello.txt")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.read("/srv/app/link.txt", "link.txt"))
        assert result["content"] == "hello world"

    def test_read_symlink_outside_root_denied(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/evil_link", "/etc/passwd")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(PathDeniedError, match="symlink target escapes"):
            asyncio.run(prov.read("/srv/app/evil_link", "evil_link"))

    def test_write_through_symlink_denied(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/link.txt", "/srv/app/hello.txt")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(PathDeniedError, match="write through symlink"):
            asyncio.run(prov.write("/srv/app/link.txt", "hacked", None))

    def test_write_regular_file_allowed(self, ssh, remote_fs):
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        rev = asyncio.run(prov.read("/srv/app/hello.txt", "hello.txt"))["revision"]
        result = asyncio.run(prov.write("/srv/app/hello.txt", "updated", rev))
        assert result["ok"] is True

    def test_download_symlink_inside_root_allowed(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/dl_link", "/srv/app/hello.txt")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.download("/srv/app/dl_link"))
        assert result["content"] == b"hello world"

    def test_download_symlink_outside_root_denied(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/bad_dl", "/etc/shadow")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(PathDeniedError, match="symlink target escapes"):
            asyncio.run(prov.download("/srv/app/bad_dl"))

    def test_delete_symlink_inside_root_allowed(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/del_link", "/srv/app/hello.txt")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.delete("/srv/app/del_link"))
        assert result["ok"] is True

    def test_delete_symlink_outside_root_denied(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/bad_del", "/tmp/evil")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(PathDeniedError, match="symlink target escapes"):
            asyncio.run(prov.delete("/srv/app/bad_del"))

    def test_meta_symlink_inside_root_allowed(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/meta_link", "/srv/app/hello.txt")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.meta("/srv/app/meta_link", "meta_link"))
        assert result["exists"] is True

    def test_meta_symlink_outside_root_denied(self, ssh, remote_fs):
        remote_fs.add_symlink("/srv/app/bad_meta", "/var/log/syslog")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(PathDeniedError, match="symlink target escapes"):
            asyncio.run(prov.meta("/srv/app/bad_meta", "bad_meta"))

    def test_regular_file_no_symlink_check_overhead(self, ssh, remote_fs):
        """Non-symlink files pass through without error."""
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.meta("/srv/app/hello.txt", "hello.txt"))
        assert result["exists"] is True

    def test_write_deny_does_not_leak_to_ssh(self, ssh, remote_fs):
        """Write through symlink is rejected BEFORE any write SSH commands."""
        remote_fs.add_symlink("/srv/app/wlink", "/srv/app/hello.txt")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        call_count_before = len(ssh.calls)
        with pytest.raises(PathDeniedError, match="write through symlink"):
            asyncio.run(prov.write("/srv/app/wlink", "x", None))
        # Only the symlink check command should have been issued, not the actual write
        write_cmds = [c for c in ssh.calls[call_count_before:] if "base64 -d" in c]
        assert len(write_cmds) == 0


# ---------------------------------------------------------------------------
# Binary file detection by extension
# ---------------------------------------------------------------------------


class TestBinaryFileDetection:
    """Binary files are blocked from read/write/download by extension."""

    def test_is_binary_by_extension_positive(self):
        assert _is_binary_by_extension("/srv/app/image.png") is True
        assert _is_binary_by_extension("/srv/app/archive.zip") is True
        assert _is_binary_by_extension("/srv/app/doc.pdf") is True
        assert _is_binary_by_extension("/srv/app/lib.so") is True
        assert _is_binary_by_extension("/srv/app/font.woff2") is True

    def test_is_binary_by_extension_negative(self):
        assert _is_binary_by_extension("/srv/app/code.py") is False
        assert _is_binary_by_extension("/srv/app/readme.md") is False
        assert _is_binary_by_extension("/srv/app/config.yaml") is False
        assert _is_binary_by_extension("/srv/app/data.json") is False
        assert _is_binary_by_extension("/srv/app/script.sh") is False

    def test_is_binary_by_extension_case_insensitive(self):
        assert _is_binary_by_extension("/srv/app/IMAGE.PNG") is True
        assert _is_binary_by_extension("/srv/app/Doc.PDF") is True

    def test_binary_extensions_is_frozenset(self):
        assert isinstance(BINARY_EXTENSIONS, frozenset)
        assert ".png" in BINARY_EXTENSIONS
        assert ".zip" in BINARY_EXTENSIONS

    def test_read_binary_extension_blocked(self, ssh, remote_fs):
        remote_fs.add_file("/srv/app/photo.png", "fake png")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(FileTypeError, match="binary"):
            asyncio.run(prov.read("/srv/app/photo.png", "photo.png"))
        # Should reject before any SSH call for content
        assert not any("base64" in c for c in ssh.calls)

    def test_read_text_extension_allowed(self, provider):
        result = asyncio.run(provider.read("/srv/app/hello.txt", "hello.txt"))
        assert result["content"] == "hello world"

    def test_download_binary_extension_blocked(self, ssh, remote_fs):
        remote_fs.add_file("/srv/app/archive.zip", "fake zip")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(FileTypeError, match="binary"):
            asyncio.run(prov.download("/srv/app/archive.zip"))

    def test_download_text_extension_allowed(self, provider):
        result = asyncio.run(provider.download("/srv/app/hello.txt"))
        assert result["content"] == b"hello world"


# ---------------------------------------------------------------------------
# Size limit tests (additional coverage)
# ---------------------------------------------------------------------------


class TestSizeLimits:
    """Size limits on read/write are enforced."""

    def test_read_within_limit(self, ssh, remote_fs):
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=1024)
        result = asyncio.run(prov.read("/srv/app/hello.txt", "hello.txt"))
        assert "content" in result

    def test_read_exceeds_limit(self, ssh, remote_fs):
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=3)
        with pytest.raises(FileTypeError, match="too large"):
            asyncio.run(prov.read("/srv/app/hello.txt", "hello.txt"))

    def test_write_within_limit(self, ssh, remote_fs):
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=1024)
        result = asyncio.run(prov.write("/srv/app/hello.txt", "ok", None))
        assert result["ok"] is True

    def test_write_exceeds_limit(self, ssh, remote_fs):
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=3)
        with pytest.raises(FileTypeError, match="exceeds"):
            asyncio.run(prov.write("/srv/app/hello.txt", "long content", None))

    def test_download_exceeds_limit(self, ssh, remote_fs):
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=3)
        with pytest.raises(FileTypeError, match="too large"):
            asyncio.run(prov.download("/srv/app/hello.txt"))


# ---------------------------------------------------------------------------
# Policy integration: all policies applied consistently
# ---------------------------------------------------------------------------


class TestPolicyIntegration:
    """Verify all policies are applied to all relevant operations."""

    def test_write_binary_extension_blocked(self, ssh, remote_fs):
        """write rejects binary extension before any SSH write."""
        remote_fs.add_file("/srv/app/image.png", "fake")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(FileTypeError, match="binary"):
            asyncio.run(prov.write("/srv/app/image.png", "x", None))

    def test_create_parent_symlink_outside_root_blocked(self, ssh, remote_fs):
        """create checks parent symlink doesn't escape root."""
        remote_fs.add_symlink("/srv/app/escape", "/tmp")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(PathDeniedError, match="symlink target escapes"):
            asyncio.run(prov.create("/srv/app/escape/new.txt", "file"))

    def test_create_parent_symlink_inside_root_allowed(self, ssh, remote_fs):
        """create allows parent symlink if target is within root."""
        remote_fs.add_symlink("/srv/app/link_sub", "/srv/app/sub")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        result = asyncio.run(prov.create("/srv/app/link_sub/new.txt", "file"))
        assert result["ok"] is True

    def test_path_validation_then_binary_then_symlink_order(self, ssh, remote_fs):
        """Path validation runs first, then binary check, then symlink."""
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        # Path outside root → PathValidationError (not FileTypeError)
        with pytest.raises(PathValidationError, match="escapes root"):
            asyncio.run(prov.read("/etc/image.png", "image.png"))

    def test_all_operations_validate_path(self, ssh, remote_fs):
        """Every operation rejects paths outside root."""
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        outside = "/etc/passwd"
        for op_name, op in [
            ("tree", lambda: asyncio.run(prov.tree(outside))),
            ("read", lambda: asyncio.run(prov.read(outside, "x"))),
            ("write", lambda: asyncio.run(prov.write(outside, "x", None))),
            ("download", lambda: asyncio.run(prov.download(outside))),
            ("create", lambda: asyncio.run(prov.create(outside, "file"))),
            ("delete", lambda: asyncio.run(prov.delete(outside))),
            ("meta", lambda: asyncio.run(prov.meta(outside, "x"))),
        ]:
            with pytest.raises(PathValidationError, match="escapes root"):
                op()

    def test_symlink_policy_on_read_download_delete_meta(self, ssh, remote_fs):
        """Symlink escaping root is blocked on read, download, delete, meta."""
        remote_fs.add_symlink("/srv/app/evil", "/etc/passwd")
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app")
        with pytest.raises(PathDeniedError):
            asyncio.run(prov.read("/srv/app/evil", "evil"))
        with pytest.raises(PathDeniedError):
            asyncio.run(prov.download("/srv/app/evil"))
        with pytest.raises(PathDeniedError):
            asyncio.run(prov.delete("/srv/app/evil"))
        with pytest.raises(PathDeniedError):
            asyncio.run(prov.meta("/srv/app/evil", "evil"))

    def test_tree_hidden_files_filter(self, provider):
        """tree filters hidden files when show_hidden=False."""
        result = asyncio.run(provider.tree("/srv/app", show_hidden=False))
        names = [i["name"] for i in result["items"]]
        assert ".env" not in names
        assert "hello.txt" in names

    def test_combined_binary_and_size_limit(self, ssh, remote_fs):
        """Binary check runs before size check (early reject)."""
        prov = RemoteFilesProvider(ssh, "/w", "prod", "/srv/app", max_file_size_bytes=999999)
        remote_fs.add_file("/srv/app/big.pdf", "x" * 100)
        with pytest.raises(FileTypeError, match="binary"):
            asyncio.run(prov.read("/srv/app/big.pdf", "big.pdf"))
        # Binary rejection happens before SSH content fetch
        assert not any("base64" in c for c in ssh.calls)
