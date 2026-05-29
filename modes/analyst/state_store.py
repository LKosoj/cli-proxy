from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict

from modes.sdk.json_store import read_json_locked, write_json_locked
from utils.paths import cli_proxy_artifact_path


def _sanitize_context_token(value: Any) -> str:
    token = str(value or "").strip()
    return token.replace("/", "_").replace("\\", "_").strip()


def _sanitize_context_key(value: Any) -> str:
    return _sanitize_context_token(value) or "default"


def build_context_key(chat_id: Any, session_id: Any) -> str:
    cid = _sanitize_context_token(chat_id)
    sid = _sanitize_context_token(session_id) or "default"
    if not cid:
        return sid
    return f"{cid}_{sid}"


def resolve_analyst_state_root(session: Any) -> str:
    workdir = str(getattr(session, "workdir", "") or "").strip()
    if workdir:
        return cli_proxy_artifact_path(workdir, ".analyst_data")
    return cli_proxy_artifact_path(os.getcwd(), ".analyst_data")


@dataclass
class AnalystContext:
    key: str
    mode: str = "spec"
    active_flow: str = ""
    runtime_template_id: str = ""
    effective_template_id: str = ""
    intent_reason: str = ""
    detail_level: str = ""
    document_kind: str = ""
    requires_codebase_grounding: bool = False
    requires_repo_audit: bool = False
    requires_final_repo_review: bool = False
    needs_clarification: bool = False
    clarification_is_blocking: bool = False
    clarification_topic: str = ""
    source_user_text: str = ""
    clarification_answers: list[str] = field(default_factory=list)
    last_draft: str = ""
    last_draft_updated_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any], key: str) -> "AnalystContext":
        raw = data if isinstance(data, dict) else {}
        return cls(
            key=key,
            mode=str(raw.get("mode", "spec") or "spec"),
            active_flow=str(raw.get("active_flow", "") or ""),
            runtime_template_id=str(raw.get("runtime_template_id", "") or ""),
            effective_template_id=str(raw.get("effective_template_id", "") or ""),
            intent_reason=str(raw.get("intent_reason", "") or ""),
            detail_level=str(raw.get("detail_level", "") or ""),
            document_kind=str(raw.get("document_kind", "") or ""),
            requires_codebase_grounding=bool(raw.get("requires_codebase_grounding", False)),
            requires_repo_audit=bool(raw.get("requires_repo_audit", False)),
            requires_final_repo_review=bool(raw.get("requires_final_repo_review", False)),
            needs_clarification=bool(raw.get("needs_clarification", False)),
            clarification_is_blocking=bool(raw.get("clarification_is_blocking", False)),
            clarification_topic=str(raw.get("clarification_topic", "") or ""),
            source_user_text=str(raw.get("source_user_text", "") or ""),
            clarification_answers=[
                str(item).strip()
                for item in (raw.get("clarification_answers") or [])
                if str(item).strip()
            ],
            last_draft=str(raw.get("last_draft", "") or ""),
            last_draft_updated_at=float(raw.get("last_draft_updated_at", 0.0) or 0.0),
            updated_at=float(raw.get("updated_at", 0.0) or 0.0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "active_flow": self.active_flow,
            "runtime_template_id": self.runtime_template_id,
            "effective_template_id": self.effective_template_id,
            "intent_reason": self.intent_reason,
            "detail_level": self.detail_level,
            "document_kind": self.document_kind,
            "requires_codebase_grounding": self.requires_codebase_grounding,
            "requires_repo_audit": self.requires_repo_audit,
            "requires_final_repo_review": self.requires_final_repo_review,
            "needs_clarification": self.needs_clarification,
            "clarification_is_blocking": self.clarification_is_blocking,
            "clarification_topic": self.clarification_topic,
            "source_user_text": self.source_user_text,
            "clarification_answers": [
                str(item).strip()
                for item in (self.clarification_answers or [])
                if str(item).strip()
            ],
            "last_draft": self.last_draft,
            "last_draft_updated_at": self.last_draft_updated_at,
            "updated_at": self.updated_at,
        }


class AnalystStateStore:
    def __init__(self, state_root: str) -> None:
        root = os.path.abspath(str(state_root or "."))
        self.root_dir = root
        self.contexts_dir = os.path.join(root, "contexts")
        os.makedirs(self.contexts_dir, exist_ok=True)

    def path_for(self, key: str) -> str:
        safe_key = _sanitize_context_key(key)
        return os.path.join(self.contexts_dir, f"{safe_key}.json")

    def load(self, key: str) -> AnalystContext:
        context_key = _sanitize_context_key(key)
        primary_path = self.path_for(context_key)

        raw = read_json_locked(primary_path, default={})
        if not isinstance(raw, dict):
            raw = {}
        return AnalystContext.from_dict(raw, context_key)

    def save(self, ctx: AnalystContext) -> None:
        ctx.updated_at = time.time()
        write_json_locked(self.path_for(ctx.key), ctx.to_dict())
