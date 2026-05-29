from __future__ import annotations

import logging
import re
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from app.services.cli_dialog_logger import log_cli_dialog
from app.services.cli_json_stream import extract_cli_evidence_from_normalized_stream
from app.services.task_bearing_cli_hook_service import get_task_bearing_cli_hook_service
from modes.sdk.runtime.cli_contracts import (
    CLIResponseFormat,
    parse_bundle_for_response_format,
    degraded_mode_output,
    repo_review_bundle_to_outputs,
    retry_notice_output,
    wrap_prompt_for_response_format,
)
from modes.sdk.runtime.cli_retry import run_cli_with_retry
from utils.text import strip_ansi
from agent.cli_routing import run_prompt_routed


class UseCliTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="use_cli",
            description=(
                "Delegate a complex task to the selected CLI (codex/gemini/claude code). "
                "Use when the task is too complex for tools or requires full coding workflow."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_text": {"type": "string", "description": "Task description for CLI"},
                    "fresh_run": {
                        "type": "boolean",
                        "description": "Run without resume context (force a fresh CLI run)",
                    },
                    "response_format": {
                        "type": "string",
                        "description": "Optional structured response contract name",
                    },
                },
                "required": ["task_text"],
            },
            parallelizable=False,
            timeout_ms=3_600_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        task_text = (args.get("task_text") or "").strip()
        fresh_run = bool(args.get("fresh_run", False))
        response_format = str(args.get("response_format") or "").strip().lower()
        if not task_text:
            return {"success": False, "error": "task_text required"}
        session = ctx.get("session")
        if not session:
            return {"success": False, "error": "CLI session not available"}

        def _extract_claim_texts(output: str) -> list[str]:
            raw = str(output or "").strip()
            if not raw:
                return []
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            bullet_lines = [
                line for line in lines
                if line.startswith(("- ", "* ", "• ")) or re.match(r"^\d+[.)]\s+", line)
            ]
            candidates: list[str] = []
            if len(bullet_lines) >= 2:
                for line in bullet_lines[:8]:
                    item = re.sub(r"^(\d+[.)]\s+|[-*•]\s+)", "", line).strip()
                    if item:
                        candidates.append(item)
            else:
                for segment in re.split(r"(?<=[.!?;])\s+|\n+", raw):
                    item = " ".join(str(segment or "").split()).strip(" -\t\r\n")
                    if item and len(item) >= 12:
                        candidates.append(item)
            deduped: list[str] = []
            seen: set[str] = set()
            for item in candidates:
                key = item.lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            return deduped[:8]

        def _collect_cli_outputs(output: str, *, bundle: Dict[str, Any] | None = None) -> list[Dict[str, Any]]:
            outputs: list[Dict[str, Any]] = [{"type": "text", "content": output}]
            normalized_path = str(getattr(session, "last_cli_normalized_stream_path", "") or "").strip()
            if normalized_path:
                outputs.append(
                    {
                        "type": "file",
                        "path": normalized_path,
                        "name": "cli normalized stream",
                    }
                )
                for item in extract_cli_evidence_from_normalized_stream(normalized_path):
                    if isinstance(item, dict):
                        outputs.append(dict(item))
            if isinstance(bundle, dict):
                verdict = str(bundle.get("verdict") or "").strip()
                if verdict:
                    outputs.extend(repo_review_bundle_to_outputs(bundle))
                else:
                    for item in bundle.get("evidence") or []:
                        if not isinstance(item, dict):
                            continue
                        outputs.append(
                            {
                                "type": str(item.get("type") or "repo_evidence").strip() or "repo_evidence",
                                "path": str(item.get("path") or "").strip(),
                                "content_preview": str(item.get("preview") or "").strip(),
                            }
                        )
                    for gap in bundle.get("open_gaps") or []:
                        gap_text = str(gap or "").strip()
                        if gap_text:
                            outputs.append({"type": "open_gap", "content": gap_text, "content_preview": gap_text})
            return outputs

        def _build_cli_claims(output: str, outputs: list[Dict[str, Any]], *, bundle: Dict[str, Any] | None = None) -> list[Dict[str, Any]]:
            if isinstance(bundle, dict) and isinstance(bundle.get("claims"), list) and bundle.get("claims"):
                return list(bundle.get("claims") or [])
            evidence: list[Dict[str, Any]] = []
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or item.get("file_path") or "").strip()
                preview = str(item.get("content_preview") or item.get("content") or "").strip()
                if not path and not preview:
                    continue
                evidence.append(
                    {
                        "type": str(item.get("type") or "text").strip() or "text",
                        "path": path,
                        "preview": preview,
                    }
                )
            texts = _extract_claim_texts(output)
            if not texts:
                return []
            return [
                {
                    "claim_id": f"claim_{idx}",
                    "status": "confirmed",
                    "text": text,
                    "evidence": evidence[:8],
                }
                for idx, text in enumerate(texts, start=1)
            ]

        try:
            config = getattr(session, "config", None)
            chat_id = ctx.get("chat_id")
            prompt_for_cli = wrap_prompt_for_response_format(task_text, response_format)
            cli_work_type = str(
                getattr(getattr(session, "cli", None), "cli_work_type", getattr(session, "cli_work_type", ""))
                or ""
            ).strip()

            # Derive per-CLI timeout from tool-level timeout so that a single
            # hanging CLI cannot consume the entire budget and prevent failover.
            tool_timeouts_ms = ctx.get("tool_timeouts_ms")
            per_cli_timeout_sec: int | None = None
            if isinstance(tool_timeouts_ms, dict):
                total_ms = tool_timeouts_ms.get("use_cli")
                if isinstance(total_ms, (int, float)) and total_ms > 0:
                    per_cli_timeout_sec = max(60, int(total_ms / 1000 / 4))

            async def _invoke_cli() -> str:
                if config is not None and cli_work_type:
                    return await run_prompt_routed(
                        session,
                        config,
                        cli_work_type,
                        prompt_for_cli,
                        force_fresh=fresh_run,
                        chat_id=chat_id,
                        timeout_sec=per_cli_timeout_sec,
                    )
                hook_config = config or getattr(session, "config", None)
                if hook_config is None:
                    return await session.run_prompt(prompt_for_cli, force_fresh=fresh_run)
                hook = get_task_bearing_cli_hook_service(hook_config)
                prepared = await hook.prepare_prompt(
                    session=session,
                    prompt=prompt_for_cli,
                    source="use_cli_plugin",
                    phase="execute",
                    task_bearing=True,
                )
                try:
                    output_local = await session.run_prompt(prepared.prompt_for_cli, force_fresh=fresh_run)
                except Exception as exc:
                    hook.record_error(prepared, error=exc)
                    raise
                hook.record_success(prepared, output=output_local)
                return output_local

            # When using routed path, failover between CLIs already provides
            # retry semantics — skip run_cli_with_retry to avoid up to 8
            # sequential CLI launches (4 candidates × 2 retries).
            use_routed = config is not None and bool(cli_work_type)
            retry_info = await run_cli_with_retry(_invoke_cli, max_attempts=1 if use_routed else 2)
            output = str(retry_info.get("output") or "")
            if retry_info.get("retried"):
                logging.warning(
                    "use_cli retried cli call after transient failure reason=%s attempts=%s",
                    retry_info.get("retry_reason") or "",
                    retry_info.get("attempts"),
                )
            if config is None or not cli_work_type:
                hook_config = config or getattr(session, "config", None)
                if hook_config is None:
                    log_cli_dialog(session, task_text, output, chat_id=chat_id)
                    output = strip_ansi(output)
                    bundle = parse_bundle_for_response_format(output, response_format)
                    structured_fallback = (
                        response_format in {CLIResponseFormat.CLAIM_BUNDLE_JSON, CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON}
                        and bundle is None
                    )
                    if structured_fallback:
                        logging.warning("use_cli %s parse failed or schema invalid; falling back to text output", response_format)
                    final_text = str((bundle or {}).get("final_text") or "").strip() or output
                    outputs = _collect_cli_outputs(final_text, bundle=bundle)
                    if structured_fallback:
                        outputs.append(
                            degraded_mode_output(
                                f"use_cli response_format={response_format} invalid_bundle_fallback_to_text"
                            )
                        )
                    if retry_info.get("retried"):
                        outputs.append(
                            retry_notice_output(
                                f"use_cli transient retry succeeded after {retry_info.get('attempts')} attempts"
                            )
                        )
                    if retry_info.get("retry_exhausted"):
                        outputs.append(
                            degraded_mode_output(
                                f"use_cli retry_exhausted reason={retry_info.get('retry_reason') or 'retryable_output'}"
                            )
                        )
                    return {
                        "success": True,
                        "output": final_text,
                        "outputs": outputs,
                        "claims": [] if structured_fallback else _build_cli_claims(final_text, outputs, bundle=bundle),
                        "open_gaps": list((bundle or {}).get("open_gaps") or []),
                    }
                log_cli_dialog(session, task_text, output, chat_id=chat_id)
            output = strip_ansi(output)
            bundle = parse_bundle_for_response_format(output, response_format)
            structured_fallback = (
                response_format in {CLIResponseFormat.CLAIM_BUNDLE_JSON, CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON}
                and bundle is None
            )
            if structured_fallback:
                logging.warning("use_cli %s parse failed or schema invalid; falling back to text output", response_format)
            final_text = str((bundle or {}).get("final_text") or "").strip() or output
            outputs = _collect_cli_outputs(final_text, bundle=bundle)
            if structured_fallback:
                outputs.append(
                    degraded_mode_output(
                        f"use_cli response_format={response_format} invalid_bundle_fallback_to_text"
                    )
                )
            if retry_info.get("retried"):
                outputs.append(
                    retry_notice_output(
                        f"use_cli transient retry succeeded after {retry_info.get('attempts')} attempts"
                    )
                )
            if retry_info.get("retry_exhausted"):
                outputs.append(
                    degraded_mode_output(
                        f"use_cli retry_exhausted reason={retry_info.get('retry_reason') or 'retryable_output'}"
                    )
                )
            return {
                "success": True,
                "output": final_text,
                "outputs": outputs,
                "claims": [] if structured_fallback else _build_cli_claims(final_text, outputs, bundle=bundle),
                "open_gaps": list((bundle or {}).get("open_gaps") or []),
            }
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
