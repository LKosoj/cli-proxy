from __future__ import annotations

from pathlib import Path

import pytest

from modes.analyst.gate_service import AnalystGateService
from modes.analyst.run_directory import AnalystRunDirectory


def _build_run_dir(tmp_path: Path) -> AnalystRunDirectory:
    run_dir = AnalystRunDirectory(str(tmp_path), run_id="2026-04-13_gate2")
    run_dir.create(
        analysis_profile="codebase",
        document_kind="spec",
        detail_level="standard",
        template_id="change_spec",
        summary="",
        user_request="Сделай ТЗ на доработку существующего проекта",
    )
    run_dir.update_meta(analysis_profile="codebase", document_kind="spec")
    Path(run_dir.codebase_context_path()).write_text("repo context", encoding="utf-8")
    return run_dir


@pytest.mark.asyncio
async def test_gate2_skips_generic_questions_for_repo_grounded_spec_without_partial_steps(tmp_path) -> None:
    run_dir = _build_run_dir(tmp_path)
    gate = AnalystGateService(run_dir)
    asked: list[str] = []

    async def _ask(question: str) -> str:
        asked.append(question)
        return "ok"

    answers = await gate.execute_gate2(_ask)

    assert asked == []
    assert answers == []
    assert gate.is_questions_locked() is False
    assert run_dir.load_meta().get("gate2_executed") is True
