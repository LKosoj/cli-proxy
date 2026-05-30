import asyncio
from pathlib import Path
import uuid

from agent.plugins.ask_user import AskUserTool
from agent.plugins.list_directory import ListDirectoryTool
from agent.plugins.search_text import SearchTextTool
from app.services import ConfigService, SessionService, TaskService
from app.services.config_service import ConfigProvider
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
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


def _build_desktop_config(tmp_path: Path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[], admlist_chat_ids=[]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(enabled=False),
    )


def _build_desktop_ask_runtime(tmp_path: Path, *, intent: str) -> dict[str, object]:
    cfg = _build_desktop_config(tmp_path, intent=intent)
    task_service = TaskService()
    session_service = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    workdir = Path(cfg.defaults.workdir) / "desktop-ask"
    workdir.mkdir(parents=True, exist_ok=True)
    session = session_service.create_desktop_session("dummy", str(workdir))
    session.name = f"Desktop {intent}"
    return {
        "facade": facade,
        "session": session,
    }


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


def test_ask_user_desktop_adapter_integration(tmp_path) -> None:
    runtime = _build_desktop_ask_runtime(tmp_path, intent="agent_plugin_desktop_ask")

    async def _run() -> None:
        facade = runtime["facade"]
        session = runtime["session"]
        session_uid = session_runtime_uid(session)
        notifications = []
        tool = AskUserTool()
        tool.initialize(services={})

        unsubscribe = facade.subscribe(notifications.append)
        await facade.start(validate_secrets=False)
        try:
            bot_app = facade._desktop_bot_app()
            task = asyncio.create_task(
                tool.execute(
                    {
                        "question": "Выбрать действие?",
                        "options": ["Да", "Нет"],
                        "allow_custom": False,
                        "system_options": False,
                    },
                    {
                        "bot": bot_app,
                        "context": object(),
                        "chat_id": session_uid,
                        "session_id": "desktop-session-id",
                    },
                )
            )

            question_id = ""
            for _ in range(100):
                if bot_app.ui_state.pending_questions:
                    question_id = next(iter(bot_app.ui_state.pending_questions))
                    break
                await asyncio.sleep(0)

            assert question_id
            pending = bot_app.ui_state.pending_questions[question_id]
            assert pending["question_id"] == question_id
            assert pending["question"] == "Выбрать действие?"
            assert pending["options"] == ["Да", "Нет"]
            assert pending["allow_custom"] is False
            assert pending["chat_id"] == session_uid
            assert pending["session_uid"] == session_uid
            assert pending["session_id"] == "desktop-session-id"

            assert notifications[-1].event == "ui:ask_question"
            assert notifications[-1].payload["session_uid"] == session_uid
            assert notifications[-1].payload["session_id"] == "desktop-session-id"
            assert notifications[-1].payload["question_id"] == question_id
            assert notifications[-1].payload["question"] == "Выбрать действие?"
            assert notifications[-1].payload["options"] == ["Да", "Нет"]
            assert notifications[-1].payload["allow_custom"] is False

            pending_futures = tool.services["pending_questions"]
            pending_futures[question_id].set_result("Да")
            result = await task

            assert result == {"success": True, "output": "User selected: Да"}
            assert question_id not in pending_futures
            assert question_id not in bot_app.ui_state.pending_questions
        finally:
            unsubscribe()
            await facade.shutdown()

    asyncio.run(_run())
