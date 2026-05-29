from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODES_ROOT = REPO_ROOT / "modes"
ADMIN_ROOT = MODES_ROOT / "admin"
SDK_ROOT = MODES_ROOT / "sdk"
SDK_RUNTIME_ROOT = SDK_ROOT / "runtime"

_modes_pkg = types.ModuleType("modes")
_modes_pkg.__path__ = [str(MODES_ROOT)]
sys.modules.setdefault("modes", _modes_pkg)

_admin_pkg = types.ModuleType("modes.admin")
_admin_pkg.__path__ = [str(ADMIN_ROOT)]
sys.modules.setdefault("modes.admin", _admin_pkg)

_sdk_pkg = types.ModuleType("modes.sdk")
_sdk_pkg.__path__ = [str(SDK_ROOT)]
sys.modules.setdefault("modes.sdk", _sdk_pkg)

_sdk_runtime_pkg = types.ModuleType("modes.sdk.runtime")
_sdk_runtime_pkg.__path__ = [str(SDK_RUNTIME_ROOT)]
sys.modules.setdefault("modes.sdk.runtime", _sdk_runtime_pkg)

_STATE_STORE_PATH = ADMIN_ROOT / "state_store.py"
_STATE_STORE_SPEC = importlib.util.spec_from_file_location("modes.admin.state_store_test", _STATE_STORE_PATH)
if _STATE_STORE_SPEC is None or _STATE_STORE_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin state_store module from {_STATE_STORE_PATH}")
_STATE_STORE_MODULE = importlib.util.module_from_spec(_STATE_STORE_SPEC)
sys.modules[_STATE_STORE_SPEC.name] = _STATE_STORE_MODULE
_STATE_STORE_SPEC.loader.exec_module(_STATE_STORE_MODULE)
AdminStateStore = _STATE_STORE_MODULE.AdminStateStore

_NOTIFIER_PATH = ADMIN_ROOT / "notifier.py"
_NOTIFIER_SPEC = importlib.util.spec_from_file_location("modes.admin.notifier_test", _NOTIFIER_PATH)
if _NOTIFIER_SPEC is None or _NOTIFIER_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin notifier module from {_NOTIFIER_PATH}")
_NOTIFIER_MODULE = importlib.util.module_from_spec(_NOTIFIER_SPEC)
sys.modules[_NOTIFIER_SPEC.name] = _NOTIFIER_MODULE
_NOTIFIER_SPEC.loader.exec_module(_NOTIFIER_MODULE)
AdminNotifier = _NOTIFIER_MODULE.AdminNotifier


class _FakeMessaging:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "chat_id": int(chat_id),
                "text": str(text or ""),
                "md2": bool(md2),
                "kwargs": dict(kwargs),
            }
        )
        return None


def test_admin_notifier_does_not_send_notifications_during_mute_period(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        session_id = "sess-muted-1"
        store.upsert_session_state(session_id, chat_id=1, status="enabled", muted_until_ts=2_000.0)
        notifier = AdminNotifier(state_store=store)
        messaging = _FakeMessaging()

        incident_row = {
            "incident_id": "inc-1",
            "session_id": session_id,
            "payload": {"decision": {"action": "notify_admin", "urgency": "high", "reason": "test"}},
        }
        result = await notifier.notify_incident(
            session_id=session_id,
            chat_id=1,
            incident_row=incident_row,
            messaging=messaging,  # type: ignore[arg-type]
            now_ts=1_000.0,
        )

        assert result.sent is False
        assert result.muted is True
        assert float(result.muted_until_ts or 0.0) == 2_000.0
        assert messaging.calls == []

    asyncio.run(_run())


def test_admin_notifier_formats_incident_and_action_messages_as_markdownv2(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        session_id = "sess_[1](x)!"
        store.upsert_session_state(session_id, chat_id=101, status="enabled")
        notifier = AdminNotifier(state_store=store)
        messaging = _FakeMessaging()

        incident_row = {
            "incident_id": "inc_[42](a)!",
            "session_id": session_id,
            "payload": {
                "decision": {
                    "action": "notify_admin_[x]",
                    "urgency": "high!",
                    "reason": "bad_(chars)[here]",
                }
            },
        }
        action_row = {
            "action_id": "act_[7](b)!",
            "session_id": session_id,
            "payload": {
                "event": "executed_failed_(policy)",
                "result": {"success": False, "returncode": -1},
            },
        }

        inc_result = await notifier.notify_incident(
            session_id=session_id,
            chat_id=101,
            incident_row=incident_row,
            messaging=messaging,  # type: ignore[arg-type]
            now_ts=1_000.0,
        )
        act_result = await notifier.notify_action(
            session_id=session_id,
            chat_id=101,
            action_row=action_row,
            messaging=messaging,  # type: ignore[arg-type]
            now_ts=1_001.0,
        )

        assert inc_result.sent is True
        assert act_result.sent is True
        assert len(messaging.calls) == 2
        assert all(bool(call["md2"]) is True for call in messaging.calls)

        incident_text = str(messaging.calls[0]["text"] or "")
        assert "*🛡 Admin Incident*" in incident_text
        assert "sess\\_\\[1\\]\\(x\\)\\!" in incident_text
        assert "inc\\_\\[42\\]\\(a\\)\\!" in incident_text
        assert "notify\\_admin\\_\\[x\\]" in incident_text
        assert "bad\\_\\(chars\\)\\[here\\]" in incident_text

        action_text = str(messaging.calls[1]["text"] or "")
        assert "*🛡 Admin Action*" in action_text
        assert "act\\_\\[7\\]\\(b\\)\\!" in action_text
        assert "executed\\_failed\\_\\(policy\\)" in action_text

    asyncio.run(_run())


def test_admin_notifier_state_isolated_between_sequential_sessions(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        muted_session = "sess-muted"
        open_session = "sess-open"
        store.upsert_session_state(muted_session, chat_id=1, status="enabled", muted_until_ts=5_000.0)
        store.upsert_session_state(open_session, chat_id=1, status="enabled", muted_until_ts=None)
        notifier = AdminNotifier(state_store=store)
        messaging = _FakeMessaging()
        incident_row = {
            "incident_id": "inc-seq",
            "payload": {"decision": {"action": "notify_admin", "urgency": "high", "reason": "seq"}},
        }

        muted_result = await notifier.notify_incident(
            session_id=muted_session,
            chat_id=1,
            incident_row=incident_row,
            messaging=messaging,  # type: ignore[arg-type]
            now_ts=1_000.0,
        )
        open_result = await notifier.notify_incident(
            session_id=open_session,
            chat_id=1,
            incident_row=incident_row,
            messaging=messaging,  # type: ignore[arg-type]
            now_ts=1_000.0,
        )

        assert muted_result.sent is False
        assert muted_result.muted is True
        assert open_result.sent is True
        assert open_result.muted is False
        assert len(messaging.calls) == 1

    asyncio.run(_run())
