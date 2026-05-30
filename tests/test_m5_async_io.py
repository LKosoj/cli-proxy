"""Tests for M5 fix: blocking I/O moved to asyncio.to_thread."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml


# ---------------------------------------------------------------------------
# config_service.save_atomic — atomicity via to_thread
# ---------------------------------------------------------------------------

def _make_minimal_config_yaml(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "telegram": {"token": "t", "whitelist_chat_ids": [1]},
                "tools": {},
                "defaults": {"workdir": str(path.parent)},
                "mcp": {"enabled": False},
                "mcp_clients": [],
                "presets": [],
                "miniapp": {"enabled": False},
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )


def test_config_service_save_atomic_uses_to_thread(tmp_path):
    """save_atomic must delegate file-write to asyncio.to_thread when content changes."""
    from app.services.config_service import ConfigService, FileConfigProvider

    cfg_path = tmp_path / "config.yaml"
    _make_minimal_config_yaml(cfg_path)

    svc = ConfigService(FileConfigProvider(str(cfg_path)))
    config = asyncio.run(svc.load())

    # Force a detectable change so save_atomic actually writes.
    # AppConfig is a dataclass but DefaultsConfig may be frozen; use direct dict mutation on
    # a reconstructed config to avoid frozen-dataclass restrictions.
    new_workdir = str(tmp_path / "changed_workdir")
    config = config.__class__(
        **{
            **config.__dict__,
            "defaults": config.defaults.__class__(
                **{**config.defaults.__dict__, "workdir": new_workdir}
            ),
        }
    )

    calls: list[Any] = []
    original_to_thread = asyncio.to_thread

    async def _tracked_to_thread(fn, *args, **kwargs):
        calls.append(fn.__name__ if callable(fn) and hasattr(fn, "__name__") else repr(fn))
        return await original_to_thread(fn, *args, **kwargs)

    with patch("app.services.config_service.asyncio.to_thread", side_effect=_tracked_to_thread):
        result = asyncio.run(svc.save_atomic(config, create_backup=False))

    assert result.path == str(cfg_path)
    assert "_write_config_atomic" in calls


def test_config_service_save_atomic_writes_file_atomically(tmp_path):
    """save_atomic must write content to disk and preserve old content on error."""
    from app.services.config_service import ConfigService, FileConfigProvider

    cfg_path = tmp_path / "config.yaml"
    _make_minimal_config_yaml(cfg_path)
    original = cfg_path.read_text(encoding="utf-8")

    svc = ConfigService(FileConfigProvider(str(cfg_path)))
    config = asyncio.run(svc.load())

    # Mutate workdir to force a change
    config = config.__class__(
        **{
            **config.__dict__,
            "defaults": config.defaults.__class__(
                **{**config.defaults.__dict__, "workdir": str(tmp_path / "new_workdir")}
            ),
        }
    )

    result = asyncio.run(svc.save_atomic(config, create_backup=True))

    assert result.changed is True
    assert cfg_path.exists()
    saved = cfg_path.read_text(encoding="utf-8")
    assert "new_workdir" in saved
    # Backup must contain original content
    assert result.backup_path is not None
    assert Path(result.backup_path).read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# routes_logs._read_available_lines — called via to_thread
# ---------------------------------------------------------------------------

def test_read_available_lines_opens_and_reads(tmp_path):
    """_read_available_lines should open the file and return lines."""
    from miniapp.routes_logs import _read_available_lines

    log_file = tmp_path / "test.log"
    log_file.write_text("line1\nline2\n", encoding="utf-8")

    stream, inode, start_pos, lines = _read_available_lines(str(log_file), None, None, 0)

    assert lines == ["line1\n", "line2\n"]
    assert stream is not None
    assert inode is not None
    stream.close()


def test_read_available_lines_incremental(tmp_path):
    """Second call with existing stream should read only new lines."""
    from miniapp.routes_logs import _read_available_lines

    log_file = tmp_path / "test.log"
    log_file.write_text("line1\n", encoding="utf-8")

    stream, inode, pos, lines = _read_available_lines(str(log_file), None, None, 0)
    assert lines == ["line1\n"]

    # Append a new line
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("line2\n")

    stream, inode, pos, lines2 = _read_available_lines(str(log_file), stream, inode, pos)
    assert lines2 == ["line2\n"]
    stream.close()


def test_read_available_lines_nonexistent_file(tmp_path):
    """When file does not exist, stream stays None and lines is empty."""
    from miniapp.routes_logs import _read_available_lines

    stream, inode, pos, lines = _read_available_lines(
        str(tmp_path / "missing.log"), None, None, 0
    )
    assert stream is None
    assert lines == []


def test_stream_log_updates_calls_read_available_lines_via_to_thread(tmp_path):
    """_stream_log_updates must call _read_available_lines inside asyncio.to_thread."""
    import miniapp.routes_logs as routes_logs_mod

    calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def _spy_to_thread(fn, *args, **kwargs):
        if callable(fn) and getattr(fn, "__name__", "") == "_read_available_lines":
            calls.append("_read_available_lines")
        return await original_to_thread(fn, *args, **kwargs)

    log_file = tmp_path / "test.log"
    log_file.write_text("", encoding="utf-8")

    # Minimal stub for LogsService
    logs_svc = MagicMock()
    logs_svc.resolve_log_path.return_value = str(log_file)
    logs_svc.file_end_position.return_value = 0
    logs_svc.allowed_session_uids.return_value = set()
    logs_svc.allowed_session_pairs.return_value = set()
    logs_svc.entry_allowed.return_value = False

    # Minimal WebSocket stub that closes after first iteration
    iteration_count = 0

    class _FakeWs:
        @property
        def closed(self):
            nonlocal iteration_count
            iteration_count += 1
            return iteration_count > 1

        async def send_json(self, data):
            pass

    ctx = MagicMock()
    ctx.logger = MagicMock()

    services = MagicMock()
    services.logs = logs_svc
    user = {"user_id": 1, "is_admin": False}

    async def _run():
        with patch.object(routes_logs_mod.asyncio, "to_thread", side_effect=_spy_to_thread):
            await routes_logs_mod._stream_log_updates(
                ctx,
                services,
                _FakeWs(),
                user=user,
                log_type="main",
                session_uid_filter=None,
                session_id_filter=None,
            )

    asyncio.run(_run())

    assert "_read_available_lines" in calls


# ---------------------------------------------------------------------------
# QwenJsonlMonitor._poll_loop — uses to_thread
# ---------------------------------------------------------------------------

def test_qwen_poll_loop_uses_to_thread():
    """QwenJsonlMonitor._poll_loop must call asyncio.to_thread(_poll_sync)."""
    from app.services.qwen_jsonl_monitor import QwenJsonlMonitor

    calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def _spy_to_thread(fn, *args, **kwargs):
        if callable(fn) and getattr(fn, "__name__", "") == "_poll_sync":
            calls.append("_poll_sync")
        return await original_to_thread(fn, *args, **kwargs)

    async def _run():
        monitor = QwenJsonlMonitor(workdir="/tmp", poll_interval=0.01)
        with patch("app.services.qwen_jsonl_monitor.asyncio.to_thread", side_effect=_spy_to_thread):
            monitor.start()
            await asyncio.sleep(0.05)
            monitor.stop()
            await asyncio.sleep(0.02)

    asyncio.run(_run())

    assert "_poll_sync" in calls
