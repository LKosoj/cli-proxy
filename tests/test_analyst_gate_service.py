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


# --- H6: пустой ответ не бросает RuntimeError, а пропускает шаг ---

@pytest.mark.asyncio
async def test_gate1_empty_answer_no_exception_returns_degraded(tmp_path) -> None:
    """Gate 1: пустой ответ от ask_fn не вызывает RuntimeError — шаг пропускается."""
    run_dir = AnalystRunDirectory(str(tmp_path), run_id="2026-04-13_gate1_empty")
    run_dir.create(
        analysis_profile="general",
        document_kind="report",
        detail_level="standard",
        template_id="base",
        summary="",
        user_request="test",
    )
    gate = AnalystGateService(run_dir)

    async def _ask_empty(question: str) -> str:
        return ""

    # Must not raise
    answers = await gate.execute_gate1(["Что нужно сделать?"], _ask_empty)
    assert answers == []


@pytest.mark.asyncio
async def test_gate1_mixed_answers_skips_empty_keeps_non_empty(tmp_path) -> None:
    """Gate 1: среди нескольких ответов пустые пропускаются, непустые сохраняются."""
    run_dir = AnalystRunDirectory(str(tmp_path), run_id="2026-04-13_gate1_mixed")
    run_dir.create(
        analysis_profile="general",
        document_kind="report",
        detail_level="standard",
        template_id="base",
        summary="",
        user_request="test",
    )
    gate = AnalystGateService(run_dir)

    responses = ["", "реальный ответ"]
    call_index = [0]

    async def _ask_mixed(question: str) -> str:
        resp = responses[call_index[0]]
        call_index[0] += 1
        return resp

    answers = await gate.execute_gate1(["Вопрос 1?", "Вопрос 2?"], _ask_mixed)
    assert answers == ["реальный ответ"]


@pytest.mark.asyncio
async def test_gate2_empty_answer_no_exception_returns_degraded(tmp_path) -> None:
    """Gate 2: пустой ответ от ask_fn не вызывает RuntimeError — шаг пропускается."""
    run_dir = _build_run_dir(tmp_path)
    run_dir.update_meta(
        steps=[{"id": "step1", "status": "partial", "gap": "нет данных о сроках"}]
    )
    gate = AnalystGateService(run_dir)

    async def _ask_empty(question: str) -> str:
        return ""

    # Must not raise
    answers = await gate.execute_gate2(_ask_empty)
    assert answers == []
    assert run_dir.load_meta().get("gate2_executed") is True


@pytest.mark.asyncio
async def test_gate2_logs_warning_on_empty_answer(tmp_path, caplog) -> None:
    """Gate 2: при пустом ответе записывается предупреждение в лог."""
    import logging
    run_dir = _build_run_dir(tmp_path)
    run_dir.update_meta(
        steps=[{"id": "stepX", "status": "partial", "gap": "неизвестный контекст"}]
    )
    gate = AnalystGateService(run_dir)

    async def _ask_empty(question: str) -> str:
        return ""

    with caplog.at_level(logging.WARNING, logger="modes.analyst.gate_service"):
        await gate.execute_gate2(_ask_empty)

    assert any("Gate 2" in r.message for r in caplog.records)
