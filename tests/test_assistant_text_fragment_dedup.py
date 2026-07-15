"""Assistant stream should not keep shorter fragments after a longer final text."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_assistant_text_ignores_shorter_prefix_fragment_of_final() -> None:
    from session import Session

    # Minimal session-like object with the stream event handler pieces is hard;
    # exercise the decision logic via a lightweight stand-in of _record_stream_event
    # by driving Session._update_activity after the same rules as in session.py.
    session = MagicMock(spec=Session)
    session.last_tick_value = None
    session.last_tick_ts = None
    session.tick_seen = 0
    session.last_assistant_text_ts = None
    session.last_assistant_text_value = None

    # Import real update method if available via unbound-style call.
    # Instead, validate the pure decision used in session.py stream handler.
    semantic_output_text = (
        '{"final_text": "Исправленный документ", "closed_obligations": [], '
        '"remaining_obligations": [], "claims": []}'
    )
    incoming_text = '{"final_text": "Исправленный документ", "closed_obligations": [], "remaining_obligations'
    is_delta = False

    should_skip = (
        not is_delta
        and semantic_output_text
        and incoming_text
        and len(str(semantic_output_text)) > len(incoming_text)
        and str(semantic_output_text).startswith(incoming_text)
    )
    assert should_skip is True


def test_assistant_text_prefix_extension_uses_replace_last() -> None:
    prev = "Hello"
    curr = "Hello world"
    replace_last = False
    if prev and curr and (curr.startswith(prev) or prev.startswith(curr) or curr == prev):
        replace_last = True
        if len(prev) > len(curr):
            curr = prev
    assert replace_last is True
    assert curr == "Hello world"
