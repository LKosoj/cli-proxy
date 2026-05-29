from __future__ import annotations

import asyncio
import json
import types

import yaml

from agent.manager import ManagerOrchestrator
from app.services.project_prompts_service import ensure_project_prompts
from modes.sdk.runtime.contracts import DevTask, ReviewResult


def _make_orchestrator() -> ManagerOrchestrator:
    obj = object.__new__(ManagerOrchestrator)
    obj._config = types.SimpleNamespace(
        defaults=types.SimpleNamespace(manager_response_archive=False)
    )
    return obj


def test_manager_orchestrator_reloads_decision_prompt_from_project_yaml(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        ensure_project_prompts(str(tmp_path))
        orch = _make_orchestrator()
        prompts_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "prompts.yaml"
        task = DevTask(id="t1", title="T", description="D", acceptance_criteria=["ok"])
        review = ReviewResult(approved=True, summary="ok", comments="")
        captured_system: list[str] = []

        async def _fake_chat_completion(_cfg, system, _user, **_kwargs):
            captured_system.append(str(system))
            return json.dumps({"verdict": "approved", "reasons": []}, ensure_ascii=False)

        monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)

        payload = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}
        prompts = payload.get("prompts") if isinstance(payload, dict) else {}
        if not isinstance(prompts, dict):
            prompts = {}
        prompts["decision_system"] = "MARKER_DECISION_PROMPT_V1"
        prompts_path.write_text(
            yaml.safe_dump({"prompts": prompts}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        verdict_1, _ = await orch._make_decision(task, review, workdir=str(tmp_path))

        prompts["decision_system"] = "MARKER_DECISION_PROMPT_V2"
        prompts_path.write_text(
            yaml.safe_dump({"prompts": prompts}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        verdict_2, _ = await orch._make_decision(task, review, workdir=str(tmp_path))

        assert verdict_1 == "approved"
        assert verdict_2 == "approved"
        assert captured_system[-2] == "MARKER_DECISION_PROMPT_V1"
        assert captured_system[-1] == "MARKER_DECISION_PROMPT_V2"

    asyncio.run(_run())
