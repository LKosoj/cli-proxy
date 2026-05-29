import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from modes.analyst.mode import AnalystMode
from modes.sdk.services.messaging import MessagingService


def test_analyst_progress_section_has_no_private_bot_message_calls():
    source = (Path(__file__).resolve().parents[1] / "modes" / "analyst" / "mode.py").read_text(
        encoding="utf-8",
    )
    progress_section = source.split("# ── progress ticker", 1)[1].split("async def _progress_ticker", 1)[0]

    assert "_send_message" not in progress_section
    assert "_edit_message" not in progress_section
    assert "_delete_message" not in progress_section


def test_analyst_progress_telegram_send_edit_delete_uses_messaging_service():
    async def _run():
        calls = []
        transport_context = object()

        async def _send(context, **kwargs):
            calls.append(("send", context, dict(kwargs)))
            return SimpleNamespace(message_id=41)

        async def _edit(context, **kwargs):
            calls.append(("edit", context, dict(kwargs)))
            return SimpleNamespace(message_id=kwargs["message_id"])

        async def _delete(context, **kwargs):
            calls.append(("delete", context, dict(kwargs)))
            return True

        mode = AnalystMode()
        messaging = MessagingService(
            send_message=_send,
            edit_message=_edit,
            delete_message=_delete,
            transport_context=transport_context,
        )
        session = SimpleNamespace(analyst_pipeline_phase="classify")
        dest = {"kind": "telegram", "chat_id": 123}
        phase_labels = {
            "classify": "Классификация задачи",
            "gather": "Сбор контекста",
        }
        started_at = time.monotonic() - 65

        progress_ref = await mode._emit_analyst_progress(
            messaging=messaging,
            session=session,
            dest=dest,
            progress_msg_ref=None,
            pipeline_started_at=started_at,
            phase_labels=phase_labels,
        )
        session.analyst_pipeline_phase = "gather"
        edited_ref = await mode._emit_analyst_progress(
            messaging=messaging,
            session=session,
            dest=dest,
            progress_msg_ref=progress_ref,
            pipeline_started_at=started_at,
            phase_labels=phase_labels,
        )
        cleared_ref = await mode._clear_analyst_progress(
            messaging=messaging,
            bot_app=SimpleNamespace(),
            dest=dest,
            progress_msg_ref=edited_ref,
        )

        assert progress_ref.message_id == 41
        assert edited_ref is progress_ref
        assert cleared_ref is None
        assert calls[0][0] == "send"
        assert calls[0][1] is transport_context
        assert calls[0][2]["chat_id"] == 123
        assert calls[0][2]["text"].startswith("🧠 Аналитик: Классификация задачи\n⏱ 1:05")
        assert calls[1] == (
            "edit",
            transport_context,
            {
                "chat_id": 123,
                "message_id": 41,
                "text": "🧠 Аналитик: Сбор контекста\n⏱ 1:05",
                "md2": True,
            },
        )
        assert calls[2] == (
            "delete",
            transport_context,
            {"chat_id": 123, "message_id": 41},
        )

    asyncio.run(_run())
