from __future__ import annotations

import logging
import re
from string import Formatter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modes.manager.schemas import (
    EXECUTOR_RESPONSE_OUTPUT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    WORK_TYPE_CLASSIFIER_SCHEMA,
    validate_payload,
)
from modes.sdk.runtime.contracts import DevTask, ExecutorRequest, ReviewResult
from modes.sdk.runtime.json_normalizer import loads_safe, parse_normalize_validate

_PROMPT_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecutionTrackingService:
    """Execution/runtime helpers for Manager mode: prompts, limits and output validation."""

    def __init__(
        self,
        *,
        min_tasks_floor: int = 6,
        min_tasks_per_req: int = 1,
        min_tasks_per_remaining: int = 1,
    ) -> None:
        self._min_tasks_floor = max(1, int(min_tasks_floor or 1))
        self._min_tasks_per_req = max(1, int(min_tasks_per_req or 1))
        self._min_tasks_per_remaining = max(1, int(min_tasks_per_remaining or 1))

    def min_tasks_dynamic(self, analysis: Any) -> int:
        """
        Calculate minimal recommended task count from project analysis.
        Deterministic and config-independent by design.
        """
        if not analysis:
            return int(self._min_tasks_floor)

        if isinstance(analysis, dict):
            requirements_raw = analysis.get("requirements")
            remaining_raw = analysis.get("remaining_work")
            checklist_raw = analysis.get("checklist_table")
        else:
            requirements_raw = getattr(analysis, "requirements", None)
            remaining_raw = getattr(analysis, "remaining_work", None)
            checklist_raw = getattr(analysis, "checklist_table", None)

        requirements = requirements_raw if isinstance(requirements_raw, list) else []
        remaining_work = remaining_raw if isinstance(remaining_raw, list) else []
        checklist_table = checklist_raw if isinstance(checklist_raw, list) else []

        req_count = sum(1 for x in requirements if str(x or "").strip())
        remaining_count = sum(1 for x in remaining_work if str(x or "").strip())
        not_done_count = 0
        for row in checklist_table:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower()
            if status == "not_done":
                not_done_count += 1

        base = max(
            int(self._min_tasks_floor),
            int(req_count) * int(self._min_tasks_per_req),
            int(remaining_count) * int(self._min_tasks_per_remaining),
        )
        # Add one extra task per each three unresolved checklist items.
        return int(base + ((int(not_done_count) + 2) // 3))

    def parse_work_type_json(
        self,
        raw: str,
        *,
        allowed_work_types: Sequence[str],
        logger: Optional[logging.Logger] = None,
    ) -> Tuple[Optional[str], float, str]:
        source = str(raw or "").strip()
        if not source:
            return None, 0.0, ""
        try:
            payload = parse_normalize_validate(source, WORK_TYPE_CLASSIFIER_SCHEMA)
        except Exception as e:
            if logger:
                logger.warning("work_type: invalid classifier json: %s", e)
            return None, 0.0, ""

        wt = str(payload.get("work_type") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        try:
            conf = float(payload.get("confidence") or 0.0)
        except Exception as e:
            if logger:
                logger.warning("work_type: invalid confidence value: %s", e)
            conf = 0.0
        if wt not in list(allowed_work_types or []):
            return None, conf, reason
        return wt, conf, reason

    @staticmethod
    def build_dev_instruction(
        template: str,
        *,
        task_title: str,
        task_description: str,
        task_acceptance: str,
        rejection_block: str,
        partial_work_block: str,
        project_context: str,
        already_done: str,
        completed_tasks_summary: str,
    ) -> str:
        return ExecutionTrackingService._format_prompt_template(
            template,
            task_title=task_title,
            task_description=task_description,
            task_acceptance=task_acceptance,
            rejection_block=rejection_block,
            partial_work_block=partial_work_block,
            project_context=project_context,
            already_done=already_done,
            completed_tasks_summary=completed_tasks_summary,
        )

    @staticmethod
    def build_rework_instruction(
        template: str,
        *,
        task_title: str,
        task_description: str,
        dev_report: str,
        review_comments: str,
        rejection_history_block: str,
        task_acceptance: str,
        partial_work_block: str,
        project_context: str,
        already_done: str,
        completed_tasks_summary: str,
        attempt: int,
        max_attempts: int,
    ) -> str:
        return ExecutionTrackingService._format_prompt_template(
            template,
            task_title=task_title,
            task_description=task_description,
            dev_report=dev_report,
            review_comments=review_comments,
            rejection_history_block=rejection_history_block,
            task_acceptance=task_acceptance,
            partial_work_block=partial_work_block,
            project_context=project_context,
            already_done=already_done,
            completed_tasks_summary=completed_tasks_summary,
            attempt=attempt,
            max_attempts=max_attempts,
        )

    @staticmethod
    def build_review_instruction(
        template: str,
        *,
        task_title: str,
        task_description: str,
        task_acceptance: str,
        dev_report: str,
        last_commit_info: str,
    ) -> str:
        return ExecutionTrackingService._format_prompt_template(
            template,
            task_title=task_title,
            task_description=task_description,
            task_acceptance=task_acceptance,
            dev_report=dev_report,
            last_commit_info=last_commit_info,
        )

    @staticmethod
    def _format_prompt_template(template: str, /, **values: Any) -> str:
        """
        Safely replace known prompt placeholders while preserving literal JSON blocks.

        Project-level manager prompts are user-editable and often contain JSON examples.
        A raw `str.format(...)` treats snippets like `{"path": "file.py"}` as fields and
        crashes with `KeyError('"path"')`. This formatter only substitutes known
        placeholders and keeps non-placeholder brace blocks verbatim. Typos in expected
        placeholder names still fail fast.
        """
        rendered: List[str] = []
        for literal_text, field_name, format_spec, conversion in Formatter().parse(str(template or "")):
            rendered.append(literal_text)
            if field_name is None:
                continue
            if field_name in values:
                rendered.append(
                    ExecutionTrackingService._format_prompt_value(
                        values[field_name],
                        format_spec=format_spec,
                        conversion=conversion,
                    )
                )
                continue
            if _PROMPT_IDENTIFIER_RE.fullmatch(str(field_name or "")):
                raise KeyError(field_name)
            rendered.append(
                ExecutionTrackingService._rebuild_literal_field(
                    field_name=field_name,
                    format_spec=format_spec,
                    conversion=conversion,
                )
            )
        return "".join(rendered)

    @staticmethod
    def _format_prompt_value(value: Any, *, format_spec: str, conversion: Optional[str]) -> str:
        if conversion == "r":
            normalized = repr(value)
        elif conversion in (None, "s"):
            normalized = value if conversion is None else str(value)
        else:
            raise ValueError(f"unsupported format conversion: {conversion!r}")
        if format_spec:
            return format(normalized, format_spec)
        return str(normalized)

    @staticmethod
    def _rebuild_literal_field(
        *,
        field_name: Optional[str],
        format_spec: Optional[str],
        conversion: Optional[str],
    ) -> str:
        field = "{" + str(field_name or "")
        if conversion:
            field += f"!{conversion}"
        if format_spec:
            field += f":{format_spec}"
        field += "}"
        return field

    @staticmethod
    def build_review_executor_request(
        *,
        task: DevTask,
        instruction: str,
        workdir: str,
        allowed_tools: Optional[List[str]],
        deadline_ms: Optional[int],
    ) -> ExecutorRequest:
        return ExecutorRequest(
            task_id=f"review:{task.id}",
            goal=instruction,
            context=f"workdir={workdir}",
            inputs={},
            allowed_tools=allowed_tools,
            deadline_ms=deadline_ms,
        )

    @staticmethod
    def extract_executor_primary_text(response: Any) -> str:
        payload: Dict[str, Any] = {
            "summary": str(getattr(response, "summary", "") or ""),
            "outputs": getattr(response, "outputs", []),
        }
        validate_payload(payload, EXECUTOR_RESPONSE_OUTPUT_SCHEMA, context="executor_response_output")

        outputs = payload.get("outputs") or []
        if isinstance(outputs, list) and outputs:
            first = outputs[0]
            if isinstance(first, dict) and "content" in first:
                return str(first.get("content") or "")
        return str(payload.get("summary") or "")

    @staticmethod
    def parse_review_result(
        text: str,
        *,
        logger: Optional[logging.Logger] = None,
        allow_action_payload_fallback: bool = True,
    ) -> Optional[ReviewResult]:
        try:
            payload = parse_normalize_validate(
                text,
                REVIEW_RESULT_SCHEMA,
                log_validation_errors=False,
            )
        except Exception as e:
            try:
                raw_payload = loads_safe(text, strict_first=False)
            except Exception:
                if logger:
                    logger.warning("try_parse_review: parse failed: %s", e)
                return None
            if not isinstance(raw_payload, dict):
                if logger:
                    logger.warning("try_parse_review: parse failed: %s", e)
                return None
            if not ExecutionTrackingService._should_coerce_review_result_fallback(raw_payload):
                if logger:
                    logger.warning("try_parse_review: parse failed: %s", e)
                return None
            if not allow_action_payload_fallback:
                if logger:
                    logger.warning(
                        "try_parse_review: rejecting action payload during strict parse keys=%s",
                        sorted(str(key).strip() for key in raw_payload.keys() if str(key).strip()),
                    )
                return None
            if logger:
                logger.warning(
                    "try_parse_review: coercing invalid action payload to rejected review_result keys=%s",
                    sorted(str(key).strip() for key in raw_payload.keys() if str(key).strip()),
                )
            return ExecutionTrackingService._coerce_review_result_fallback(raw_payload)

        raw_not_done = payload.get("not_done_assessment")
        not_done_assessment: List[Dict[str, str]] = []
        if isinstance(raw_not_done, list):
            for item in raw_not_done:
                if not isinstance(item, dict):
                    continue
                not_done_assessment.append(
                    {
                        "item": str(item.get("item") or "").strip(),
                        "why_not": str(item.get("why_not") or "").strip(),
                        "verdict": str(item.get("verdict") or "").strip(),
                        "comment": str(item.get("comment") or "").strip(),
                    }
                )
        return ReviewResult(
            approved=bool(payload.get("approved")),
            summary=str(payload.get("summary") or ""),
            comments=str(payload.get("comments") or ""),
            tests_passed=payload.get("tests_passed") if isinstance(payload.get("tests_passed"), bool) else None,
            files_reviewed=list(payload.get("files_reviewed") or []),
            not_done_assessment=not_done_assessment,
        )

    @staticmethod
    def _coerce_review_result_fallback(raw_payload: Dict[str, Any]) -> ReviewResult:
        summary = str(raw_payload.get("summary") or "").strip() or "Невалидный формат ответа ревьюера"
        detail_parts: List[str] = [
            "Ожидался JSON ReviewResult с обязательными полями approved/summary/comments.",
        ]
        keys = sorted(str(k).strip() for k in raw_payload.keys() if str(k).strip())
        if keys:
            detail_parts.append(f"Получены ключи: {', '.join(keys)}.")
        command = str(raw_payload.get("command") or "").strip()
        if command:
            detail_parts.append(f"Вместо финального вердикта пришла команда: {command}")
        pattern = str(raw_payload.get("pattern") or "").strip()
        path = str(raw_payload.get("path") or "").strip()
        if pattern or path:
            grep_comment = "Вместо финального вердикта пришёл служебный объект"
            if pattern:
                grep_comment += f" pattern={pattern!r}"
            if path:
                grep_comment += f" path={path!r}"
            detail_parts.append(grep_comment + ".")
        raw_comments = str(raw_payload.get("comments") or "").strip()
        if raw_comments:
            detail_parts.append(f"Исходный comments: {raw_comments}")
        detail_parts.append("Такой ответ трактуется как rejected; проверь prompt/normalizer ревью.")
        fallback_comment = " ".join(part for part in detail_parts if part).strip()
        return ReviewResult(
            approved=False,
            summary=summary,
            comments=fallback_comment,
            tests_passed=raw_payload.get("tests_passed") if isinstance(raw_payload.get("tests_passed"), bool) else None,
            files_reviewed=[
                str(item).strip()
                for item in (raw_payload.get("files_reviewed") or [])
                if str(item).strip()
            ],
            not_done_assessment=[
                {
                    "item": "review_result_schema",
                    "why_not": "missing required fields approved/summary/comments",
                    "verdict": "not_justified",
                    "comment": fallback_comment,
                }
            ],
        )

    @staticmethod
    def _should_coerce_review_result_fallback(raw_payload: Dict[str, Any]) -> bool:
        required_keys = {"approved", "summary", "comments"}
        payload_keys = {str(key).strip() for key in raw_payload.keys() if str(key).strip()}
        if required_keys.issubset(payload_keys):
            return False
        action_keys = {"command", "pattern", "path"}
        return bool(payload_keys & action_keys)


__all__ = ["ExecutionTrackingService"]
