import asyncio
import difflib
import hashlib
import inspect
import logging
import os
import posixpath
import re
import shutil
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.security import DenyReasonCode, SecurityValidationError
from session import Session

logger = logging.getLogger(__name__)


class FilesServiceError(Exception):
    status = 400


class SessionUidRequiredError(FilesServiceError):
    status = 400


class SessionNotFoundError(FilesServiceError):
    status = 404


class PathDeniedError(FilesServiceError):
    status = 403


class PathValidationError(FilesServiceError):
    status = 400


class RevisionConflictError(FilesServiceError):
    status = 409

    def __init__(
        self,
        message: str = "revision mismatch",
        *,
        expected_revision: Optional[str] = None,
        current_revision: Optional[str] = None,
        current_content: Optional[str] = None,
        diff_unified: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        self.current_content = current_content
        self.diff_unified = diff_unified


class FileTypeError(FilesServiceError):
    status = 400


class FilesSearchError(FilesServiceError):
    status = 500


class FilesRemoteTargetUnavailableError(FilesServiceError):
    status = 409


# Extensions considered binary — editing/download of these is blocked.
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mkv", ".mov",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".pyc", ".pyo", ".class", ".jar", ".war",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".sqlite", ".db",
})


def _is_binary_by_extension(path: str) -> bool:
    """Check if file is considered binary based on its extension."""
    _, ext = os.path.splitext(path)
    return ext.lower() in BINARY_EXTENSIONS


class LocalFilesProvider:
    """File operations provider that uses a local root directory.

    All paths are resolved relative to *root*.  The provider performs no
    session resolution or security checks — callers are expected to supply
    an already-validated *resolved_path* or use the helper ``_safe_path``
    for simple root-relative resolution.
    """

    def __init__(self, root: str, max_file_size_bytes: int = 5 * 1024 * 1024) -> None:
        self._root = os.path.realpath(root)
        self._max_file_size_bytes = max_file_size_bytes

    @property
    def root(self) -> str:
        return self._root

    @property
    def max_file_size_bytes(self) -> int:
        return self._max_file_size_bytes

    def _safe_path(self, rel_path: str) -> str:
        """Resolve *rel_path* against root, raising on traversal."""
        joined = os.path.normpath(os.path.join(self._root, rel_path))
        real = os.path.realpath(joined)
        if not (real == self._root or real.startswith(self._root + os.sep)):
            raise PathValidationError("path escapes root")
        return real

    @staticmethod
    def _revision(path: str) -> str:
        with open(path, "rb") as f:
            data = f.read()
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _is_binary(path: str) -> bool:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk

    def tree(
        self,
        resolved_path: str,
        *,
        show_hidden: bool = True,
        respect_gitignore: bool = False,
    ) -> Dict[str, Any]:
        if not os.path.exists(resolved_path):
            raise PathValidationError("path not found")
        if not os.path.isdir(resolved_path):
            raise PathValidationError("path is not a directory")

        # Collect gitignored paths if requested
        ignored: set = set()
        if respect_gitignore:
            ignored = self._git_ignored_names(resolved_path)

        items: List[Dict[str, Any]] = []
        for entry in os.listdir(resolved_path):
            if not show_hidden and entry.startswith("."):
                continue
            if respect_gitignore and entry in ignored:
                continue
            full = os.path.join(resolved_path, entry)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, self._root)
            items.append(
                {
                    "name": entry,
                    "path": "." if rel == "." else rel.replace("\\", "/"),
                    "is_dir": os.path.isdir(full),
                    "size": int(st.st_size),
                    "mtime": int(st.st_mtime),
                }
            )
        items.sort(key=lambda item: (not bool(item.get("is_dir")), str(item.get("name", "")).lower()))
        return {
            "root": self._root,
            "path": "." if resolved_path == self._root else os.path.relpath(resolved_path, self._root).replace("\\", "/"),
            "items": items,
        }

    def _git_ignored_names(self, dir_path: str) -> set:
        """Return set of entry names in *dir_path* that are git-ignored."""
        import subprocess
        entries = os.listdir(dir_path)
        if not entries:
            return set()
        try:
            result = subprocess.run(
                ["git", "check-ignore", "--"] + entries,
                cwd=dir_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return set()
        return {os.path.basename(line.strip()) for line in result.stdout.splitlines() if line.strip()}

    def read(self, resolved_path: str, rel_path: str) -> Dict[str, Any]:
        if not os.path.isfile(resolved_path):
            raise PathValidationError("file not found")
        if _is_binary_by_extension(resolved_path):
            raise FileTypeError("binary files are not editable")
        size = os.path.getsize(resolved_path)
        if size > self._max_file_size_bytes:
            raise FileTypeError(f"file is too large: {size} bytes")
        if self._is_binary(resolved_path):
            raise FileTypeError("binary files are not editable")
        try:
            with open(resolved_path, "r", encoding="utf-8", errors="strict") as f:
                text = f.read()
        except UnicodeDecodeError as exc:
            raise FileTypeError("file is not UTF-8") from exc

        st = os.stat(resolved_path)
        return {
            "content": text,
            "revision": self._revision(resolved_path),
            "meta": {
                "path": rel_path,
                "size": int(st.st_size),
                "mtime": int(st.st_mtime),
            },
        }

    def write(self, resolved_path: str, content: str, expected_revision: Optional[str], *, force: bool = False) -> Dict[str, Any]:
        if not os.path.isfile(resolved_path):
            raise PathValidationError("file not found")

        old_revision = self._revision(resolved_path)

        if not force and expected_revision:
            if old_revision != expected_revision:
                try:
                    with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
                        current_text = f.read()
                except OSError:
                    current_text = ""
                diff = "\n".join(difflib.unified_diff(
                    (content or "").splitlines(keepends=True),
                    current_text.splitlines(keepends=True),
                    fromfile="yours", tofile="current", lineterm="",
                ))
                raise RevisionConflictError(
                    "revision mismatch",
                    expected_revision=expected_revision,
                    current_revision=old_revision,
                    current_content=current_text,
                    diff_unified=diff,
                )

        payload = str(content or "")
        if len(payload.encode("utf-8")) > self._max_file_size_bytes:
            raise FileTypeError("content exceeds configured file size limit")

        tmp = f"{resolved_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, resolved_path)

        new_revision = self._revision(resolved_path)
        result: Dict[str, Any] = {"ok": True, "revision": new_revision}
        if force:
            result["forced"] = True
            result["old_revision"] = old_revision
        return result

    def download(
        self,
        resolved_path: str,
        *,
        allow_binary: bool = False,
        max_file_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not os.path.isfile(resolved_path):
            raise PathValidationError("file not found")
        if not allow_binary and _is_binary_by_extension(resolved_path):
            raise FileTypeError("binary files are not editable")

        limit = self._max_file_size_bytes if max_file_size_bytes is None else int(max_file_size_bytes)
        size = os.path.getsize(resolved_path)
        if size > limit:
            raise FileTypeError(f"file is too large: {size} bytes")
        if not allow_binary and self._is_binary(resolved_path):
            raise FileTypeError("binary files are not editable")
        with open(resolved_path, "rb") as f:
            raw = f.read()
        if not allow_binary:
            try:
                raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise FileTypeError("file is not UTF-8") from exc

        filename = os.path.basename(resolved_path) or "download.txt"
        return {
            "content": raw,
            "filename": filename,
            "size": int(size),
        }

    def create(self, resolved_path: str, kind: str) -> Dict[str, Any]:
        if os.path.exists(resolved_path):
            raise PathValidationError("path already exists")

        if kind == "file":
            parent = os.path.dirname(resolved_path)
            os.makedirs(parent, exist_ok=True)
            Path(resolved_path).touch(exist_ok=False)
        elif kind == "dir":
            os.makedirs(resolved_path, exist_ok=False)
        else:
            raise PathValidationError("kind must be 'file' or 'dir'")

        return {"ok": True}

    def delete(self, resolved_path: str, *, recursive: bool = False) -> Dict[str, Any]:
        if not os.path.exists(resolved_path):
            raise PathValidationError("path not found")
        if os.path.isdir(resolved_path):
            if recursive:
                shutil.rmtree(resolved_path)
            else:
                os.rmdir(resolved_path)
        else:
            os.remove(resolved_path)
        return {"ok": True}

    def meta(self, resolved_path: str, rel_path: str) -> Dict[str, Any]:
        if not os.path.exists(resolved_path):
            raise PathValidationError("path not found")
        st = os.stat(resolved_path)
        return {
            "path": rel_path,
            "exists": True,
            "is_dir": os.path.isdir(resolved_path),
            "size": int(st.st_size),
            "mtime": int(st.st_mtime),
        }


class RemoteFilesProvider:
    """File operations provider that works over SSH/SFTP via :class:`SSHService`.

    Mirrors the :class:`LocalFilesProvider` interface but executes all I/O on
    a remote host.  ``resolved_path`` arguments are absolute paths *on the
    remote machine*.
    """

    def __init__(
        self,
        ssh_service: Any,
        workdir: str,
        host_alias: str,
        remote_root: str,
        max_file_size_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self._ssh = ssh_service
        self._workdir = workdir
        self._alias = host_alias
        self._root = remote_root
        self._max_file_size_bytes = max_file_size_bytes
        self._root_real: Optional[str] = None

    @property
    def root(self) -> str:
        return self._root

    @property
    def max_file_size_bytes(self) -> int:
        return self._max_file_size_bytes

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _exec(self, command: str, *, timeout_sec: int = 30) -> Any:
        return await self._ssh.exec(
            self._workdir, self._alias, command, timeout_sec=timeout_sec,
        )

    @staticmethod
    def _q(path: str) -> str:
        return shlex.quote(str(path or ""))

    @classmethod
    def _q_glob(cls, path: str, suffix: str) -> str:
        return f"{cls._q(path)}{suffix}"

    def _safe_path(self, rel_path: str) -> str:
        """Resolve *rel_path* against remote root, raising on traversal."""
        joined = posixpath.normpath(posixpath.join(self._root, rel_path))
        if not (joined == self._root or joined.startswith(self._root + "/")):
            raise PathValidationError("path escapes root")
        return joined

    def _validate_resolved_path(self, resolved_path: str) -> str:
        """Normalize *resolved_path* and verify it stays within the remote root."""
        normed = posixpath.normpath(resolved_path)
        if not (normed == self._root or normed.startswith(self._root + "/")):
            raise PathValidationError("path escapes root")
        return normed

    @staticmethod
    def _path_within(base: str, path: str) -> bool:
        return path == base or path.startswith(base + "/")

    async def _remote_realpath(self, path: str) -> str:
        r = await self._exec(f"readlink -f {self._q(path)}")
        target = (r.stdout or "").strip()
        if r.exit_code != 0 or not target:
            raise PathValidationError("path not found")
        return posixpath.normpath(target)

    async def _remote_root_realpath(self) -> str:
        if self._root_real is None:
            self._root_real = await self._remote_realpath(self._root)
        return self._root_real

    async def _ensure_realpath_within_root(self, resolved_path: str) -> str:
        root_real = await self._remote_root_realpath()
        real = await self._remote_realpath(resolved_path)
        if not self._path_within(root_real, real):
            raise PathDeniedError("symlink target escapes remote_project_root")
        return real

    async def _check_symlink_target(self, resolved_path: str) -> None:
        """If *resolved_path* is a symlink, verify its target stays within root."""
        q_path = self._q(resolved_path)
        r = await self._exec(f"test -L {q_path} && readlink -f {q_path} || echo NOT_LINK")
        target = (r.stdout or "").strip()
        if target == "NOT_LINK":
            return
        normed = posixpath.normpath(target)
        root_real = await self._remote_root_realpath()
        if not self._path_within(root_real, normed):
            raise PathDeniedError("symlink target escapes remote_project_root")

    async def _deny_symlink_write(self, resolved_path: str) -> None:
        """Reject write operations on symlinks."""
        r = await self._exec(f"test -L {self._q(resolved_path)} && echo LINK || echo OK")
        if (r.stdout or "").strip() == "LINK":
            raise PathDeniedError("write through symlink is not allowed")

    @staticmethod
    def _revision_from_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    async def _git_ignored_names(self, dir_path: str) -> set:
        """Return set of entry names in *dir_path* that are git-ignored on remote."""
        r = await self._exec(
            f"cd {self._q(dir_path)} && ls -A 2>/dev/null | xargs -r git check-ignore -- 2>/dev/null || true",
            timeout_sec=10,
        )
        return {posixpath.basename(line.strip()) for line in (r.stdout or "").splitlines() if line.strip()}

    # ------------------------------------------------------------------
    # tree
    # ------------------------------------------------------------------

    async def tree(
        self,
        resolved_path: str,
        *,
        show_hidden: bool = True,
        respect_gitignore: bool = False,
    ) -> Dict[str, Any]:
        resolved_path = self._validate_resolved_path(resolved_path)
        await self._ensure_realpath_within_root(resolved_path)
        q_path = self._q(resolved_path)
        r = await self._exec(f"test -d {q_path} && echo DIR || echo MISSING")
        tag = (r.stdout or "").strip()
        if tag != "DIR":
            if tag == "MISSING" or r.exit_code != 0:
                raise PathValidationError("path not found")
            raise PathValidationError("path is not a directory")

        # Collect gitignored names if requested
        ignored: set = set()
        if respect_gitignore:
            ignored = await self._git_ignored_names(resolved_path)

        # List entries with stat info: type size mtime name
        r = await self._exec(
            f"stat -c '%F|%s|%Y|%n' {self._q_glob(resolved_path, '/*')} "
            f"{self._q_glob(resolved_path, '/.*')} 2>/dev/null || true",
            timeout_sec=15,
        )
        items: List[Dict[str, Any]] = []
        for line in (r.stdout or "").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            ftype, size_s, mtime_s, full_path = parts
            name = posixpath.basename(full_path)
            if name in (".", ".."):
                continue
            if not show_hidden and name.startswith("."):
                continue
            if respect_gitignore and name in ignored:
                continue
            rel = posixpath.relpath(full_path, self._root)
            is_dir = "directory" in ftype.lower()
            items.append({
                "name": name,
                "path": "." if rel == "." else rel,
                "is_dir": is_dir,
                "size": int(size_s),
                "mtime": int(mtime_s),
            })
        items.sort(key=lambda item: (not bool(item.get("is_dir")), str(item.get("name", "")).lower()))
        return {
            "root": self._root,
            "path": "." if resolved_path == self._root else posixpath.relpath(resolved_path, self._root),
            "items": items,
        }

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    async def read(self, resolved_path: str, rel_path: str) -> Dict[str, Any]:
        resolved_path = self._validate_resolved_path(resolved_path)
        if _is_binary_by_extension(resolved_path):
            raise FileTypeError("binary files are not editable")
        await self._ensure_realpath_within_root(resolved_path)
        await self._check_symlink_target(resolved_path)
        q_path = self._q(resolved_path)
        r = await self._exec(f"test -f {q_path} && echo OK || echo MISSING")
        if (r.stdout or "").strip() != "OK":
            raise PathValidationError("file not found")

        # Get size
        r = await self._exec(f"stat -c '%s %Y' {q_path}")
        parts = (r.stdout or "").strip().split()
        if len(parts) < 2:
            raise PathValidationError("file not found")
        size = int(parts[0])
        mtime = int(parts[1])

        if size > self._max_file_size_bytes:
            raise FileTypeError(f"file is too large: {size} bytes")

        # Read content via cat (base64 to handle any encoding)
        r = await self._exec(f"base64 {q_path}", timeout_sec=30)
        if r.exit_code != 0:
            raise PathValidationError(f"failed to read file: {r.stderr}")

        import base64
        raw = base64.b64decode(r.stdout or "")

        if b"\x00" in raw[:8192]:
            raise FileTypeError("binary files are not editable")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FileTypeError("file is not UTF-8") from exc

        return {
            "content": text,
            "revision": self._revision_from_bytes(raw),
            "meta": {
                "path": rel_path,
                "size": size,
                "mtime": mtime,
            },
        }

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    async def write(self, resolved_path: str, content: str, expected_revision: Optional[str], *, force: bool = False) -> Dict[str, Any]:
        resolved_path = self._validate_resolved_path(resolved_path)
        if _is_binary_by_extension(resolved_path):
            raise FileTypeError("binary files are not editable")
        await self._ensure_realpath_within_root(resolved_path)
        await self._deny_symlink_write(resolved_path)
        q_path = self._q(resolved_path)
        r = await self._exec(f"test -f {q_path} && echo OK || echo MISSING")
        if (r.stdout or "").strip() != "OK":
            raise PathValidationError("file not found")

        # Get old revision for force save audit
        r = await self._exec(f"sha256sum {q_path}")
        old_revision = (r.stdout or "").strip().split()[0] if r.exit_code == 0 else ""

        if not force and expected_revision:
            if old_revision != expected_revision:
                import base64 as b64mod
                r2 = await self._exec(f"base64 {q_path}", timeout_sec=15)
                current_text = ""
                if r2.exit_code == 0:
                    try:
                        current_text = b64mod.b64decode(r2.stdout or "").decode("utf-8", errors="replace")
                    except Exception:
                        current_text = ""
                diff = "\n".join(difflib.unified_diff(
                    (content or "").splitlines(keepends=True),
                    current_text.splitlines(keepends=True),
                    fromfile="yours", tofile="current", lineterm="",
                ))
                raise RevisionConflictError(
                    "revision mismatch",
                    expected_revision=expected_revision,
                    current_revision=old_revision,
                    current_content=current_text,
                    diff_unified=diff,
                )

        payload = str(content or "")
        encoded = payload.encode("utf-8")
        if len(encoded) > self._max_file_size_bytes:
            raise FileTypeError("content exceeds configured file size limit")

        import base64
        b64 = base64.b64encode(encoded).decode("ascii")
        tmp = f"{resolved_path}.tmp"
        q_tmp = self._q(tmp)
        r = await self._exec(
            f"echo '{b64}' | base64 -d > {q_tmp} && mv {q_tmp} {q_path}",
            timeout_sec=30,
        )
        if r.exit_code != 0:
            raise PathValidationError(f"failed to write file: {r.stderr}")

        r = await self._exec(f"sha256sum {q_path}")
        new_revision = (r.stdout or "").strip().split()[0] if r.exit_code == 0 else ""
        result: Dict[str, Any] = {"ok": True, "revision": new_revision}
        if force:
            result["forced"] = True
            result["old_revision"] = old_revision
        return result

    # ------------------------------------------------------------------
    # download
    # ------------------------------------------------------------------

    async def download(
        self,
        resolved_path: str,
        *,
        allow_binary: bool = False,
        max_file_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        resolved_path = self._validate_resolved_path(resolved_path)
        if not allow_binary and _is_binary_by_extension(resolved_path):
            raise FileTypeError("binary files are not editable")
        await self._ensure_realpath_within_root(resolved_path)
        await self._check_symlink_target(resolved_path)
        q_path = self._q(resolved_path)
        r = await self._exec(f"test -f {q_path} && echo OK || echo MISSING")
        if (r.stdout or "").strip() != "OK":
            raise PathValidationError("file not found")

        r = await self._exec(f"stat -c '%s' {q_path}")
        size = int((r.stdout or "0").strip())
        limit = self._max_file_size_bytes if max_file_size_bytes is None else int(max_file_size_bytes)
        if size > limit:
            raise FileTypeError(f"file is too large: {size} bytes")

        import base64
        r = await self._exec(f"base64 {q_path}", timeout_sec=30)
        if r.exit_code != 0:
            raise PathValidationError(f"failed to read file: {r.stderr}")

        raw = base64.b64decode(r.stdout or "")
        if not allow_binary and b"\x00" in raw[:8192]:
            raise FileTypeError("binary files are not editable")
        if not allow_binary:
            try:
                raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise FileTypeError("file is not UTF-8") from exc

        filename = posixpath.basename(resolved_path) or "download.txt"
        return {
            "content": raw,
            "filename": filename,
            "size": size,
        }

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    async def create(self, resolved_path: str, kind: str) -> Dict[str, Any]:
        resolved_path = self._validate_resolved_path(resolved_path)
        parent = posixpath.dirname(resolved_path)
        if parent != resolved_path:
            await self._ensure_realpath_within_root(parent)
            await self._check_symlink_target(parent)
        q_path = self._q(resolved_path)
        r = await self._exec(f"test -e {q_path} && echo EXISTS || echo OK")
        if (r.stdout or "").strip() == "EXISTS":
            raise PathValidationError("path already exists")

        if kind == "file":
            parent = posixpath.dirname(resolved_path)
            r = await self._exec(f"mkdir -p {self._q(parent)} && touch {q_path}")
        elif kind == "dir":
            r = await self._exec(f"mkdir {q_path}")
        else:
            raise PathValidationError("kind must be 'file' or 'dir'")

        if r.exit_code != 0:
            raise PathValidationError(f"failed to create: {r.stderr}")
        return {"ok": True}

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, resolved_path: str, *, recursive: bool = False) -> Dict[str, Any]:
        resolved_path = self._validate_resolved_path(resolved_path)
        await self._ensure_realpath_within_root(resolved_path)
        await self._check_symlink_target(resolved_path)
        q_path = self._q(resolved_path)
        r = await self._exec(f"test -e {q_path} && echo OK || echo MISSING")
        if (r.stdout or "").strip() != "OK":
            raise PathValidationError("path not found")

        delete_cmd = f"rm -rf {q_path}" if recursive else f"test -d {q_path} && rmdir {q_path} || rm {q_path}"
        r = await self._exec(delete_cmd)
        if r.exit_code != 0:
            raise PathValidationError(f"failed to delete: {r.stderr}")
        return {"ok": True}

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------

    async def meta(self, resolved_path: str, rel_path: str) -> Dict[str, Any]:
        resolved_path = self._validate_resolved_path(resolved_path)
        await self._ensure_realpath_within_root(resolved_path)
        await self._check_symlink_target(resolved_path)
        r = await self._exec(f"stat -c '%F|%s|%Y' {self._q(resolved_path)} 2>/dev/null")
        if r.exit_code != 0:
            raise PathValidationError("path not found")

        parts = (r.stdout or "").strip().split("|")
        if len(parts) < 3:
            raise PathValidationError("path not found")

        ftype = parts[0]
        is_dir = "directory" in ftype.lower()
        return {
            "path": rel_path,
            "exists": True,
            "is_dir": is_dir,
            "size": int(parts[1]),
            "mtime": int(parts[2]),
        }


@dataclass
class SessionFilesService:
    """Session-aware file service that selects between Local and Remote providers.

    Handles session resolution, security validation, provider selection based
    on effective execution target, and logging.
    """

    app: Any

    @property
    def max_file_size_bytes(self) -> int:
        kb = int(getattr(self.app.config.miniapp, "max_edit_file_size_kb", 5120))
        return max(1, kb) * 1024

    @staticmethod
    def _parse_session_uid(session_uid: str) -> tuple[int, str]:
        raw = str(session_uid or "").strip()
        if not raw:
            raise SessionUidRequiredError("session_uid is required")
        match = re.fullmatch(r"(-?\d+):([^:\s]+)", raw)
        if not match:
            raise PathValidationError("invalid session_uid")
        return (int(match.group(1)), str(match.group(2)))

    def _session_by_uid(self, session_uid: str) -> Session:
        manager = getattr(self.app, "manager", None)
        if not manager:
            raise SessionNotFoundError("session not found")
        session = None
        if hasattr(manager, "get_by_uid"):
            try:
                session = manager.get_by_uid(str(session_uid))
            except Exception:
                logger.exception("session files failed to resolve session by uid via manager.get_by_uid")
                session = None
        if not session:
            chat_id, session_id = self._parse_session_uid(session_uid)
            if hasattr(manager, "get"):
                try:
                    session = manager.get(int(chat_id), str(session_id))
                except Exception:
                    logger.exception("session files failed to resolve session by uid via manager.get")
                    session = None
        if not session:
            raise SessionNotFoundError("session not found")
        return session

    def _root_real(self, session_uid: str) -> str:
        session = self._session_by_uid(session_uid)
        return os.path.realpath(session.workdir)

    def _is_admin_user(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        checker = getattr(self.app, "is_admin", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(int(user_id)))
        except Exception:
            logger.exception("session files admin check failed user_id=%s", user_id)
            return False

    def _ensure_remote_host_allowed(self, hosts: Dict[str, Any], host_alias: str, user_id: Optional[int]) -> None:
        if user_id is None or self._is_admin_user(user_id):
            return
        cfg = hosts.get(str(host_alias or ""))
        if cfg is None:
            return
        acl = getattr(cfg, "allowed_chat_ids", None)
        if acl is not None and int(user_id) not in {int(item) for item in acl}:
            raise PathDeniedError("remote host is not allowed for this user")

    def _provider(self, session_uid: str, *, user_id: Optional[int] = None):
        """Select provider based on effective execution target of the session."""
        session = self._session_by_uid(session_uid)
        rc_svc = getattr(self.app, "remote_control_service", None)
        if rc_svc is not None:
            from app.services.remote_control_service import ExecutionTarget
            from app.services.ssh_config_loader import load_ssh_config
            from sessions.session_state_access import is_remote_control_enabled
            workdir = str(getattr(session, "workdir", "") or "").strip()
            hosts = load_ssh_config(workdir) if workdir else {}
            effective = rc_svc.compute_effective_state(session, hosts)
            rc_enabled = is_remote_control_enabled(session)
            if rc_enabled and effective.execution_target != ExecutionTarget.REMOTE:
                raise FilesServiceError("Remote Control is enabled but the remote target is unavailable")
            if effective.execution_target == ExecutionTarget.REMOTE:
                self._ensure_remote_host_allowed(hosts, str(effective.host_alias or ""), user_id)
                ssh_svc = getattr(self.app, "ssh_service", None)
                if ssh_svc is None:
                    raise FilesServiceError("SSH service unavailable for remote file operations")
                if not effective.host_alias or not effective.remote_project_root:
                    raise FilesServiceError(
                        "Remote Control is enabled but the selected host is not eligible for remote workspace operations"
                    )
                return RemoteFilesProvider(
                    ssh_service=ssh_svc,
                    workdir=workdir,
                    host_alias=effective.host_alias,
                    remote_root=effective.remote_project_root,
                    max_file_size_bytes=self.max_file_size_bytes,
                )
        return LocalFilesProvider(os.path.realpath(session.workdir), self.max_file_size_bytes)

    def execution_context(self, session_uid: str, *, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Return resolved execution context for *session_uid*."""
        session = self._session_by_uid(session_uid)
        workdir = str(getattr(session, "workdir", "") or "").strip()
        rc_svc = getattr(self.app, "remote_control_service", None)
        if rc_svc is None:
            return {
                "remote_control_enabled": False,
                "execution_target": "local",
                "host_alias": None,
                "remote_project_root": None,
            }
        from app.services.ssh_config_loader import load_ssh_config
        from sessions.session_state_access import is_remote_control_enabled

        hosts = load_ssh_config(workdir) if workdir else {}
        effective = rc_svc.compute_effective_state(session, hosts)
        if effective.host_alias:
            self._ensure_remote_host_allowed(hosts, str(effective.host_alias or ""), user_id)
        return {
            "remote_control_enabled": bool(is_remote_control_enabled(session)),
            "execution_target": effective.execution_target.value,
            "host_alias": effective.host_alias,
            "remote_project_root": effective.remote_project_root,
        }

    def _config_real(self) -> str:
        return os.path.realpath(self.app.config.path)

    def _security(self):
        security = getattr(self.app, "security", None)
        if security is None or not hasattr(security, "resolve_path"):
            raise RuntimeError("SecurityFacade.resolve_path is not configured for SessionFilesService")
        return security

    def _map_validation_error(self, exc: SecurityValidationError) -> FilesServiceError:
        code = str(exc.code or "")
        if code == DenyReasonCode.PATH_ESCAPES_ROOT:
            return PathValidationError("path escapes active session root")

        base_name = str(exc.details.get("base_name") or "").strip().lower()
        if code == DenyReasonCode.PROTECTED_PATH:
            return PathDeniedError("config.yaml can only be edited via Config UI")
        if code == DenyReasonCode.PROTECTED_NAME:
            if base_name == "config.yaml":
                return PathDeniedError("config.yaml can only be edited via Config UI")
            if base_name == ".env" or base_name.startswith(".env."):
                return PathDeniedError(".env is protected")
            return PathDeniedError("sensitive file is protected")
        if code == DenyReasonCode.PROTECTED_EXTENSION:
            return PathDeniedError("sensitive file is protected")
        return PathValidationError(str(exc))

    def _resolve_path(
        self,
        user_id: int,
        session_uid: str,
        rel_path: str,
        *,
        root_override: Optional[str] = None,
        protect_sensitive: bool = True,
    ) -> str:
        root = str(root_override or self._root_real(session_uid) or "").strip()
        context = {
            "user_id": int(user_id),
            "session_uid": str(session_uid),
        }
        deny_names = ()
        deny_extensions = ()
        if protect_sensitive:
            deny_names = ("config.yaml", ".env", "id_rsa", "id_ed25519")
            deny_extensions = (".pem", ".key")
            context.update({
                "protected_paths": (self._config_real(),),
                "protected_name_prefixes": (".env.",),
            })
        try:
            result = self._security().resolve_path(
                root,
                str(rel_path or ""),
                deny_names=deny_names,
                deny_extensions=deny_extensions,
                context=context,
            )
        except SecurityValidationError as exc:
            raise self._map_validation_error(exc) from exc
        return str(result.resolved_path)

    def tree(
        self,
        user_id: int,
        session_uid: str,
        rel_path: str,
        *,
        protect_sensitive: bool = True,
    ) -> Dict[str, Any]:
        provider = self._provider(session_uid, user_id=int(user_id))
        root = getattr(provider, "root", None)
        path = self._resolve_path(
            user_id,
            session_uid,
            rel_path,
            root_override=root,
            protect_sensitive=protect_sensitive,
        )
        payload = provider.tree(path)
        logger.info(
            "session file tree",
            extra={"chat_id": user_id, "user_id": user_id, "action": "files_tree", "path": rel_path or ".", "status": "ok", "error": ""},
        )
        return payload

    def read(self, user_id: int, session_uid: str, rel_path: str) -> Dict[str, Any]:
        provider = self._provider(session_uid, user_id=int(user_id))
        root = getattr(provider, "root", None)
        path = self._resolve_path(user_id, session_uid, rel_path, root_override=root)
        payload = provider.read(path, rel_path)
        logger.info(
            "session file read",
            extra={"chat_id": user_id, "user_id": user_id, "action": "files_read", "path": rel_path, "status": "ok", "error": ""},
        )
        return payload

    def write(
        self, user_id: int, session_uid: str, rel_path: str,
        content: str, expected_revision: Optional[str], *, force: bool = False,
    ) -> Dict[str, Any]:
        provider = self._provider(session_uid, user_id=int(user_id))
        root = getattr(provider, "root", None)
        path = self._resolve_path(user_id, session_uid, rel_path, root_override=root)
        payload = provider.write(path, content, expected_revision, force=force)
        if inspect.isawaitable(payload):
            async def _await_and_log() -> Dict[str, Any]:
                resolved = await payload
                logger.info(
                    "session file write",
                    extra={
                        "chat_id": user_id,
                        "user_id": user_id,
                        "action": "files_write",
                        "path": rel_path,
                        "status": "ok",
                        "error": "",
                    },
                )
                return resolved

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(_await_and_log())
            return _await_and_log()
        logger.info(
            "session file write",
            extra={"chat_id": user_id, "user_id": user_id, "action": "files_write", "path": rel_path, "status": "ok", "error": ""},
        )
        return payload

    def download(
        self,
        user_id: int,
        session_uid: str,
        rel_path: str,
        *,
        allow_binary: bool = False,
        max_size_bytes: Optional[int] = None,
        protect_sensitive: bool = True,
    ) -> Dict[str, Any]:
        provider = self._provider(session_uid, user_id=int(user_id))
        root = getattr(provider, "root", None)
        path = self._resolve_path(
            user_id,
            session_uid,
            rel_path,
            root_override=root,
            protect_sensitive=protect_sensitive,
        )
        payload = provider.download(
            path,
            allow_binary=allow_binary,
            max_file_size_bytes=max_size_bytes,
        )
        logger.info(
            "session file download",
            extra={"chat_id": user_id, "user_id": user_id, "action": "files_download", "path": rel_path, "status": "ok", "error": ""},
        )
        return payload

    def create(self, user_id: int, session_uid: str, rel_path: str, kind: str) -> Dict[str, Any]:
        provider = self._provider(session_uid, user_id=int(user_id))
        root = getattr(provider, "root", None)
        path = self._resolve_path(user_id, session_uid, rel_path, root_override=root)
        payload = provider.create(path, kind)
        logger.info(
            "session file create",
            extra={"chat_id": user_id, "user_id": user_id, "action": "files_create", "path": rel_path, "status": "ok", "error": ""},
        )
        return payload

    def delete(
        self,
        user_id: int,
        session_uid: str,
        rel_path: str,
        *,
        recursive: bool = False,
        protect_sensitive: bool = True,
    ) -> Dict[str, Any]:
        if not bool(getattr(self.app.config.miniapp, "enable_delete", True)):
            raise PathDeniedError("delete is disabled by configuration")

        provider = self._provider(session_uid, user_id=int(user_id))
        root = getattr(provider, "root", None)
        path = self._resolve_path(
            user_id,
            session_uid,
            rel_path,
            root_override=root,
            protect_sensitive=protect_sensitive,
        )
        payload = provider.delete(path, recursive=recursive)
        logger.info(
            "session file delete",
            extra={"chat_id": user_id, "user_id": user_id, "action": "files_delete", "path": rel_path, "status": "ok", "error": ""},
        )
        return payload

    def meta(
        self,
        user_id: int,
        session_uid: str,
        rel_path: str,
        *,
        protect_sensitive: bool = True,
    ) -> Dict[str, Any]:
        provider = self._provider(session_uid, user_id=int(user_id))
        root = getattr(provider, "root", None)
        path = self._resolve_path(
            user_id,
            session_uid,
            rel_path,
            root_override=root,
            protect_sensitive=protect_sensitive,
        )
        payload = provider.meta(path, rel_path)
        logger.info(
            "session file meta",
            extra={"chat_id": user_id, "user_id": user_id, "action": "files_meta", "path": rel_path, "status": "ok", "error": ""},
        )
        return payload

    @staticmethod
    def _parse_local_grep_match(line: str, *, workdir: str) -> Optional[Dict[str, Any]]:
        parts = str(line or "").rstrip("\n").split(":", 2)
        if len(parts) < 3:
            return None
        file_path = parts[0]
        try:
            rel = os.path.relpath(file_path, workdir)
        except ValueError:
            rel = file_path
        try:
            line_no = int(parts[1])
        except ValueError:
            return None
        return {"file": rel, "line": line_no, "text": parts[2]}

    async def _run_local_files_search(
        self,
        *,
        pattern: str,
        search_path: str,
        workdir: str,
        case_sensitive: bool,
        max_results: int,
        timeout_sec: float = 30.0,
    ) -> tuple[List[Dict[str, Any]], bool]:
        case_flag = [] if case_sensitive else ["-i"]
        proc = await asyncio.create_subprocess_exec(
            "grep",
            "-rn",
            *case_flag,
            "--",
            pattern,
            search_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        matches: List[Dict[str, Any]] = []
        truncated = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_sec)
        try:
            while len(matches) < max_results:
                if proc.stdout is None:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                if not raw_line:
                    break
                parsed = self._parse_local_grep_match(
                    raw_line.decode("utf-8", errors="replace"),
                    workdir=workdir,
                )
                if parsed is not None:
                    matches.append(parsed)
            if len(matches) >= max_results and proc.returncode is None:
                truncated = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    logger.debug("session files local search process already exited before kill")
            remaining = max(0.1, deadline - loop.time())
            await asyncio.wait_for(proc.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    logger.debug("session files local search process already exited before timeout kill")
                await proc.wait()
            raise
        return matches, truncated

    async def search(
        self,
        user_id: int,
        session_uid: str,
        pattern: str,
        rel_path: str = ".",
        *,
        case_sensitive: bool = True,
        max_results: int = 200,
    ) -> Dict[str, Any]:
        pattern = str(pattern or "").strip()
        if not pattern:
            raise PathValidationError("pattern query parameter is required")
        max_results = max(1, min(int(max_results), 1000))

        session = self._session_by_uid(session_uid)
        ctx = self.execution_context(session_uid, user_id=int(user_id))

        rc_svc = getattr(self.app, "remote_control_service", None)
        ssh_svc = getattr(self.app, "ssh_service", None)

        if rc_svc is not None and ssh_svc is not None:
            from app.services.remote_control_service import ExecutionTarget
            from app.services.remote_shell_service import RemoteShellService
            from app.services.ssh_config_loader import load_ssh_config

            workdir = str(getattr(session, "workdir", "") or "").strip()
            hosts = load_ssh_config(workdir) if workdir else {}
            effective = rc_svc.compute_effective_state(session, hosts)

            if effective.execution_target == ExecutionTarget.REMOTE:
                self._ensure_remote_host_allowed(hosts, str(effective.host_alias or ""), int(user_id))
                root = str(effective.remote_project_root or "").strip()
                safe_remote_path = self._resolve_path(
                    int(user_id),
                    session_uid,
                    str(rel_path or "."),
                    root_override=root,
                )
                rel_search_path = posixpath.relpath(safe_remote_path, root)
                if rel_search_path == ".":
                    rel_search_path = "."
                shell_svc = RemoteShellService(ssh_svc)
                result = await shell_svc.search_code(
                    workdir,
                    effective.host_alias,
                    pattern,
                    path=rel_search_path,
                    cwd=root,
                    case_sensitive=case_sensitive,
                    max_results=max_results,
                )
                if result.error:
                    raise FilesSearchError(result.error)
                return {
                    "ok": True,
                    "matches": [
                        {"file": m.file, "line": m.line, "text": m.text}
                        for m in result.matches
                    ],
                    "truncated": result.truncated,
                    "execution_target": "remote",
                }
            if bool(ctx.get("remote_control_enabled")):
                raise FilesRemoteTargetUnavailableError(
                    "Remote Control is enabled but the remote target is unavailable"
                )

        workdir = str(getattr(session, "workdir", "") or "").strip()
        search_path = self._resolve_path(
            int(user_id),
            session_uid,
            str(rel_path or "."),
        )
        try:
            matches, truncated = await self._run_local_files_search(
                pattern=pattern,
                search_path=search_path,
                workdir=workdir,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
        except (FileNotFoundError, asyncio.TimeoutError) as exc:
            raise FilesSearchError(str(exc)) from exc

        return {
            "ok": True,
            "matches": matches,
            "truncated": truncated,
            "execution_target": "local",
        }


FilesService = SessionFilesService
