from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.run_artifact_store import RunArtifactStore
from modes.analyst.run_directory import AnalystRunDirectory, resolve_analyst_runs_root
from modes.analyst.template_service import get_template_for_session
from modes.sdk.json_store import read_json_locked
from modes.sdk.runtime.memory_store import ensure_chat_workspace
from modes.sdk.runtime.openai_client import chat_completion
from utils.text import strip_ansi

_VALID_DOCUMENT_KINDS = {"analysis", "spec", "audit"}
_SPEC_TEMPLATE_IDS = {
    "new_spec",
    "change_spec",
    "ui_change_spec",
    "integration_change_spec",
    "narrow_backend_change_spec",
}
_DRAFT_DOCUMENT_PROFILES: dict[str, dict[str, str]] = {
    "spec": {
        "kind": "spec",
        "title": "Черновик ТЗ",
        "document_name": "техническое задание",
        "menu_label": "📥 Скачать черновик ТЗ",
        "missing_text": "Черновик ТЗ пока не сформирован.",
        "sent_text": "Черновик ТЗ отправлен файлом.",
        "failed_text": "Не удалось отправить черновик ТЗ.",
        "file_prefix": "analyst-spec-",
    },
    "analysis": {
        "kind": "analysis",
        "title": "Черновик аналитической записки",
        "document_name": "аналитическая записка",
        "menu_label": "📥 Скачать черновик анализа",
        "missing_text": "Черновик аналитической записки пока не сформирован.",
        "sent_text": "Черновик аналитической записки отправлен файлом.",
        "failed_text": "Не удалось отправить черновик аналитической записки.",
        "file_prefix": "analyst-analysis-",
    },
    "audit": {
        "kind": "audit",
        "title": "Черновик отчета по аудиту",
        "document_name": "отчет по аудиту",
        "menu_label": "📥 Скачать черновик аудита",
        "missing_text": "Черновик отчета по аудиту пока не сформирован.",
        "sent_text": "Черновик отчета по аудиту отправлен файлом.",
        "failed_text": "Не удалось отправить черновик отчета по аудиту.",
        "file_prefix": "analyst-audit-",
    },
    "generic": {
        "kind": "generic",
        "title": "Черновик документа",
        "document_name": "документ",
        "menu_label": "📥 Скачать черновик документа",
        "missing_text": "Черновик документа пока не сформирован.",
        "sent_text": "Черновик документа отправлен файлом.",
        "failed_text": "Не удалось отправить черновик документа.",
        "file_prefix": "analyst-draft-",
    },
}


def _load_existing_text(path: str) -> str:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
    except Exception:
        return ""
    return ""


def _build_draft_llm_materials(
    *,
    artifact_dir: str,
    session_id: str,
    last_entry: dict[str, Any],
) -> dict[str, Any]:
    step_results = last_entry.get("step_results") or []
    if not isinstance(step_results, list):
        step_results = []

    fact_pack_path = os.path.join(artifact_dir, f"{session_id}_fact_pack.md")
    claim_ledger_path = os.path.join(artifact_dir, f"{session_id}_claim_ledger.json")

    compact_steps = []
    step_artifacts: list[str] = []
    for item in step_results[-25:]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            "task_id": str(item.get("task_id") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "status": str(item.get("status") or "").strip(),
            "summary": str(item.get("summary") or "").strip(),
        }
        step_artifact = str(item.get("orchestrator_artifact") or "").strip()
        if step_artifact:
            compact_item["step_artifact"] = step_artifact
            step_artifacts.append(step_artifact)
        outputs = item.get("outputs") or []
        compact_outputs = []
        if isinstance(outputs, list):
            for output in outputs[:8]:
                if not isinstance(output, dict):
                    continue
                compact_output = {
                    "type": str(output.get("type") or "").strip(),
                    "path": str(output.get("path") or output.get("file_path") or "").strip(),
                    "content_len": int(output.get("content_len") or 0),
                    "content_spilled": bool(output.get("content_spilled")),
                    "content_preview": str(output.get("content_preview") or "").strip(),
                }
                compact_outputs.append(compact_output)
                if compact_output["path"]:
                    step_artifacts.append(compact_output["path"])
        if compact_outputs:
            compact_item["outputs"] = compact_outputs
        compact_steps.append(compact_item)

    deduped_artifacts: list[str] = []
    seen_paths: set[str] = set()
    for path in [fact_pack_path, claim_ledger_path, *step_artifacts]:
        clean = str(path or "").strip()
        if not clean or clean in seen_paths or not os.path.exists(clean):
            continue
        seen_paths.add(clean)
        deduped_artifacts.append(clean)

    payload: dict[str, Any] = {
        "session_id": session_id,
        "user_query": str(last_entry.get("user") or "").strip(),
        "step_results": compact_steps,
        "artifact_paths": deduped_artifacts,
    }
    fact_pack_text = _load_existing_text(fact_pack_path)
    if fact_pack_text:
        payload["fact_pack_text"] = fact_pack_text
    return payload


def _build_draft_llm_materials_from_run_dir(*, session: Any) -> dict[str, Any]:
    latest_run = AnalystRunDirectory.latest_run(
        resolve_analyst_runs_root(session),
        session_id=str(getattr(session, "id", "") or ""),
    )
    if latest_run is None:
        return {}
    meta = latest_run.load_meta()
    steps = meta.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return {}

    compact_steps = []
    artifact_paths: list[str] = []
    for item in steps[-25:]:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or "").strip()
        compact_item = {
            "task_id": step_id,
            "title": str(item.get("title") or "").strip(),
            "status": str(item.get("status") or "").strip(),
            "summary": str(item.get("summary") or "").strip(),
        }
        artifact_rel = str(item.get("artifact") or "").strip()
        if artifact_rel:
            artifact_path = os.path.join(latest_run.run_path, artifact_rel)
            compact_item["step_artifact"] = artifact_path
            artifact_paths.append(artifact_path)
        compact_steps.append(compact_item)

    deduped_artifacts: list[str] = []
    seen_paths: set[str] = set()
    for path in artifact_paths:
        clean = str(path or "").strip()
        if not clean or clean in seen_paths or not os.path.exists(clean):
            continue
        seen_paths.add(clean)
        deduped_artifacts.append(clean)

    payload: dict[str, Any] = {
        "session_id": str(getattr(session, "id", "") or ""),
        "user_query": str(meta.get("user_request") or "").strip(),
        "step_results": compact_steps,
        "artifact_paths": deduped_artifacts,
        "run_id": latest_run.run_id,
    }
    evidence_trail = meta.get("evidence_trail")
    if isinstance(evidence_trail, dict) and evidence_trail:
        payload["evidence_trail"] = evidence_trail
    return payload


def _resolve_analyst_artifacts_dir(*, bot_app: Any, session: Any, cwd: str) -> str:
    handle = getattr(session, "analyst_run_artifact_handle", None)
    artifacts_dir = str(getattr(handle, "artifacts_dir", "") or "").strip()
    if artifacts_dir:
        return artifacts_dir

    config = getattr(bot_app, "config", None)
    if config is not None:
        try:
            latest = RunArtifactStore(config).latest_run(session=session, mode_id="analyst")
        except Exception:
            latest = None
        artifacts_dir = str(getattr(latest, "artifacts_dir", "") or "").strip()
        if artifacts_dir:
            return artifacts_dir

    return os.path.join(cwd, "_orchestrator")


def _normalize_document_kind(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _VALID_DOCUMENT_KINDS else ""


def _normalize_template_id(value: Any) -> str:
    return str(value or "").strip().lower()


def _extract_document_kind_from_session(session: Any) -> str:
    if session is None:
        return ""
    session_flags = getattr(session, "analyst_intent_flags", None)
    if isinstance(session_flags, dict):
        normalized = _normalize_document_kind(session_flags.get("document_kind"))
        if normalized:
            return normalized
    return _normalize_document_kind(getattr(session, "document_kind", ""))


def _extract_template_id_hints(*, session: Any, analyst_context: Any) -> list[str]:
    hints: list[str] = []
    if analyst_context is not None:
        hints.extend(
            [
                _normalize_template_id(getattr(analyst_context, "effective_template_id", "")),
                _normalize_template_id(getattr(analyst_context, "runtime_template_id", "")),
            ]
        )
    if session is not None:
        hints.extend(
            [
                _normalize_template_id(getattr(getattr(session, "modes", None), "analyst_template_id", "")),
                _normalize_template_id(getattr(session, "analyst_template_id", "")),
            ]
        )
    return [hint for hint in hints if hint]


def _extract_mode_hints(*, session: Any, analyst_context: Any) -> list[str]:
    hints: list[str] = []
    if analyst_context is not None:
        hints.extend(
            [
                str(getattr(analyst_context, "active_flow", "") or "").strip().lower(),
                str(getattr(analyst_context, "mode", "") or "").strip().lower(),
            ]
        )
    if session is not None:
        hints.extend(
            [
                str(getattr(getattr(session, "modes", None), "analyst_mode", "") or "").strip().lower(),
                str(getattr(session, "analyst_mode", "") or "").strip().lower(),
            ]
        )
    return [hint for hint in hints if hint]


def resolve_draft_document_kind(
    *,
    session: Any,
    analyst_context: Any = None,
    template_override: Any = None,
) -> str:
    for candidate in (
        _normalize_document_kind(getattr(analyst_context, "document_kind", "")),
        _extract_document_kind_from_session(session),
    ):
        if candidate:
            return candidate

    template_output_kind = ""
    if isinstance(template_override, dict):
        template_output_kind = _normalize_document_kind(template_override.get("output_kind"))
    if template_output_kind:
        return template_output_kind

    for template_id in _extract_template_id_hints(session=session, analyst_context=analyst_context):
        if template_id == "audit":
            return "audit"
        if template_id in _SPEC_TEMPLATE_IDS:
            return "spec"

    for mode_hint in _extract_mode_hints(session=session, analyst_context=analyst_context):
        if mode_hint == "audit":
            return "audit"
        if mode_hint == "analysis":
            return "analysis"
        if mode_hint == "spec":
            return "spec"

    return ""


def resolve_draft_document_profile(
    *,
    session: Any,
    analyst_context: Any = None,
    template_override: Any = None,
) -> dict[str, str]:
    kind = resolve_draft_document_kind(
        session=session,
        analyst_context=analyst_context,
        template_override=template_override,
    )
    profile_key = kind if kind in _DRAFT_DOCUMENT_PROFILES else "generic"
    return dict(_DRAFT_DOCUMENT_PROFILES[profile_key])


def _render_draft_wrapper(*, session: Any, body_text: str, profile: dict[str, str]) -> str:
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    clean_body = strip_ansi(body_text).strip()
    return (
        f"# {profile['title']}\n\n"
        f"Сессия: {session.id}\n"
        f"Тип документа: {profile['document_name']}\n"
        f"Сформировано: {generated_at}\n\n"
        f"{clean_body}\n"
    )


async def build_draft_text(
    bot_app: Any,
    session: Any,
    *,
    chat_id: int,
    analyst_context: Any = None,
    template_override: Any = None,
    chat_completion_fn: Any = None,
) -> str:
    """Build human-friendly analyst draft text for download actions."""
    profile = resolve_draft_document_profile(
        session=session,
        analyst_context=analyst_context,
        template_override=template_override,
    )
    text = ""
    cwd = ""
    context_draft = str(getattr(analyst_context, "last_draft", "") or "").strip()
    if context_draft:
        return _render_draft_wrapper(session=session, body_text=context_draft, profile=profile)
    last = None
    try:
        cwd = ensure_chat_workspace(bot_app.config.defaults.workdir, int(chat_id))
        path = os.path.join(cwd, "SESSION.json")
        data = read_json_locked(path, default={"orchestrator_by_task": {}})
        if not isinstance(data, dict):
            data = {"orchestrator_by_task": {}}
        items = ((data.get("orchestrator_by_task") or {}).get(session.id) or [])
        if items and isinstance(items[-1], dict):
            last = items[-1]
            text = str(last.get("final") or "").strip()
    except Exception:
        last = None
        text = ""

    # If final is missing but we have step materials, try to assemble a structured draft.
    if not text:
        step_results = last.get("step_results") if isinstance(last, dict) else []
        if not isinstance(step_results, list):
            step_results = []

        template = template_override if isinstance(template_override, dict) else get_template_for_session(session)
        required_sections = []
        if isinstance(template, dict):
            required_sections = list(template.get("required_sections") or [])
        required_sections = [str(x).strip() for x in required_sections if str(x).strip()]

        session_payload: dict[str, Any] = {}
        if isinstance(last, dict):
            artifacts_dir = _resolve_analyst_artifacts_dir(
                bot_app=bot_app,
                session=session,
                cwd=cwd,
            )
            session_payload = _build_draft_llm_materials(
                artifact_dir=artifacts_dir,
                session_id=str(getattr(session, "id", "") or ""),
                last_entry=last,
            )
        run_dir_payload = _build_draft_llm_materials_from_run_dir(session=session)
        payload = dict(session_payload)
        if run_dir_payload:
            payload.update(
                {
                    key: value
                    for key, value in run_dir_payload.items()
                    if key in {"session_id", "user_query", "step_results", "evidence_trail", "run_id"} and value
                }
            )
            merged_paths: list[str] = []
            seen_paths: set[str] = set()
            for path in [*list(run_dir_payload.get("artifact_paths") or []), *list(session_payload.get("artifact_paths") or [])]:
                clean = str(path or "").strip()
                if not clean or clean in seen_paths:
                    continue
                seen_paths.add(clean)
                merged_paths.append(clean)
            if merged_paths:
                payload["artifact_paths"] = merged_paths

        has_materials = bool(
            payload.get("step_results") or payload.get("artifact_paths") or payload.get("fact_pack_text")
        )
        if has_materials:
            parts = [
                str(item.get("summary") or "").strip()
                for item in list(payload.get("step_results") or [])
                if isinstance(item, dict) and str(item.get("summary") or "").strip()
            ]
            fallback_text = "\n\n".join(parts).strip()

            if bot_app.config.defaults.openai_api_key and bot_app.config.defaults.openai_model:
                raw = json.dumps(payload, ensure_ascii=False)
                if len(raw) > 120_000:
                    payload = {
                        "session_id": str(getattr(session, "id", "") or ""),
                        "user_query": str(payload.get("user_query") or "").strip(),
                        "artifact_paths": list(payload.get("artifact_paths") or []),
                        "fact_pack_text": str(payload.get("fact_pack_text") or "").strip(),
                        "step_summaries": [
                            {
                                "task_id": str(item.get("task_id") or "").strip(),
                                "status": str(item.get("status") or "").strip(),
                                "summary": str(item.get("summary") or "").strip(),
                                "step_artifact": str(item.get("step_artifact") or "").strip(),
                            }
                            for item in list(payload.get("step_results") or [])[:25]
                            if isinstance(item, dict)
                        ],
                        "degraded_mode": "artifact_bundle_compacted_for_draft_llm",
                    }
                    raw = json.dumps(payload, ensure_ascii=False)
                req = "\n".join(f"- {x}" for x in required_sections) if required_sections else "- (не заданы)"
                system = (
                    "Ты помощник-аналитик. На основе материалов собери предварительную структуру документа.\n"
                    "Правила:\n"
                    "- Формат: Markdown.\n"
                    "- Следуй списку обязательных секций.\n"
                    "- Недостающие секции помечай как: [В процессе].\n"
                    "- Не выдумывай факты: если данных нет, помечай как [В процессе] или [Нужно уточнить].\n"
                    "- Не описывай внутренние логи/инструменты, дай только текст черновика.\n"
                )
                user = f"Обязательные секции:\n{req}\n\nМатериалы (JSON):\n{raw}"
                completion_fn = chat_completion_fn or chat_completion
                try:
                    out = await completion_fn(bot_app.config, system, user)
                    text = (out or "").strip() or fallback_text
                except Exception:
                    text = fallback_text
            else:
                text = fallback_text

    if not text:
        text = str(getattr(session, "state_summary", "") or "").strip()

    if not text:
        return ""

    return _render_draft_wrapper(session=session, body_text=text, profile=profile)
