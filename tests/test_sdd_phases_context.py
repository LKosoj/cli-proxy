from __future__ import annotations

import asyncio

from modes.sdd.phases import (
    _render_plan_md,
    _render_spec_md,
    generate_plan,
    generate_tasks,
    parse_affected_modules,
    parse_out_of_scope,
)

PLAN_PAYLOAD = {
    "architecture": "a",
    "stack": ["s"],
    "constraints": ["c"],
    "risks": ["r"],
    "affected_modules": ["modes/x.py"],
}
TASKS_PAYLOAD = {
    "project_goal": "goal",
    "tasks": [
        {
            "id": "TASK-1",
            "title": "t",
            "description": "d",
            "acceptance_criteria": ["WHEN x THE SYSTEM SHALL y"],
            "covers_requirements": ["REQ-1"],
            "depends_on": [],
        }
    ],
}

# A template that echoes every context placeholder so substitution is observable.
CTX_TEMPLATE = "{constitution}|{project_profile}|{relevant_nodes}|{out_of_scope}|{decisions}"


def test_generate_plan_passes_context_to_cli_call() -> None:
    captured = {}

    async def fake_cli_call(work_type, system, user, schema):
        captured.update(work_type=work_type, system=system, user=user, schema=schema)
        return dict(PLAN_PAYLOAD)

    plan_md, payload = asyncio.run(
        generate_plan(
            fake_cli_call,
            spec_md="# Spec\nsome spec body",
            constitution="CONST",
            project_profile="PROFILE",
            relevant_nodes="NODES",
            out_of_scope="- not this",
            decisions="DECISIONS",
            prompts={"plan": CTX_TEMPLATE},
        )
    )
    assert captured["work_type"] == "planning"
    assert captured["system"] == "CONST|PROFILE|NODES|- not this|DECISIONS"
    assert "some spec body" in captured["user"]
    assert payload["affected_modules"] == ["modes/x.py"]
    assert "## Affected Modules" in plan_md
    assert "modes/x.py" in plan_md


def test_generate_tasks_puts_spec_and_plan_in_user_message() -> None:
    captured = {}

    async def fake_cli_call(work_type, system, user, schema):
        captured.update(work_type=work_type, system=system, user=user)
        return dict(TASKS_PAYLOAD)

    plan = asyncio.run(
        generate_tasks(
            fake_cli_call,
            spec_md="SPEC_CONTENT",
            plan_md="PLAN_CONTENT",
            constitution="CONST",
            project_profile="PROFILE",
            relevant_nodes="NODES",
            out_of_scope="OOS",
            decisions="DEC",
            prompts={"tasks": CTX_TEMPLATE},
        )
    )
    assert captured["work_type"] == "planning"
    # Artifact bodies live in the user message; context lives in the system prompt.
    assert "SPEC_CONTENT" in captured["user"]
    assert "PLAN_CONTENT" in captured["user"]
    assert captured["system"] == "CONST|PROFILE|NODES|OOS|DEC"
    assert plan.tasks[0].id == "TASK-1"
    assert plan.project_goal == "goal"


def test_generate_plan_revision_appended_to_user() -> None:
    captured = {}

    async def fake_cli_call(work_type, system, user, schema):
        captured["user"] = user
        return dict(PLAN_PAYLOAD)

    asyncio.run(
        generate_plan(
            fake_cli_call,
            spec_md="SPEC",
            constitution="",
            project_profile="",
            relevant_nodes="",
            out_of_scope="",
            decisions="",
            prompts={"plan": ""},
            revision="please add caching",
        )
    )
    assert "please add caching" in captured["user"]
    assert "REVISION REQUEST" in captured["user"]


def test_parse_out_of_scope_from_spec_md() -> None:
    spec = (
        "# Spec\n\n## Requirements\n\n- **REQ-1**: do X\n\n"
        "## Out of Scope\n\n- No Y\n- No Z\n\n## Acceptance Criteria\n"
    )
    assert parse_out_of_scope(spec) == ["No Y", "No Z"]


def test_parse_out_of_scope_absent_returns_empty() -> None:
    assert parse_out_of_scope("# Spec\n\n## Requirements\n\n- **REQ-1**: x") == []


def test_parse_affected_modules_from_plan_md() -> None:
    plan = "# Technical Plan\n\n## Risks\n\n- r\n\n## Affected Modules\n\n- modes/a.py\n- modes/b.py\n"
    assert parse_affected_modules(plan) == ["modes/a.py", "modes/b.py"]


def test_render_spec_md_includes_out_of_scope_section() -> None:
    payload = {"feature_slug": "f", "out_of_scope": ["alpha", "beta"]}
    md = _render_spec_md(payload, intent="i")
    assert "## Out of Scope" in md
    assert "- alpha" in md
    assert "- beta" in md


def test_render_plan_md_includes_affected_modules_section() -> None:
    md = _render_plan_md(PLAN_PAYLOAD)
    assert "## Affected Modules" in md
    assert "- modes/x.py" in md


def test_render_spec_md_omits_out_of_scope_when_empty() -> None:
    md = _render_spec_md({"feature_slug": "f"}, intent="i")
    assert "## Out of Scope" not in md
