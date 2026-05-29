import pytest

from modes.sdk.runtime import (
    EventSeverity,
    EventType,
    OrchestratorEvent,
    ReactionAction,
    ReactionEngine,
    ReactionRule,
)


def _failed_event(*, retry_count: int = 0, error_code: str = "E1") -> OrchestratorEvent:
    return OrchestratorEvent(
        event_type=EventType.STEP_FAILED,
        severity=EventSeverity.ERROR,
        session_id="s1",
        step_id="step-1",
        message="step failed",
        payload={"retry_count": retry_count, "error_code": error_code},
    )


def test_reaction_rule_roundtrip_and_matching() -> None:
    rule = ReactionRule(
        rule_id="r1",
        event_types=[EventType.STEP_FAILED],
        min_severity=EventSeverity.WARNING,
        payload_equals={"error_code": "E1"},
        actions=[ReactionAction(action_type="retry_step", params={"max_retries": 2})],
        enabled=True,
    )
    raw = rule.to_dict()
    loaded = ReactionRule.from_dict(raw)

    assert loaded.rule_id == "r1"
    assert loaded.matches(_failed_event(retry_count=0, error_code="E1")) is True
    assert loaded.matches(_failed_event(retry_count=0, error_code="OTHER")) is False


@pytest.mark.asyncio
async def test_reaction_engine_executes_retry_and_ask_user_actions() -> None:
    captured = {"question": "", "options": []}

    async def _ask_user(question: str, options: list[str], _ctx: dict) -> str:
        captured["question"] = question
        captured["options"] = list(options)
        return "Повторить"

    engine = ReactionEngine(ask_user_fn=_ask_user)
    rules = [
        ReactionRule(
            rule_id="retry_on_fail",
            event_types=[EventType.STEP_FAILED],
            actions=[
                ReactionAction(action_type="retry_step", params={"max_retries": 3}),
                ReactionAction(
                    action_type="ask_user",
                    params={"question": "Повторить шаг?", "options": ["Повторить", "Остановить"]},
                ),
            ],
        )
    ]

    results = await engine.execute(_failed_event(retry_count=1), rules, ctx={"chat_id": 1})

    assert len(results) == 2
    assert results[0]["action"] == "retry_step"
    assert results[0]["status"] == "queued"
    assert results[1]["action"] == "ask_user"
    assert results[1]["status"] == "answered"
    assert captured["question"] == "Повторить шаг?"
    assert captured["options"] == ["Повторить", "Остановить"]


@pytest.mark.asyncio
async def test_reaction_engine_supports_custom_action_extensions() -> None:
    engine = ReactionEngine()

    async def _custom_handler(event: OrchestratorEvent, action: ReactionAction, _ctx: dict) -> dict:
        return {
            "action": action.action_type,
            "status": "ok",
            "step": event.step_id,
            "tag": action.params.get("tag"),
        }

    engine.register_action("custom_action", _custom_handler)
    rules = [
        ReactionRule(
            rule_id="custom",
            event_types=[EventType.STEP_FAILED],
            actions=[ReactionAction(action_type="custom_action", params={"tag": "mode-specific"})],
        )
    ]

    results = await engine.execute(_failed_event(retry_count=0), rules)

    assert results == [
        {"action": "custom_action", "status": "ok", "step": "step-1", "tag": "mode-specific"}
    ]


@pytest.mark.asyncio
async def test_reaction_engine_notify_failure_and_retry_limit() -> None:
    sent: list[str] = []

    async def _notify(msg: str, _ctx: dict) -> None:
        sent.append(msg)

    engine = ReactionEngine(notify_failure_fn=_notify)
    rules = [
        ReactionRule(
            rule_id="notify-and-retry",
            event_types=[EventType.STEP_FAILED],
            actions=[
                ReactionAction(action_type="retry_step", params={"max_retries": 1}),
                ReactionAction(action_type="notify_failure", params={"message": "Step failed permanently"}),
            ],
        )
    ]

    results = await engine.execute(_failed_event(retry_count=1), rules)

    assert results[0]["action"] == "retry_step"
    assert results[0]["status"] == "skipped"
    assert results[1]["action"] == "notify_failure"
    assert results[1]["status"] == "sent"
    assert sent == ["Step failed permanently"]
