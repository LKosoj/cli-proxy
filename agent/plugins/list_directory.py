from __future__ import annotations

from datetime import datetime
import grp
import logging
import os
from pathlib import Path
import pwd
import stat
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling import helpers

logger = logging.getLogger(__name__)
_SIX_MONTHS_SECONDS = 60 * 60 * 24 * 30 * 6


def _format_timestamp(timestamp: float) -> str:
    current = datetime.now().timestamp()
    dt = datetime.fromtimestamp(timestamp)
    if abs(current - float(timestamp)) >= _SIX_MONTHS_SECONDS:
        return dt.strftime("%b %e  %Y")
    return dt.strftime("%b %e %H:%M")


def _format_blocks(stat_result: os.stat_result) -> int:
    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is None:
        if stat.S_ISDIR(stat_result.st_mode):
            return 0
        size = max(0, int(stat_result.st_size))
        return max(1, (size + 1023) // 1024) if size else 0
    return max(0, int(blocks) // 2)


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(int(gid)).gr_name
    except KeyError:
        return str(gid)


def _format_entry_line(display_name: str, stat_result: os.stat_result) -> str:
    return (
        f"{stat.filemode(stat_result.st_mode)} "
        f"{int(stat_result.st_nlink):>2} "
        f"{_owner_name(int(stat_result.st_uid))} "
        f"{_group_name(int(stat_result.st_gid))} "
        f"{int(stat_result.st_size):>8} "
        f"{_format_timestamp(float(stat_result.st_mtime))} "
        f"{display_name}"
    )


def _symlink_display_name(path: Path, base_name: str) -> str:
    if not path.is_symlink():
        return base_name
    try:
        return f"{base_name} -> {os.readlink(path)}"
    except OSError:
        return base_name


def _list_file(path: Path) -> str:
    stat_result = path.lstat()
    return _format_entry_line(str(path), stat_result) + "\n"


def _list_directory(path: Path) -> str:
    current_stat = path.lstat()
    parent_path = path.parent if path.parent != path else path
    parent_stat = parent_path.lstat()
    total_blocks = _format_blocks(current_stat) + _format_blocks(parent_stat)
    child_lines = []

    with os.scandir(path) as entries:
        for entry in entries:
            entry_path = Path(entry.path)
            entry_stat = entry.stat(follow_symlinks=False)
            total_blocks += _format_blocks(entry_stat)
            child_lines.append(
                (
                    entry.name,
                    _format_entry_line(_symlink_display_name(entry_path, entry.name), entry_stat),
                )
            )

    lines = [f"total {total_blocks}"]
    lines.append(_format_entry_line(".", current_stat))
    lines.append(_format_entry_line("..", parent_stat))
    lines.extend(line for _, line in sorted(child_lines, key=lambda item: item[0]))
    return "\n".join(lines) + "\n"


class ListDirectoryTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_directory",
            description="List contents of a directory.",
            parameters={"type": "object", "properties": {"path": {"type": "string", "description": "Directory path (default: current)"}}},
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        cwd = ctx["cwd"]
        path = args.get("path")
        dir_path = path if path else cwd
        if not os.path.isabs(dir_path):
            dir_path = os.path.join(cwd, dir_path)
        resolved_path, err = helpers._resolve_within_workspace(dir_path, cwd)
        if err:
            blocked = helpers.blocked_from_error(err)
            if blocked:
                return blocked
            return {"success": False, "error": err}
        dir_path = resolved_path
        if helpers._is_other_user_workspace(dir_path, cwd):
            reason = "Cannot access other user's workspace"
            return helpers.blocked_error(reason)
        blocked_dirs = ["/etc", "/root", "/.ssh", "/proc", "/sys", "/dev", "/boot", "/var/log", "/var/run"]
        resolved = os.path.realpath(dir_path).lower()
        for b in blocked_dirs:
            if resolved == b or resolved.startswith(b + "/"):
                reason = f"Cannot list directory {b} for security reasons"
                return helpers.blocked_error(reason)
        if "/.ssh" in resolved:
            reason = "Cannot list .ssh directory"
            return helpers.blocked_error(reason)
        try:
            target_path = Path(dir_path)
            output = _list_directory(target_path) if target_path.is_dir() else _list_file(target_path)
            return {"success": True, "output": output}
        except Exception as e:
            logger.exception("list_directory: failed to list path=%r", dir_path)
            return {"success": False, "error": str(e)}
