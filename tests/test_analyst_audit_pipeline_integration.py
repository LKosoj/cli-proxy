import asyncio
from pathlib import Path

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.analyst.state_store import AnalystStateStore, build_context_key
from modes.analyst.template_service import get_template_for_session
from utils import cli_proxy_artifact_path


class _FakeAnalystRuntime:
    capabilities = {"run_analyst", "template_provider"}

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.clear_calls: list[str] = []

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities

    def clear_session_cache(self, session_id: str) -> None:
        self.clear_calls.append(str(session_id))

    async def run(self, session, analyst_prompt: str, _bot_app, _context, _dest):
        store = AnalystStateStore(cli_proxy_artifact_path(str(session.workdir), ".analyst_data"))
        context_key = build_context_key(getattr(session, "chat_id", None), getattr(session, "id", None))
        ctx = store.load(context_key)
        self.calls.append(
            {
                "prompt": str(analyst_prompt or ""),
                "ctx_mode": str(getattr(ctx, "mode", "") or ""),
                "ctx_active_flow": str(getattr(ctx, "active_flow", "") or ""),
                "ctx_runtime_template_id": str(getattr(ctx, "runtime_template_id", "") or ""),
                "session_runtime_template_id": str(getattr(session, "analyst_runtime_template_id", "") or ""),
            }
        )
        return "ok"

    def get_template_for_session(self, session):
        return get_template_for_session(session)


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


def _write_templates(path: Path) -> None:
    path.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["DEFAULT_MARK"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  audit:
    name: "Audit"
    description: "D"
    required_sections: ["AUDIT_MARK"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
""",
        encoding="utf-8",
    )


async def _wait_mode_tasks_done(app: BotApp, *, session_id: str, mode_id: str, timeout_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout_s)
    while loop.time() < deadline:
        if not app.mode_tasks.list(session_id=str(session_id), mode_id=str(mode_id)):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"mode tasks did not finish in time session_id={session_id} mode_id={mode_id}")
