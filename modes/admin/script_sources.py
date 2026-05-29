"""
Sources registry for "build runbook from scripts" flow.

Оператор задаёт каталог со скриптами (напр. /srv/ops/fixes/), мы:
  1. проверяем что путь лежит под whitelisted корнем из admin.runbook_sources;
  2. резолвим симлинки → проверяем ещё раз;
  3. возвращаем плоский список .sh/.bash файлов с базовой информацией.

Цель — жёстко ограничить, куда билдер может заглянуть, чтобы случайно (или
преднамеренно) не начать тащить в runbook содержимое /etc/shadow.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence

_log = logging.getLogger(__name__)

SCRIPT_EXTENSIONS = (".sh", ".bash")
DEFAULT_MAX_FILES = 50
DEFAULT_MAX_FILE_SIZE = 256 * 1024  # 256 KiB per script


class ScriptSourceError(RuntimeError):
    """Raised when a path is rejected or scanning fails."""


@dataclass
class ScriptFile:
    path: Path
    name: str
    size_bytes: int
    sha1: str

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "size_bytes": int(self.size_bytes),
            "sha1": self.sha1,
        }


def load_whitelist(admin_cfg: Optional[Mapping[str, Any]]) -> List[Path]:
    """
    Читает admin.runbook_sources из config. Возвращает список абсолютных
    резолвленных путей к разрешённым корневым каталогам.
    """
    if not isinstance(admin_cfg, Mapping):
        return []
    raw = admin_cfg.get("runbook_sources")
    if raw is None:
        return []
    if isinstance(raw, str):
        items: Sequence[Any] = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: List[Path] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            resolved = Path(text).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            _log.warning("runbook_sources: skip invalid path %r", text)
            continue
        out.append(resolved)
    return out


def _is_under(child: Path, parent: Path) -> bool:
    """True если `child` эквивалентен `parent` или является его потомком."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_and_check(candidate: str, whitelist: Sequence[Path]) -> Path:
    raw = str(candidate or "").strip()
    if not raw:
        raise ScriptSourceError("path is empty")
    # strict=True: реальная проверка существования и резолв симлинков.
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise ScriptSourceError(f"path does not exist: {raw}")
    except (OSError, RuntimeError) as exc:
        raise ScriptSourceError(f"path cannot be resolved: {exc}")
    if not whitelist:
        raise ScriptSourceError(
            "admin.runbook_sources is empty — add at least one allowed directory",
        )
    if not any(_is_under(resolved, root) for root in whitelist):
        raise ScriptSourceError(
            f"path {resolved} is outside admin.runbook_sources whitelist",
        )
    return resolved


def _open_nofollow(path: Path) -> int:
    """Открывает файл на чтение с O_NOFOLLOW + O_CLOEXEC — чтобы TOCTOU-замена
    regular file на symlink между stat и open приводила к OSError, а не к
    чтению неожиданного файла.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(str(path), flags)


def _read_fd_all(fd: int, max_bytes: int) -> bytes:
    """Читает до max_bytes из fd. Бросает ScriptSourceError при превышении."""
    chunks: List[bytes] = []
    remaining = int(max_bytes)
    while remaining > 0:
        data = os.read(fd, min(65536, remaining))
        if not data:
            break
        chunks.append(data)
        remaining -= len(data)
    # если после max_bytes всё ещё есть данные — файл слишком большой
    probe = os.read(fd, 1)
    if probe:
        raise ScriptSourceError(f"file exceeds {max_bytes} bytes")
    return b"".join(chunks)


def _sha1_of_fd(fd: int) -> str:
    h = hashlib.sha1()
    os.lseek(fd, 0, 0)
    while True:
        data = os.read(fd, 65536)
        if not data:
            break
        h.update(data)
    return h.hexdigest()


def scan_scripts_directory(
    directory: str,
    *,
    whitelist: Sequence[Path],
    extensions: Iterable[str] = SCRIPT_EXTENSIONS,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> List[ScriptFile]:
    """
    Сканирует каталог (не рекурсивно) и возвращает список подходящих скриптов.

    Принципы:
      - путь должен существовать, резолв симлинков, проверка что всё ещё под whitelist
      - файл должен быть регулярным файлом (не symlink — чтобы scan не выходил за пределы)
      - расширение из whitelisted set
      - ограничение по размеру и количеству
    """
    root = _resolve_and_check(directory, whitelist)
    if not root.is_dir():
        raise ScriptSourceError(f"path is not a directory: {root}")

    ext_set = {str(e).lower() for e in extensions}
    out: List[ScriptFile] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.suffix.lower() not in ext_set:
            continue
        # O_NOFOLLOW ⇒ если файл стал symlink'ом между listdir и open — OSError.
        try:
            fd = _open_nofollow(entry)
        except OSError:
            _log.debug("scan: skip %s (symlink/nonregular)", entry)
            continue
        try:
            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            size = int(st.st_size)
            if size > int(max_file_size):
                _log.info("scan: skip %s (%d bytes > %d)", entry, size, max_file_size)
                continue
            try:
                sha1 = _sha1_of_fd(fd)
            except OSError as exc:
                _log.warning("scan: failed to hash %s: %s", entry, exc)
                continue
        finally:
            os.close(fd)
        out.append(ScriptFile(path=entry, name=entry.name, size_bytes=size, sha1=sha1))
        if len(out) >= int(max_files):
            break
    return out


def read_script_text(
    path: str,
    *,
    whitelist: Sequence[Path],
    max_bytes: int = DEFAULT_MAX_FILE_SIZE,
) -> str:
    """Безопасно читает скрипт (для превью/build). Путь обязан быть под whitelist.

    Резолв + open через O_NOFOLLOW — после резолва сам файл не должен быть симлинком.
    Чтение и проверка размера выполняются через один fd (без TOCTOU).
    """
    resolved = _resolve_and_check(path, whitelist)
    try:
        fd = _open_nofollow(resolved)
    except OSError as exc:
        raise ScriptSourceError(f"cannot open {resolved}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise ScriptSourceError(f"not a regular file: {resolved}")
        if int(st.st_size) > int(max_bytes):
            raise ScriptSourceError(
                f"file too large: {int(st.st_size)} bytes (limit {max_bytes})",
            )
        data = _read_fd_all(fd, int(max_bytes))
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace")


__all__ = [
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_SIZE",
    "SCRIPT_EXTENSIONS",
    "ScriptFile",
    "ScriptSourceError",
    "load_whitelist",
    "read_script_text",
    "scan_scripts_directory",
]
