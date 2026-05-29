from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from modes.sdk.runtime.events import EventSeverity, EventType, OrchestratorEvent
from modes.sdk.runtime.json_normalizer import JSONSchemaValidationError, parse_normalize_validate
from modes.sdk.runtime.reactions import ReactionAction, ReactionEngine, ReactionRule
from modes.sdk.runtime.validation import (
    LanguageStack,
    ToolchainCommand,
    ValidationAdapter,
    ValidationStatus,
)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "retry_count": {"type": "integer", "default": 0},
        "needs_input": {"type": "boolean", "default": False},
        "reroute": {"type": "boolean", "default": False},
        "target_mode": {"type": "string", "default": ""},
        "meta": {"type": "object", "default": {}},
    },
    "required": ["task"],
    "additionalProperties": True,
}


@dataclass(frozen=True)
class SyntheticScenario:
    name: str
    raw: str
    parse_error: Optional[Any]
    required_actions: tuple[str, ...]
    expected_retry_status: Optional[str]
    validation_mode: str
    issue_tag: str


def _wrap(kind: str, raw: str) -> str:
    if kind == "plain":
        return raw
    if kind == "fenced_json":
        return f"```json\n{raw}\n```"
    if kind == "fenced_any":
        return f"```\n{raw}\n```"
    if kind == "surrounded":
        return f"Noise before\n{raw}\nNoise after"
    raise ValueError(f"unknown wrapper kind: {kind}")


def _build_scenarios() -> list[SyntheticScenario]:
    wrappers = ("plain", "fenced_json", "fenced_any", "surrounded")
    cases = [
        {
            "id": "minimal",
            "raw": '{"task":"alpha"}',
            "parse_error": None,
            "required_actions": ("retry_step",),
            "expected_retry_status": "queued",
            "issue_tag": "baseline",
        },
        {
            "id": "retry_high",
            "raw": '{"task":"alpha","retry_count":7}',
            "parse_error": None,
            "required_actions": ("retry_step",),
            "expected_retry_status": "skipped",
            "issue_tag": "baseline",
        },
        {
            "id": "needs_input",
            "raw": '{"task":"alpha","needs_input":true}',
            "parse_error": None,
            "required_actions": ("retry_step", "ask_user"),
            "expected_retry_status": "queued",
            "issue_tag": "baseline",
        },
        {
            "id": "reroute",
            "raw": '{"task":"alpha","reroute":true,"target_mode":"analyst"}',
            "parse_error": None,
            "required_actions": ("retry_step", "reroute"),
            "expected_retry_status": "queued",
            "issue_tag": "baseline",
        },
        {
            "id": "needs_and_reroute",
            "raw": '{"task":"alpha","needs_input":true,"reroute":true}',
            "parse_error": None,
            "required_actions": ("retry_step", "ask_user", "reroute"),
            "expected_retry_status": "queued",
            "issue_tag": "baseline",
        },
        {
            "id": "issue5_control_char",
            "raw": '{"task":"alpha","meta":{"note":"bad\x01char"}}',
            "parse_error": None,
            "required_actions": ("retry_step",),
            "expected_retry_status": "queued",
            "issue_tag": "issue5",
        },
        {
            "id": "issue5_invalid_escape",
            "raw": '{"task":"alpha","meta":{"note":"bad\\qescape"}}',
            "parse_error": None,
            "required_actions": ("retry_step",),
            "expected_retry_status": "queued",
            "issue_tag": "issue5",
        },
        {
            "id": "issue5_missing_required",
            "raw": '{"retry_count":1}',
            "parse_error": JSONSchemaValidationError,
            "required_actions": ("retry_step", "ask_user"),
            "expected_retry_status": "skipped",
            "issue_tag": "issue5",
        },
        {
            "id": "issue5_non_object",
            "raw": '["a","b"]',
            "parse_error": (TypeError, json.JSONDecodeError),
            "required_actions": ("retry_step", "ask_user"),
            "expected_retry_status": "skipped",
            "issue_tag": "issue5",
        },
        {
            "id": "issue5_malformed_json",
            "raw": '{"task":"alpha"',
            "parse_error": json.JSONDecodeError,
            "required_actions": ("retry_step", "ask_user"),
            "expected_retry_status": "skipped",
            "issue_tag": "issue5",
        },
    ]

    scenarios: list[SyntheticScenario] = []
    index = 0
    for wrapper in wrappers:
        for case in cases:
            if case["parse_error"] is not None:
                validation_mode = "issue3_not_run"
            elif index % 3 == 0:
                validation_mode = "ok"
            elif index % 3 == 1:
                validation_mode = "failed"
            else:
                validation_mode = "issue3_not_run"
            scenarios.append(
                SyntheticScenario(
                    name=f"{wrapper}:{case['id']}",
                    raw=_wrap(wrapper, str(case["raw"])),
                    parse_error=case["parse_error"],
                    required_actions=tuple(case["required_actions"]),
                    expected_retry_status=case["expected_retry_status"],
                    validation_mode=validation_mode,
                    issue_tag=str(case["issue_tag"]),
                )
            )
            index += 1

    # Additional validation-heavy synthetic matrix for not_run / issue3.
    for i in range(12):
        scenarios.append(
            SyntheticScenario(
                name=f"validation-matrix-{i:02d}",
                raw='{"task":"matrix","retry_count":1}',
                parse_error=None,
                required_actions=("retry_step",),
                expected_retry_status="queued",
                validation_mode="issue3_not_run" if i % 2 == 0 else "failed",
                issue_tag="issue3",
            )
        )

    return scenarios


SCENARIOS = _build_scenarios()


class _SyntheticValidationAdapter(ValidationAdapter):
    stack = LanguageStack.PYTHON

    def __init__(self, mode: str):
        self._mode = mode

    def build_toolchain(self, _workdir: str) -> list[ToolchainCommand]:
        if self._mode == "issue3_not_run":
            return [ToolchainCommand(tool="missing", command=["__missing_v2_binary__"], optional=False)]
        if self._mode == "failed":
            return [ToolchainCommand(tool="pytest", command=["bash", "-lc", "false"], optional=False)]
        return [ToolchainCommand(tool="flake8", command=["bash", "-lc", "true"], optional=False)]


def _build_rules() -> list[ReactionRule]:
    return [
        ReactionRule(
            rule_id="v2.retry",
            event_types=[EventType.STEP_FAILED],
            min_severity=EventSeverity.ERROR,
            actions=[ReactionAction(action_type="retry_step", params={"max_retries": 2})],
        ),
        ReactionRule(
            rule_id="v2.ask",
            event_types=[EventType.STEP_FAILED],
            min_severity=EventSeverity.ERROR,
            payload_equals={"needs_input": True},
            actions=[ReactionAction(action_type="ask_user", params={"question": "Need input?"})],
        ),
        ReactionRule(
            rule_id="v2.reroute",
            event_types=[EventType.STEP_FAILED],
            min_severity=EventSeverity.ERROR,
            payload_equals={"reroute": True},
            actions=[ReactionAction(action_type="reroute", params={"target_mode": "analyst"})],
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_v2_synthetic_scenario_matrix(scenario: SyntheticScenario) -> None:
    parsed_payload: dict[str, Any]
    parse_failed = False

    try:
        parsed_payload = parse_normalize_validate(scenario.raw, SCHEMA)
        if scenario.parse_error is not None:
            raise AssertionError(f"scenario {scenario.name} expected parse error {scenario.parse_error}")
    except Exception as exc:  # noqa: BLE001
        if scenario.parse_error is None:
            raise
        expected_error = scenario.parse_error
        if isinstance(expected_error, tuple):
            assert isinstance(exc, expected_error)
        else:
            assert isinstance(exc, expected_error)
        parse_failed = True
        parsed_payload = {
            "task": "parse_failed",
            "retry_count": 99,
            "needs_input": True,
            "reroute": False,
            "target_mode": "",
        }

    async def _ask_user(_question: str, options: list[str], _ctx: dict[str, Any]) -> str:
        return options[0] if options else "ok"

    engine = ReactionEngine(ask_user_fn=_ask_user)

    async def _reroute_handler(event: OrchestratorEvent, action: ReactionAction, _ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "reroute",
            "status": "queued",
            "step_id": event.step_id,
            "target_mode": str(action.params.get("target_mode") or "unknown"),
        }

    engine.register_action("reroute", _reroute_handler)
    event = OrchestratorEvent(
        event_type=EventType.STEP_FAILED,
        severity=EventSeverity.ERROR,
        session_id="synthetic",
        step_id=scenario.name,
        message="synthetic failure",
        payload={
            "retry_count": int(parsed_payload.get("retry_count", 0)),
            "needs_input": bool(parsed_payload.get("needs_input", False)),
            "reroute": bool(parsed_payload.get("reroute", False)),
        },
    )
    reaction_results = await engine.execute(event, _build_rules(), ctx={"scenario": scenario.name})
    action_names = [str(item.get("action") or "") for item in reaction_results]
    for expected_action in scenario.required_actions:
        assert expected_action in action_names
    if scenario.expected_retry_status:
        retry_statuses = [str(item.get("status") or "") for item in reaction_results if item.get("action") == "retry_step"]
        assert scenario.expected_retry_status in retry_statuses

    adapter = _SyntheticValidationAdapter(scenario.validation_mode)
    runner_calls = {"count": 0}

    async def _runner(command: list[str], _workdir: str) -> dict[str, Any]:
        runner_calls["count"] += 1
        if scenario.validation_mode == "failed":
            return {"exit_code": 1, "output": f"failed command: {' '.join(command)}"}
        return {"exit_code": 0, "output": "ok"}

    report = await adapter.run("/tmp/v2", _runner)
    if scenario.validation_mode == "ok":
        assert report.status is ValidationStatus.OK
        assert runner_calls["count"] == 1
    elif scenario.validation_mode == "failed":
        assert report.status is ValidationStatus.FAILED
        assert runner_calls["count"] == 1
    else:
        assert report.status is ValidationStatus.FAILED
        assert any(step.status is ValidationStatus.NOT_RUN for step in report.steps)
        assert runner_calls["count"] == 0

    # parse-failure scenarios must still execute reliability path via fallback payload.
    if parse_failed:
        assert "ask_user" in action_names


def test_v2_synthetic_scenario_count_and_issue_coverage() -> None:
    assert len(SCENARIOS) >= 50
    tags = {s.issue_tag for s in SCENARIOS}
    assert "issue3" in tags
    assert "issue5" in tags


def test_v2_synthetic_explicitly_covers_all_v2_paths() -> None:
    rule_actions = {
        action.action_type
        for rule in _build_rules()
        for action in rule.actions
    }
    assert {"retry_step", "ask_user", "reroute"}.issubset(rule_actions)
    assert any(s.parse_error is None for s in SCENARIOS)
    assert any(s.parse_error is not None for s in SCENARIOS)
    validation_modes = {s.validation_mode for s in SCENARIOS}
    assert {"ok", "failed", "issue3_not_run"}.issubset(validation_modes)


@pytest.mark.asyncio
async def test_v2_synthetic_stress_reaction_engine_under_load() -> None:
    async def _ask_user(_question: str, options: list[str], _ctx: dict[str, Any]) -> str:
        return options[0] if options else "ok"

    engine = ReactionEngine(ask_user_fn=_ask_user)

    async def _reroute_handler(event: OrchestratorEvent, _action: ReactionAction, _ctx: dict[str, Any]) -> dict[str, Any]:
        return {"action": "reroute", "status": "queued", "step_id": event.step_id}

    engine.register_action("reroute", _reroute_handler)
    rules = _build_rules()

    events = []
    for i in range(240):
        events.append(
            OrchestratorEvent(
                event_type=EventType.STEP_FAILED,
                severity=EventSeverity.ERROR,
                session_id="stress",
                step_id=f"step-{i}",
                message="boom",
                payload={"retry_count": i % 4, "needs_input": i % 3 == 0, "reroute": i % 5 == 0},
            )
        )

    async def _run_single(ev: OrchestratorEvent) -> list[dict[str, Any]]:
        return await engine.execute(ev, rules, ctx={"load": True})

    all_results = await asyncio.gather(*[_run_single(ev) for ev in events])
    flat = [item for batch in all_results for item in batch]
    assert len(flat) >= 240
    assert any(item.get("action") == "retry_step" for item in flat)
    assert any(item.get("action") == "ask_user" for item in flat)
    assert any(item.get("action") == "reroute" for item in flat)


def test_v2_synthetic_stress_normalizer_under_load() -> None:
    raw = "```json\n{\"task\":\"bulk\",\"meta\":{\"text\":\"a\\qb\"}}\n``` trailing"
    for _ in range(600):
        parsed = parse_normalize_validate(raw, SCHEMA)
        assert parsed["task"] == "bulk"
