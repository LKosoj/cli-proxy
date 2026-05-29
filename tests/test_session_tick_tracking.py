import time

from app.services.session_tick_history_store import load_session_ticks
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session


def _build_session(tmp_path) -> Session:
    tool = ToolConfig(
        name="dummy",
        mode="headless",
        cmd=["bash", "-lc", "cat"],
        headless_cmd=["bash", "-lc", "cat"],
    )
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={"dummy": tool},
        defaults=DefaultsConfig(workdir=str(tmp_path), state_path=str(tmp_path / "state.json")),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return Session(
        id="s1",
        tool=tool,
        workdir=str(tmp_path),
        idle_timeout_sec=10,
        config=cfg,
        chat_id=777,
    )


def test_session_tick_tracking_updates_on_first_tick(tmp_path) -> None:
    session = _build_session(tmp_path)
    assert session.last_tick_ts is None
    assert session.tick_seen == 0
    assert load_session_ticks(session) == []

    session._update_activity("progress 100001s")
    assert session.last_tick_ts is not None
    assert session.tick_seen == 1
    assert session.last_tick_value == "100001s"
    tick_history = load_session_ticks(session)
    assert len(tick_history) == 1
    assert tick_history[0]["value"] == "100001s"
    assert float(tick_history[0]["ts"]) > 0.0


def test_session_tick_tracking_counts_only_when_tick_changes(tmp_path) -> None:
    session = _build_session(tmp_path)
    session._update_activity("progress 100001s")
    first_ts = float(session.last_tick_ts or 0.0)

    session._update_activity("progress 100001s")
    assert session.tick_seen == 1
    assert float(session.last_tick_ts or 0.0) >= first_ts
    assert len(load_session_ticks(session)) == 1

    time.sleep(0.01)
    session._update_activity("progress 100002s")
    assert session.tick_seen == 2
    assert float(session.last_tick_ts or 0.0) > first_ts
    assert session.last_tick_value == "100002s"
    assert [str(item.get("value")) for item in load_session_ticks(session)] == ["100001s", "100002s"]


def test_session_tick_tracking_ignores_short_mm_ss_time_tokens(tmp_path) -> None:
    session = _build_session(tmp_path)

    session._update_activity("progress 01:23")
    assert session.tick_seen == 0
    assert session.last_tick_value is None

    first_ts = session.last_tick_ts
    time.sleep(0.01)
    session._update_activity("progress 01:24")
    assert session.tick_seen == 0
    assert session.last_tick_value is None
    assert session.last_tick_ts == first_ts


def test_session_tick_tracking_falls_back_to_any_output(tmp_path) -> None:
    session = _build_session(tmp_path)

    session._update_activity("model: thinking hard without explicit tick token")
    assert session.tick_seen == 1
    assert isinstance(session.last_tick_value, str)
    assert session.last_tick_value.startswith("model: thinking hard")

    first_ts = float(session.last_tick_ts or 0.0)
    time.sleep(0.01)
    session._update_activity("model: thinking hard without explicit tick token")
    assert session.tick_seen == 1
    assert float(session.last_tick_ts or 0.0) > first_ts


def test_session_tick_tracking_store_keeps_only_last_100_ticks(tmp_path) -> None:
    session = _build_session(tmp_path)
    for idx in range(125):
        session._update_activity(f"progress {100000 + idx}s")

    tick_history = load_session_ticks(session)
    assert len(tick_history) == 100
    values = [str(item.get("value")) for item in tick_history]
    assert values[0] == "100025s"
    assert values[-1] == "100124s"


def test_session_tick_tracking_store_skips_short_ticks(tmp_path) -> None:
    session = _build_session(tmp_path)
    session._update_activity("progress 1s")
    assert session.last_tick_value is None
    assert session.tick_seen == 0
    assert load_session_ticks(session) == []


def test_session_tick_tracking_can_store_short_final_text_when_allowed(tmp_path) -> None:
    session = _build_session(tmp_path)

    session._update_activity("OK", allow_short=True)

    assert session.last_tick_value == "OK"
    assert session.tick_seen == 1
    tick_history = load_session_ticks(session)
    assert len(tick_history) == 1
    assert tick_history[0]["value"] == "OK"


def test_session_tick_tracking_marks_assistant_text_and_keeps_it_separately(tmp_path) -> None:
    session = _build_session(tmp_path)

    session._update_activity("Assistant says hello", tick_kind="assistant_text")
    session._update_activity("tool event 100001s", tick_kind="tool_event")

    assert session.last_tick_value == "100001s"
    assert session.last_assistant_text_value == "Assistant says hello"
    assert session.last_assistant_text_ts is not None
    tick_history = load_session_ticks(session)
    assert tick_history[0]["kind"] == "assistant_text"
    assert tick_history[1]["kind"] == "tool_event"


def test_session_tick_tracking_replaces_streamed_assistant_delta_in_place(tmp_path) -> None:
    session = _build_session(tmp_path)

    session._update_activity("Assistant says", tick_kind="assistant_text", replace_last=True)
    session._update_activity("Assistant says hello", tick_kind="assistant_text", replace_last=True)

    assert session.tick_seen == 1
    assert session.last_tick_value == "Assistant says hello"
    assert session.last_assistant_text_value == "Assistant says hello"
    tick_history = load_session_ticks(session)
    assert tick_history == [
        {
            "ts": tick_history[0]["ts"],
            "value": "Assistant says hello",
            "kind": "assistant_text",
        }
    ]


def test_session_tick_tracking_ignores_time_only_assistant_text(tmp_path) -> None:
    session = _build_session(tmp_path)

    session._update_activity("Assistant says hello", tick_kind="assistant_text")
    first_assistant_ts = session.last_assistant_text_ts

    session._update_activity("04:58:45", tick_kind="assistant_text")

    assert session.last_assistant_text_value == "Assistant says hello"
    assert session.last_assistant_text_ts == first_assistant_ts
    assert session.last_tick_value == "Assistant says hello"
    tick_history = load_session_ticks(session)
    assert tick_history == [
        {
            "ts": tick_history[0]["ts"],
            "value": "Assistant says hello",
            "kind": "assistant_text",
        }
    ]


def test_session_tick_tracking_falls_back_to_full_text_for_short_token_when_allowed(tmp_path) -> None:
    session = _build_session(tmp_path)

    session._update_activity("done 50%", allow_short=True)

    assert session.last_tick_value == "done 50%"
    assert session.tick_seen == 1
    tick_history = load_session_ticks(session)
    assert len(tick_history) == 1
    assert tick_history[0]["value"] == "done 50%"


def test_session_tick_tracking_supports_bracket_format(tmp_path) -> None:
    """Qwen Code style: [123s]"""
    session = _build_session(tmp_path)

    session._update_activity("working [100001s]")
    assert session.tick_seen == 1
    assert session.last_tick_value == "[100001s]"

    session._update_activity("working [100002s]")
    assert session.tick_seen == 2
    assert session.last_tick_value == "[100002s]"


def test_session_tick_tracking_supports_checkmark_format(tmp_path) -> None:
    """Qwen Code style: ✓ 123s"""
    session = _build_session(tmp_path)

    session._update_activity("✓ 100001s completed")
    assert session.tick_seen == 1
    assert "✓" in session.last_tick_value

    session._update_activity("✓ 100002s completed")
    assert session.tick_seen == 2


def test_session_tick_tracking_supports_step_format(tmp_path) -> None:
    """Step/tick format: step #123 or tick:123"""
    session = _build_session(tmp_path)

    session._update_activity("step #42: analyzing")
    assert session.tick_seen == 1
    assert "42" in session.last_tick_value

    session._update_activity("tick:43 processing")
    assert session.tick_seen == 2


def test_session_tick_tracking_supports_percent_format(tmp_path) -> None:
    """Percent format: 50%, 100%"""
    session = _build_session(tmp_path)

    # Percentages are short (< 6 chars) so they fall back to full text
    # This test verifies that percent output is still tracked as activity
    session._update_activity("processing 50 percent complete")
    assert session.tick_seen == 1

    session._update_activity("processing 75 percent complete")
    assert session.tick_seen == 2

    session._update_activity("done 100 percent")
    assert session.tick_seen == 3
