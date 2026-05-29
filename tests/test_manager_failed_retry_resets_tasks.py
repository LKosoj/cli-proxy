import asyncio
import types

from bot import BotApp
from tg.callbacks import CallbackHandler
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from modes.sdk.planning import load_plan, save_plan
from session import session_runtime_uid


class _FakeMessage:
    def __init__(self, chat_id: int = 1, message_id: int = 10) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


def _build_app(tmp_path) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            openai_api_key="k",
            openai_model="m",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def test_failed_retry_resets_failed_and_blocked_tasks(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "manager"

        plan = ProjectPlan(
            project_goal="goal",
            tasks=[
                DevTask(
                    id="t1",
                    title="Failed task",
                    description="",
                    acceptance_criteria=["ok"],
                    status="failed",
                    attempt=3,
                    max_attempts=3,
                    completed_at="2026-01-01 00:00:00",
                ),
                DevTask(
                    id="t2",
                    title="Blocked task",
                    description="",
                    acceptance_criteria=["ok"],
                    status="blocked",
                    attempt=1,
                    max_attempts=3,
                ),
                DevTask(
                    id="t3",
                    title="Approved task",
                    description="",
                    acceptance_criteria=["ok"],
                    status="approved",
                    attempt=1,
                    max_attempts=3,
                ),
            ],
            status="failed",
        )
        save_plan(session.workdir, plan)

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            return True

        app._edit_message = _edit_message

        created = []

        def _create_task(*, mode_id, coro, name, session_id=None, session_uid=None):
            created.append((session_uid if session_uid is not None else session_id, mode_id, name))
            try:
                coro.close()
            except Exception:
                pass

        app.mode_tasks.create = _create_task

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:failed_retry"))
        await handler.handle_callback(update, context=object())

        updated = load_plan(session.workdir)
        assert updated is not None
        by_id = {t.id: t for t in updated.tasks}

        assert by_id["t1"].status == "pending"
        assert by_id["t1"].attempt == 0
        assert by_id["t1"].completed_at is None

        assert by_id["t2"].status == "pending"
        assert by_id["t2"].attempt == 1

        assert by_id["t3"].status == "approved"
        assert by_id["t3"].attempt == 1

        assert created
        assert created[-1][0] == session_runtime_uid(session)
        assert created[-1][2] == "failed_retry"

    asyncio.run(_run())
