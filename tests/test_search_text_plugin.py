from __future__ import annotations

import subprocess
from typing import Any, Dict
from unittest.mock import patch

import pytest

from agent.plugins import search_text as search_text_module
from agent.plugins.search_text import SearchTextTool


def _ctx(tmp_path) -> Dict[str, Any]:
    return {"cwd": str(tmp_path)}


def _args(pattern: str = "hello", path: str | None = None) -> Dict[str, Any]:
    a: Dict[str, Any] = {"pattern": pattern}
    if path is not None:
        a["path"] = path
    return a


# ---------------------------------------------------------------------------
# Хелпер: мок subprocess.run, возвращающий CompletedProcess с заданными полями
# ---------------------------------------------------------------------------

def _make_proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.asyncio
async def test_subprocess_run_called_via_thread(tmp_path):
    """subprocess.run должен вызываться через asyncio.to_thread, а не напрямую в event loop."""
    proc = _make_proc(0, stdout="match line\n")

    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return proc

    tool = SearchTextTool()
    with patch("agent.plugins.search_text.asyncio.to_thread", side_effect=fake_to_thread):
        result = await tool.execute(_args("hello"), _ctx(tmp_path))

    assert calls, "asyncio.to_thread должен был быть вызван"
    assert calls[0][0] is subprocess.run
    assert result["success"] is True


@pytest.mark.asyncio
async def test_timeout_expired_returns_failure(tmp_path):
    """TimeoutExpired → success: False с сообщением о таймауте."""
    tool = SearchTextTool()

    async def fake_to_thread(func, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["grep"], timeout=10)

    with patch("agent.plugins.search_text.asyncio.to_thread", side_effect=fake_to_thread):
        result = await tool.execute(_args("hello"), _ctx(tmp_path))

    assert result["success"] is False
    assert "timed out" in result["error"].lower()


@pytest.mark.asyncio
async def test_returncode_ge_2_returns_failure(tmp_path):
    """returncode >= 2 → success: False."""
    proc = _make_proc(2, stdout="", stderr="something went wrong")
    tool = SearchTextTool()

    async def fake_to_thread(func, *args, **kwargs):
        return proc

    with patch("agent.plugins.search_text.asyncio.to_thread", side_effect=fake_to_thread):
        result = await tool.execute(_args("hello"), _ctx(tmp_path))

    assert result["success"] is False
    assert "exit 2" in result["error"]


@pytest.mark.asyncio
async def test_returncode_1_no_matches(tmp_path):
    """returncode == 1 (grep/rg: нет совпадений) → success: True + '(no matches)'."""
    proc = _make_proc(1, stdout="", stderr="")
    tool = SearchTextTool()

    async def fake_to_thread(func, *args, **kwargs):
        return proc

    with patch("agent.plugins.search_text.asyncio.to_thread", side_effect=fake_to_thread):
        result = await tool.execute(_args("hello"), _ctx(tmp_path))

    assert result["success"] is True
    assert result["output"] == "(no matches)"


@pytest.mark.asyncio
async def test_file_not_found_returns_failure(tmp_path):
    """FileNotFoundError от _build_search_command → success: False."""
    tool = SearchTextTool()

    # Патчим через объект модуля (а не строковый таргет): плагин-лоадер
    # modes/sdk/runtime/tooling/loader.py:48,54 грузит плагин через
    # spec_from_file_location("agent.plugins.search_text", ...) и делает
    # sys.modules[spec.name] = module, т.е. подменяет sys.modules["agent.plugins.search_text"]
    # свежей копией. После этого строковый patch попал бы в новый модуль, тогда как
    # execute (из импортированного здесь SearchTextTool) использует globals старого.
    # Объект search_text_module импортирован вместе с SearchTextTool и совпадает
    # с execute.__globals__, поэтому patch.object надёжно перехватывает символ.
    with patch.object(search_text_module, "_build_search_command", side_effect=FileNotFoundError("grep not available")):
        result = await tool.execute(_args("hello"), _ctx(tmp_path))

    assert result["success"] is False
    assert "Search tool not found" in result["error"]
