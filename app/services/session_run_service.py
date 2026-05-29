from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from app.services.run_artifact_store import RunArtifactStore, is_terminal_status
from modes.analyst.state_store import AnalystStateStore, build_context_key
from session import session_runtime_uid, session_scoped_key
from sessions.session_state_access import set_analyst_mode
from utils.paths import cli_proxy_artifact_path


class ModeScopedPreRunResetService:
    """Applies a minimal pre-run reset for modes that require it."""

    ANALYST_MODE_ID = "analyst"

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def apply(
        self,
        *,
        session: Any,
        mode_id: Optional[str],
        clear_runtime_cache: Callable[[str], None],
        clear_pending_questions: Callable[[str], int],
    ) -> bool:
        normalized_mode = str(mode_id or "").strip()
        if not normalized_mode:
            return False
        if normalized_mode != self.ANALYST_MODE_ID:
            return False

        preserve_runtime_template_id = self._resolve_preserved_runtime_template_id(session)
        self._reset_analyst_state(session, preserve_runtime_template_id=preserve_runtime_template_id)

        session_id = str(getattr(session, "id", "") or "").strip()
        scoped_key = session_scoped_key(session)
        session_uid = session_runtime_uid(session)
        if not session_id:
            return True

        try:
            clear_runtime_cache(scoped_key or session_id)
        except Exception:
            self._logger.exception(
                "mode pre-run reset: clear runtime cache failed session_id=%s scoped_key=%s",
                session_id,
                scoped_key,
            )

        pending_tokens: list[str] = []
        if scoped_key:
            pending_tokens.append(scoped_key)
        if session_uid:
            pending_tokens.append(session_uid)
        if session_id and session_id not in pending_tokens:
            pending_tokens.append(session_id)
        for pending_token in pending_tokens:
            try:
                removed = int(clear_pending_questions(pending_token) or 0)
                if removed > 0:
                    break
            except Exception:
                self._logger.exception(
                    "mode pre-run reset: clear pending questions failed session_id=%s token=%s",
                    session_id,
                    pending_token,
                )

        return True

    def _reset_analyst_state(self, session: Any, *, preserve_runtime_template_id: str = "") -> None:
        self._reset_analyst_context(session, preserve_runtime_template_id=preserve_runtime_template_id)
        self._reset_analyst_run_artifacts(session)

        if hasattr(session, "analyst_mode") or hasattr(session, "modes"):
            set_analyst_mode(session, "spec")
        if hasattr(session, "analyst_runtime_template_id"):
            session.analyst_runtime_template_id = str(preserve_runtime_template_id or "")

    def _resolve_preserved_runtime_template_id(self, session: Any) -> str:
        runtime_override = str(getattr(session, "analyst_runtime_template_id", "") or "").strip().lower()
        if runtime_override != "audit":
            return ""
        ctx = self._load_analyst_context(session)
        if ctx is None:
            return ""
        ctx_mode = str(getattr(ctx, "mode", "") or "").strip().lower()
        ctx_flow = str(getattr(ctx, "active_flow", "") or "").strip().lower()
        ctx_runtime = str(getattr(ctx, "runtime_template_id", "") or "").strip().lower()
        if ctx_mode == "audit" and ctx_flow == "audit" and ctx_runtime == "audit":
            return "audit"
        return ""

    def _load_analyst_context(self, session: Any) -> Optional[Any]:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return None
        try:
            state_root = cli_proxy_artifact_path(workdir, ".analyst_data")
            store = AnalystStateStore(state_root)
            keys = self._analyst_context_keys(session)
            for context_key in keys:
                if os.path.exists(store.path_for(context_key)):
                    return store.load(context_key)
            if keys:
                return store.load(keys[0])
            return None
        except Exception:
            self._logger.exception(
                "mode pre-run reset: analyst context load failed session_id=%s",
                str(getattr(session, "id", "") or ""),
            )
            return None

    def _reset_analyst_context(self, session: Any, *, preserve_runtime_template_id: str = "") -> None:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return

        try:
            state_root = cli_proxy_artifact_path(workdir, ".analyst_data")
            store = AnalystStateStore(state_root)
            for idx, context_key in enumerate(self._analyst_context_keys(session)):
                if idx > 0 and not os.path.exists(store.path_for(context_key)):
                    continue
                ctx = store.load(context_key)
                ctx.mode = "spec"
                ctx.active_flow = ""
                ctx.runtime_template_id = str(preserve_runtime_template_id or "")
                ctx.effective_template_id = ""
                ctx.intent_reason = ""
                ctx.detail_level = ""
                ctx.document_kind = ""
                ctx.requires_codebase_grounding = False
                ctx.requires_repo_audit = False
                ctx.requires_final_repo_review = False
                ctx.needs_clarification = False
                ctx.clarification_is_blocking = False
                ctx.clarification_topic = ""
                ctx.source_user_text = ""
                ctx.clarification_answers = []
                ctx.last_draft = ""
                ctx.last_draft_updated_at = 0.0
                store.save(ctx)
        except Exception:
            self._logger.exception(
                "mode pre-run reset: analyst context reset failed session_id=%s",
                str(getattr(session, "id", "") or ""),
            )

    def _reset_analyst_run_artifacts(self, session: Any) -> None:
        try:
            store = RunArtifactStore(getattr(session, "config", None))
            latest = store.latest_run(session=session, mode_id=self.ANALYST_MODE_ID)
            if latest is None:
                return
            state = store.load_state(latest)
            if is_terminal_status(state.get("status")):
                return
            store.mark_finished(
                latest,
                status="superseded",
                phase=str(state.get("phase") or "execute"),
            )
        except Exception:
            self._logger.exception(
                "mode pre-run reset: analyst run artifacts reset failed session_id=%s",
                str(getattr(session, "id", "") or ""),
            )

    @staticmethod
    def _analyst_context_keys(session: Any) -> list[str]:
        keys: list[str] = []
        scoped_key = session_scoped_key(session)
        legacy_key = build_context_key(
            getattr(session, "chat_id", None),
            str(getattr(session, "id", "") or "").strip() or "default",
        )
        for key in (scoped_key, legacy_key):
            normalized = str(key or "").strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
        return keys
