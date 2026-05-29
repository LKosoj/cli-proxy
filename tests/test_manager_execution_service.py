from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from modes.manager.services import ExecutionTrackingService
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis


def test_execution_service_min_tasks_dynamic_is_deterministic() -> None:
    service = ExecutionTrackingService(min_tasks_floor=6, min_tasks_per_req=1, min_tasks_per_remaining=1)
    analysis = ProjectAnalysis(
        current_state="state",
        already_done=["seed"],
        remaining_work=["a", "b", "c", "d"],
        requirements=["REQ-1", "REQ-2", "REQ-3"],
        checklist_table=[
            {"item": "i1", "status": "not_done", "how": "", "why_not": "x"},
            {"item": "i2", "status": "done", "how": "ok", "why_not": ""},
            {"item": "i3", "status": "not_done", "how": "", "why_not": "x"},
            {"item": "i4", "status": "not_done", "how": "", "why_not": "x"},
        ],
    )

    first = service.min_tasks_dynamic(analysis)
    second = service.min_tasks_dynamic(analysis)
    assert first == second
    assert first == 7


def test_execution_service_parse_work_type_json_uses_schema_validation() -> None:
    service = ExecutionTrackingService()

    valid_raw = json.dumps({"work_type": "development", "confidence": 0.91, "reason": "code task"}, ensure_ascii=False)
    wt, conf, reason = service.parse_work_type_json(
        valid_raw,
        allowed_work_types=["development", "planning"],
    )
    assert wt == "development"
    assert conf == pytest.approx(0.91)
    assert reason == "code task"

    invalid_raw = json.dumps({"work_type": "development", "confidence": "high", "reason": "invalid"}, ensure_ascii=False)
    wt_bad, conf_bad, reason_bad = service.parse_work_type_json(
        invalid_raw,
        allowed_work_types=["development", "planning"],
    )
    assert wt_bad is None
    assert conf_bad == 0.0
    assert reason_bad == ""


def test_execution_service_extract_executor_primary_text_enforces_schema() -> None:
    text = ExecutionTrackingService.extract_executor_primary_text(
        SimpleNamespace(summary="fallback", outputs=[{"type": "text", "content": "primary"}])
    )
    assert text == "primary"

    fallback = ExecutionTrackingService.extract_executor_primary_text(
        SimpleNamespace(summary="fallback", outputs=[])
    )
    assert fallback == "fallback"

    with pytest.raises(ValueError):
        ExecutionTrackingService.extract_executor_primary_text(
            SimpleNamespace(summary="bad", outputs="not-a-list")
        )


def test_execution_service_parse_review_result_uses_schema() -> None:
    valid = "```json\n{\"approved\": true, \"summary\": \"ok\", \"comments\": \"\", \"files_reviewed\": []}\n```"
    parsed = ExecutionTrackingService.parse_review_result(valid)
    assert parsed is not None
    assert parsed.approved is True
    assert parsed.summary == "ok"

    invalid = "review output without valid json"
    assert ExecutionTrackingService.parse_review_result(invalid) is None


def test_execution_service_parse_review_result_coerces_schema_mismatch_to_comment_fallback() -> None:
    invalid = json.dumps(
        {
            "command": ".venv/bin/python -m pytest -q tests/test_analyst_intent_routing.py",
            "summary": "",
        },
        ensure_ascii=False,
    )
    parsed = ExecutionTrackingService.parse_review_result(invalid)
    assert parsed is not None
    assert parsed.approved is False
    assert parsed.summary == "Невалидный формат ответа ревьюера"
    assert "approved/summary/comments" in parsed.comments
    assert "команда" in parsed.comments
    assert parsed.not_done_assessment
    assert parsed.not_done_assessment[0]["item"] == "review_result_schema"


def test_execution_service_parse_review_result_prefers_final_review_json_in_mixed_output() -> None:
    mixed = (
        'tool payload {"path":"/srv/git_projects/Samovar/ui/web/ajax_snapshot.h","offset":1,"limit":200}\n'
        'final review {"approved":false,"summary":"Есть замечания","comments":"Нужна правка"}'
    )
    parsed = ExecutionTrackingService.parse_review_result(mixed)
    assert parsed is not None
    assert parsed.approved is False
    assert parsed.summary == "Есть замечания"
    assert parsed.comments == "Нужна правка"


def test_execution_service_parse_review_result_action_payload_fallback_does_not_log_json_normalizer_error(caplog) -> None:
    invalid = json.dumps(
        {
            "path": "/srv/git_projects/LLMApiGateway/llm_gateway_core/api/v1/embeddings.py",
            "offset": 1,
            "limit": 200,
        },
        ensure_ascii=False,
    )

    caplog.set_level("ERROR", logger="modes.sdk.runtime.json_normalizer")
    parsed = ExecutionTrackingService.parse_review_result(invalid)

    assert parsed is not None
    assert parsed.approved is False
    assert parsed.summary == "Невалидный формат ответа ревьюера"
    assert "path='/srv/git_projects/LLMApiGateway/llm_gateway_core/api/v1/embeddings.py'" in parsed.comments
    assert not [
        record for record in caplog.records
        if record.name == "modes.sdk.runtime.json_normalizer" and record.levelname == "ERROR"
    ]


def test_execution_service_parse_review_result_strict_mode_does_not_coerce_action_payload() -> None:
    invalid = json.dumps(
        {
            "path": "tests/test_miniapp_rc_settings_put.py",
            "offset": 1,
            "limit": 200,
        },
        ensure_ascii=False,
    )

    parsed = ExecutionTrackingService.parse_review_result(
        invalid,
        allow_action_payload_fallback=False,
    )

    assert parsed is None


def test_execution_service_build_review_instruction_preserves_literal_json_fields() -> None:
    template = (
        "### Задача: {task_title}\n"
        "Служебный пример: {\"path\": \"file.py\", \"pattern\": \"TODO\"}\n"
        "### Отчёт: {dev_report}\n"
        "### Коммит: {last_commit_info}\n"
    )

    rendered = ExecutionTrackingService.build_review_instruction(
        template,
        task_title="Починить manager review",
        task_description="desc",
        task_acceptance="- no crash",
        dev_report="done",
        last_commit_info="commit abc123",
    )

    assert "Починить manager review" in rendered
    assert '{"path": "file.py", "pattern": "TODO"}' in rendered
    assert "commit abc123" in rendered


def test_execution_service_build_review_instruction_keeps_fast_failure_for_unknown_placeholder() -> None:
    with pytest.raises(KeyError, match="task_titel"):
        ExecutionTrackingService.build_review_instruction(
            "### Задача: {task_titel}",
            task_title="ok",
            task_description="desc",
            task_acceptance="- acc",
            dev_report="done",
            last_commit_info="commit",
        )


def test_execution_service_sequential_runs_with_different_intents_are_isolated() -> None:
    service = ExecutionTrackingService()
    template = (
        "task={task_title};desc={task_description};acc={task_acceptance};"
        "ctx={project_context};done={already_done};completed={completed_tasks_summary};"
        "partial={partial_work_block};reject={rejection_block}"
    )

    first_prompt = service.build_dev_instruction(
        template,
        task_title="intent-one-task",
        task_description="first run",
        task_acceptance="- first",
        rejection_block="",
        partial_work_block="first-partial",
        project_context="ctx-one",
        already_done="done-one",
        completed_tasks_summary="completed-one",
    )
    second_prompt = service.build_dev_instruction(
        template,
        task_title="intent-two-task",
        task_description="second run",
        task_acceptance="- second",
        rejection_block="none",
        partial_work_block="second-partial",
        project_context="ctx-two",
        already_done="done-two",
        completed_tasks_summary="completed-two",
    )

    assert "intent-one-task" in first_prompt
    assert "intent-two-task" in second_prompt
    assert "ctx-one" not in second_prompt
    assert "done-one" not in second_prompt

    req_first = service.build_review_executor_request(
        task=DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["ok"]),
        instruction="review one",
        workdir="/tmp/one",
        allowed_tools=["use_cli"],
        deadline_ms=1000,
    )
    req_second = service.build_review_executor_request(
        task=DevTask(id="task_2", title="T2", description="D2", acceptance_criteria=["ok"]),
        instruction="review two",
        workdir="/tmp/two",
        allowed_tools=["ask_user"],
        deadline_ms=2000,
    )
    req_first.inputs["mutation"] = "x"

    assert req_first.task_id == "review:task_1"
    assert req_second.task_id == "review:task_2"
    assert req_second.inputs == {}
    assert req_first.context == "workdir=/tmp/one"
    assert req_second.context == "workdir=/tmp/two"
