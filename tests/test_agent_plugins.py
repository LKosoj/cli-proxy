import asyncio
from pathlib import Path
import uuid

from agent.plugins.ask_user import AskUserTool
from agent.plugins.list_directory import ListDirectoryTool
from agent.plugins.search_text import SearchTextTool
from app.services import ConfigService, SessionService, TaskService
from app.services.config_service import ConfigProvider
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager, session_runtime_uid


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
        current = self.config
        for part in str(key or "").split("."):
            token = part.strip()
            if not token:
                continue
            if isinstance(current, dict):
                if token not in current:
                    return default
                current = current[token]
                continue
            if not hasattr(current, token):
                return default
            current = getattr(current, token)
        return current


def _execute_list_directory(path: str | None, cwd: Path) -> dict:
    tool = ListDirectoryTool()
    args = {"path": path} if path is not None else {}
    return asyncio.run(tool.execute(args, {"cwd": str(cwd)}))


def _execute_search_text(args: dict, cwd: Path) -> dict:
    tool = SearchTextTool()
    return asyncio.run(tool.execute(args, {"cwd": str(cwd)}))


def test_list_directory_shell_injection_payloads_are_treated_as_literal_paths(tmp_path) -> None:
    marker_dollar = Path("/tmp") / f"cli_proxy_list_directory_dollar_{uuid.uuid4().hex}"
    marker_backtick = Path("/tmp") / f"cli_proxy_list_directory_backtick_{uuid.uuid4().hex}"

    marker_dollar.unlink(missing_ok=True)
    marker_backtick.unlink(missing_ok=True)

    dollar_result = _execute_list_directory(f"$(touch {marker_dollar})", tmp_path)
    backtick_result = _execute_list_directory(f"`touch {marker_backtick}`", tmp_path)

    assert marker_dollar.exists() is False
    assert marker_backtick.exists() is False
    assert dollar_result["success"] is False
    assert backtick_result["success"] is False


def test_list_directory_supports_literal_paths_with_spaces_and_special_characters(tmp_path) -> None:
    special_dir = tmp_path / "literal $(dir) `ticks` [sample]"
    special_dir.mkdir()
    (special_dir / "alpha.txt").write_text("alpha", encoding="utf-8")
    (special_dir / "beta.txt").write_text("beta", encoding="utf-8")

    result = _execute_list_directory("literal $(dir) `ticks` [sample]", tmp_path)

    assert result["success"] is True
    assert "alpha.txt" in result["output"]
    assert "beta.txt" in result["output"]


def test_list_directory_keeps_normal_directory_listing_sorted(tmp_path) -> None:
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "b.txt").write_text("b", encoding="utf-8")
    (sample_dir / "a.txt").write_text("a", encoding="utf-8")

    result = _execute_list_directory("sample", tmp_path)

    assert result["success"] is True
    lines = result["output"].splitlines()
    assert lines[0].startswith("total ")
    a_index = next(index for index, line in enumerate(lines) if line.endswith("a.txt"))
    b_index = next(index for index, line in enumerate(lines) if line.endswith("b.txt"))
    assert a_index < b_index


def test_search_text_shell_injection_payloads_are_treated_as_literals(tmp_path) -> None:
    marker_pattern = Path("/tmp") / f"cli_proxy_search_text_pattern_{uuid.uuid4().hex}"
    marker_path = Path("/tmp") / f"cli_proxy_search_text_path_{uuid.uuid4().hex}"

    marker_pattern.unlink(missing_ok=True)
    marker_path.unlink(missing_ok=True)

    pattern_result = _execute_search_text({"pattern": f"$(touch {marker_pattern})"}, tmp_path)
    path_result = _execute_search_text(
        {"pattern": "needle", "path": f"`touch {marker_path}`"},
        tmp_path,
    )

    assert marker_pattern.exists() is False
    assert marker_path.exists() is False
    assert pattern_result["success"] is True
    assert pattern_result["output"] == "(no matches)"
    # H9: поиск по несуществующему пути больше не маскируется под "(no matches)" —
    # возвращается ошибка (success=False). Ключевое: shell-инъекция не сработала
    # (маркер выше не создан), путь обработан как литерал, а не выполнен в shell.
    assert path_result["success"] is False
    assert "error" in path_result


def test_search_text_keeps_normal_search_behavior_and_output_limit(tmp_path) -> None:
    special_dir = tmp_path / "literal $(dir) `ticks` [sample]"
    special_dir.mkdir()
    search_file = special_dir / "matches.txt"
    search_file.write_text(
        "\n".join(f"{index:03d}: needle" for index in range(250)),
        encoding="utf-8",
    )

    result = _execute_search_text(
        {"pattern": "needle", "path": "literal $(dir) `ticks` [sample]"},
        tmp_path,
    )

    assert result["success"] is True
    lines = result["output"].splitlines()
    assert len(lines) == 200
    assert lines[0].endswith("001: needle") is False
    assert lines[0].endswith("000: needle")
    assert lines[-1].endswith("199: needle")


