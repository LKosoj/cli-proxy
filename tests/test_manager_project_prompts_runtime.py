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


def test_manager_orchestrator_loads_system_prompts_in_active_lang(tmp_path) -> None:
    """W3: _load_manager_prompts must honour the per-run resolved language so
    non-Russian users get localized LLM system prompts, not always Russian.
    Language lives in a ContextVar (per asyncio task) so concurrent runs on the
    shared singleton orchestrator cannot clobber each other."""
    from agent.manager_core import _MANAGER_ACTIVE_LANG

    ensure_project_prompts(str(tmp_path))
    orch = _make_orchestrator()

    tok = _MANAGER_ACTIVE_LANG.set("ru")
    try:
        prompts_ru = orch._load_manager_prompts(str(tmp_path))
    finally:
        _MANAGER_ACTIVE_LANG.reset(tok)

    tok = _MANAGER_ACTIVE_LANG.set("de")
    try:
        prompts_de = orch._load_manager_prompts(str(tmp_path))
    finally:
        _MANAGER_ACTIVE_LANG.reset(tok)

    # A localized SYSTEM prompt key must be served in the active language.
    for key in ("commit_message_system", "final_report_system"):
        assert prompts_ru.get(key), f"missing ru system prompt: {key}"
        assert prompts_de.get(key), f"missing de system prompt: {key}"
    assert prompts_ru["commit_message_system"] != prompts_de["commit_message_system"]

    # Outside of any run() (ContextVar at its default) it falls back to Russian.
    prompts_default = orch._load_manager_prompts(str(tmp_path))
    assert prompts_default["commit_message_system"] == prompts_ru["commit_message_system"]


def test_manager_active_lang_isolated_across_concurrent_tasks(tmp_path) -> None:
    """W3 race: the shared singleton orchestrator must not let one task's
    language leak into a concurrently-running task. ContextVar guarantees each
    asyncio task carries its own copy."""
    from agent.manager_core import _MANAGER_ACTIVE_LANG

    ensure_project_prompts(str(tmp_path))
    orch = _make_orchestrator()

    async def _run_one(lang: str) -> str:
        _MANAGER_ACTIVE_LANG.set(lang)
        # Yield control so the other task interleaves between set and read.
        await asyncio.sleep(0)
        prompts = orch._load_manager_prompts(str(tmp_path))
        return prompts["commit_message_system"]

    async def _main() -> tuple[str, str]:
        return await asyncio.gather(_run_one("de"), _run_one("ru"))  # type: ignore[return-value]

    de_text, ru_text = asyncio.run(_main())
    assert de_text != ru_text  # each task kept its own language despite interleaving
