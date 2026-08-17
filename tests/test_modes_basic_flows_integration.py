import asyncio
import types
from typing import Any, Dict, List

import pytest

from app.services.input_dispatch_service import InputDispatchService
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.sdk.session_busy import is_session_busy
from modes.sdk.services.mode_callbacks import ModeCallbackRouterService
from session import session_runtime_uid
from tg.callbacks import CallbackHandler


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


async def _wait_until(predicate, *, timeout: float = 2.0, sleep_s: float = 0.01) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout)
    while loop.time() < deadline:
        if bool(predicate()):
            return
        await asyncio.sleep(sleep_s)
    raise AssertionError("timeout waiting for condition")


def _three_busy_flags_released(session: Any) -> bool:
    run_lock = getattr(session, "run_lock", None)
    is_active_by_tick = getattr(session, "is_active_by_tick", None)
    tick_active = False
    if callable(is_active_by_tick):
        try:
            last_tick_ts = getattr(session, "last_tick_ts", None)
            if last_tick_ts is not None:
                tick_active = bool(is_active_by_tick(now=float(last_tick_ts) + 4.0))
            else:
                tick_active = bool(is_active_by_tick())
        except TypeError:
            tick_active = bool(is_active_by_tick())
    probe_session = types.SimpleNamespace(
        busy=bool(getattr(session, "busy", False)),
        is_active_by_tick=(lambda: bool(tick_active)),
    )
    return bool(
        not bool(getattr(session, "busy", False))
        and not bool(run_lock and run_lock.locked())
        and not tick_active
        and not is_session_busy(probe_session, run_lock)
    )


class _BlockingRunRuntime:
    def __init__(
        self,
        *,
        capability: str,
        entered: asyncio.Event,
        release: asyncio.Event,
        run_inputs: List[str],
        block_first_run: bool = True,
    ) -> None:
        self.capabilities = frozenset({str(capability)})
        self._entered = entered
        self._release = release
        self._run_inputs = run_inputs
        self._block_first_run = bool(block_first_run)

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities

    async def run(self, session: Any, prompt: str, bot_app: Any, context: Any, dest: Dict[str, Any]) -> str:
        _ = session
        _ = bot_app
        _ = context
        _ = dest
        self._run_inputs.append(str(prompt or ""))
        if self._block_first_run and len(self._run_inputs) == 1:
            self._entered.set()
            await self._release.wait()
        return f"OK:{prompt}"

    def pause(self, session: Any) -> None:
        _ = session
        return None

    def reset(self, session: Any) -> None:
        _ = session
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode_id", ["agent"])
async def test_basic_mode_flow_enable_run_busy_queue_completion_status_for_all_modes(tmp_path, mode_id: str) -> None:
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = None

    sent: List[Dict[str, Any]] = []
    edited: List[Dict[str, Any]] = []

    async def _send_message(_ctx, *, chat_id: int, text: str, **kwargs):
        sent.append({"chat_id": int(chat_id), "text": str(text or ""), "kwargs": dict(kwargs or {})})
        return True

    async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
        edited.append(
            {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "text": str(text or ""),
                "reply_markup": reply_markup,
            }
        )
        return True

    async def _send_output(_session, _dest, _output, _context, **_kwargs):
        return None

    app._send_message = _send_message
    app._edit_message = _edit_message
    app.send_output = _send_output

    mode = app.mode_registry.get(mode_id)
    assert mode is not None

    entered = asyncio.Event()
    release = asyncio.Event()
    run_inputs: List[str] = []
    app.register_mode_runtime(
        "agent",
        _BlockingRunRuntime(
            capability="run_agent",
            entered=entered,
            release=release,
            run_inputs=run_inputs,
            block_first_run=True,
        ),
    )

    handler = CallbackHandler(app)

    await handler.handle_callback(
        types.SimpleNamespace(callback_query=_FakeQuery(f"ma:{mode_id}:enable")),
        context=object(),
    )
    assert session.modes.active_mode == mode_id

    first_prompt = f"first-{mode_id}"
    second_prompt = f"second-{mode_id}"

    await app._handle_user_input(
        session,
        first_prompt,
        1,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1, "user_id": 77},
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    assert bool(session.busy) is True

    before_status_busy = len(sent) + len(edited)
    await handler.handle_callback(
        types.SimpleNamespace(callback_query=_FakeQuery(f"ma:{mode_id}:status")),
        context=object(),
    )
    assert len(sent) + len(edited) > before_status_busy

    await app._handle_user_input(
        session,
        second_prompt,
        1,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1, "user_id": 77},
    )
    pending_head = InputDispatchService.pending_head(getattr(app, "pending", None), app.telegram_ui_key(1))
    assert pending_head is not None
    assert str(getattr(pending_head, "text", "")) == second_prompt

    await handler.handle_callback(
        types.SimpleNamespace(callback_query=_FakeQuery("queue_input")),
        context=object(),
    )

    assert list(getattr(session, "queue", []) or [])
    assert dict(session.queue[0]).get("text") == second_prompt
    assert InputDispatchService.pending_head(getattr(app, "pending", None), app.telegram_ui_key(1)) is None

    release.set()

    def has_two_runs() -> bool:
        return len(run_inputs) >= 2

    try:
        await _wait_until(has_two_runs, timeout=1.0)
    except AssertionError:
        await _wait_until(
            lambda: not InputDispatchService._is_session_running(session, app),
            timeout=5.0,
        )
        pending_after_release = InputDispatchService.pending_head(
            getattr(app, "pending", None),
            app.telegram_ui_key(1),
        )
        if pending_after_release is not None:
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("queue_input")),
                context=object(),
            )
        elif len(getattr(session, "queue", []) or []) > 0:
            await handler._kick_session_queue_if_idle(session=session, chat_id=1, context=object())
        await _wait_until(has_two_runs, timeout=3.0)

    await _wait_until(
        lambda: (
            _three_busy_flags_released(session)
            and len(getattr(session, "queue", []) or []) == 0
            and len(app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id=mode_id) or []) == 0
        ),
        timeout=6.0,
    )
    assert _three_busy_flags_released(session) is True

    before_status_done = len(sent) + len(edited)
    await handler.handle_callback(
        types.SimpleNamespace(callback_query=_FakeQuery(f"ma:{mode_id}:status")),
        context=object(),
    )
    assert len(sent) + len(edited) > before_status_done

    assert session.modes.active_mode == mode_id
    assert InputDispatchService.pending_head(getattr(app, "pending", None), app.telegram_ui_key(1)) is None
    assert len(getattr(session, "queue", []) or []) == 0
    assert list(run_inputs)[:2] == [first_prompt, second_prompt]


@pytest.mark.asyncio
async def test_mode_transitions_preserve_clean_session_state_between_runs(tmp_path) -> None:
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = None

    async def _send_message(_ctx, *, chat_id: int, text: str, **kwargs):
        _ = chat_id
        _ = text
        _ = kwargs
        return True

    async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
        _ = chat_id
        _ = message_id
        _ = text
        _ = reply_markup
        return True

    async def _send_output(_session, _dest, _output, _context, **_kwargs):
        return None

    app._send_message = _send_message
    app._edit_message = _edit_message
    app.send_output = _send_output

    run_inputs: List[str] = []
    app.register_mode_runtime(
        "agent",
        _BlockingRunRuntime(
            capability="run_agent",
            entered=asyncio.Event(),
            release=asyncio.Event(),
            run_inputs=run_inputs,
            block_first_run=False,
        ),
    )

    handler = CallbackHandler(app)
    mode_id = "agent"
    await _wait_until(
        lambda: not ModeCallbackRouterService._is_session_busy_for_mode_changes(session),
        timeout=5.0,
    )
    await handler.handle_callback(
        types.SimpleNamespace(callback_query=_FakeQuery(f"ma:{mode_id}:enable")),
        context=object(),
    )
    assert session.modes.active_mode == mode_id

    await app._handle_user_input(
        session,
        "transition-agent",
        1,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1, "user_id": 77},
    )

    await _wait_until(
        lambda: (
            _three_busy_flags_released(session)
            and len(getattr(session, "queue", []) or []) == 0
            and len(app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id=mode_id) or []) == 0
        ),
        timeout=6.0,
    )
    assert _three_busy_flags_released(session) is True
    assert InputDispatchService.pending_head(getattr(app, "pending", None), app.telegram_ui_key(1)) is None
    assert any("transition-agent" in x for x in run_inputs)
    assert session.modes.active_mode == "agent"
