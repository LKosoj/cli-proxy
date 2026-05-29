import asyncio
from types import SimpleNamespace

from app.services.lifecycle_service import build_post_shutdown
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig


def _make_config(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
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
            summary_max_chars=200,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_botapp_html_pool_is_wired_and_shutdown_is_idempotent(tmp_path):
    app = BotApp(_make_config(tmp_path))
    pool = app._html_process_pool
    assert pool is not None
    assert app.session_management._output_service._html_process_pool is pool

    app.shutdown_html_process_pool()
    assert app._html_process_pool is None
    assert app.session_management._output_service._html_process_pool is None

    # Repeated shutdown should be a no-op.
    app.shutdown_html_process_pool()
    assert app._html_process_pool is None


def test_post_shutdown_calls_html_pool_shutdown():
    async def _run():
        mcp_calls = []
        miniapp_calls = []
        html_shutdown_calls = []
        runtime_shutdown_calls = []

        class _DummyTask:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        task = _DummyTask()
        bot_app = SimpleNamespace(
            _task_deadline_checker_task=task,
            shutdown_runtime=lambda: _mark_async(runtime_shutdown_calls),
            mcp=SimpleNamespace(stop=lambda: _mark_async(mcp_calls)),
            miniapp_server=SimpleNamespace(stop=lambda: _mark_async(miniapp_calls)),
            shutdown_html_process_pool=lambda: html_shutdown_calls.append("called"),
        )

        post_shutdown = build_post_shutdown(bot_app)
        await post_shutdown(application=None)

        assert task.cancelled is True
        assert bot_app._task_deadline_checker_task is None
        assert len(runtime_shutdown_calls) == 1
        assert len(mcp_calls) == 1
        assert len(miniapp_calls) == 1
        assert len(html_shutdown_calls) == 1

    async def _mark_async(store):
        store.append("called")

    asyncio.run(_run())
