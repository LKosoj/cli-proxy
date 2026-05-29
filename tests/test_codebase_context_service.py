from __future__ import annotations

import types

from modes.analyst.mode import AnalystMode
from modes.manager.mode import ManagerMode
from modes.sdk.services import CodebaseContextService, CodebaseContextText


class _FakeMapper:
    def __init__(self, status: dict) -> None:
        self._status = dict(status)
        self.calls = []

    def get_status(self, *, workdir: str) -> dict:
        self.calls.append(str(workdir))
        return dict(self._status)


def _runtime_getter(mapper: object):
    return lambda capability: mapper if capability == "codebase_mapper_status" else None


def test_codebase_context_service_renders_known_docs_in_stable_order() -> None:
    mapper = _FakeMapper(
        {
            "status": "ready",
            "docs": [
                "CONCERNS.md",
                "STACK.md",
                "INTEGRATIONS.md",
            ],
        }
    )
    session = types.SimpleNamespace(workdir="/tmp/project")
    text = CodebaseContextText(
        intro="intro",
        stack="stack",
        architecture="architecture",
        structure="structure",
        integrations="integrations",
        conventions="conventions",
        testing="testing",
        concerns="concerns",
        outro="outro",
    )
    rendered = CodebaseContextService.build_context(
        session=session,
        runtime_getter=_runtime_getter(mapper),
        text=text,
    )

    assert mapper.calls == ["/tmp/project"]
    assert rendered.splitlines() == [
        "intro",
        "stack",
        "integrations",
        "concerns",
        "outro",
    ]


def test_codebase_context_service_returns_empty_when_mapper_not_ready() -> None:
    mapper = _FakeMapper({"status": "building", "docs": ["STACK.md"]})
    session = types.SimpleNamespace(workdir="/tmp/project")
    text = CodebaseContextText(
        intro="intro",
        stack="stack",
        architecture="architecture",
        structure="structure",
        integrations="integrations",
        conventions="conventions",
        testing="testing",
        concerns="concerns",
        outro="outro",
    )
    rendered = CodebaseContextService.build_context(
        session=session,
        runtime_getter=_runtime_getter(mapper),
        text=text,
    )
    assert rendered == ""


def test_analyst_mode_builds_context_via_shared_service_with_prompt_overrides() -> None:
    mode = AnalystMode()
    mode._prompts = {
        "codebase_intro": "analyst intro",
        "codebase_stack": "analyst stack",
        "codebase_architecture": "analyst architecture",
        "codebase_structure": "analyst structure",
        "codebase_integrations": "analyst integrations",
        "codebase_conventions": "analyst conventions",
        "codebase_testing": "analyst testing",
        "codebase_concerns": "analyst concerns",
        "codebase_outro": "analyst outro",
    }
    mapper = _FakeMapper({"status": "ready", "docs": ["STACK.md", "TESTING.md"]})
    mode.initialize(services={"runtime_by_capability": _runtime_getter(mapper)})

    rendered = mode._build_codebase_context(session=types.SimpleNamespace(workdir="/tmp/a"))

    assert rendered.splitlines() == [
        "analyst intro",
        "analyst stack",
        "analyst testing",
        "analyst outro",
    ]


def test_manager_mode_builds_context_via_shared_service_with_prompt_overrides() -> None:
    mode = ManagerMode()
    prompt_values = {
        "codebase_intro": "manager intro",
        "codebase_stack": "manager stack",
        "codebase_architecture": "manager architecture",
        "codebase_structure": "manager structure",
        "codebase_integrations": "manager integrations",
        "codebase_conventions": "manager conventions",
        "codebase_testing": "manager testing",
        "codebase_concerns": "manager concerns",
        "codebase_outro": "manager outro",
    }
    mapper = _FakeMapper({"status": "ready", "docs": ["ARCHITECTURE.md", "CONCERNS.md"]})
    mode.initialize(services={"runtime_by_capability": _runtime_getter(mapper)})
    mode._load_prompts = lambda *, session: dict(prompt_values)

    rendered = mode._build_codebase_context(session=types.SimpleNamespace(workdir="/tmp/m"))

    assert rendered.splitlines() == [
        "manager intro",
        "manager architecture",
        "manager concerns",
        "manager outro",
    ]
