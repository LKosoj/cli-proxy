from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodebaseContextText:
    intro: str
    stack: str
    architecture: str
    structure: str
    integrations: str
    conventions: str
    testing: str
    concerns: str
    outro: str


class CodebaseContextService:
    _DOC_ORDER = (
        ("STACK.md", "stack"),
        ("ARCHITECTURE.md", "architecture"),
        ("STRUCTURE.md", "structure"),
        ("INTEGRATIONS.md", "integrations"),
        ("CONVENTIONS.md", "conventions"),
        ("TESTING.md", "testing"),
        ("CONCERNS.md", "concerns"),
    )

    @classmethod
    def build_context(
        cls,
        *,
        session: Any,
        runtime_getter: Any,
        text: CodebaseContextText,
    ) -> str:
        mapper = runtime_getter("codebase_mapper_status") if callable(runtime_getter) else None
        if mapper is None:
            return ""
        try:
            status = mapper.get_status(workdir=session.workdir)
        except Exception:
            _log.exception("codebase context: failed to get mapper status")
            return ""
        if not isinstance(status, dict):
            return ""
        if str(status.get("status") or "").strip() != "ready":
            return ""

        docs = {str(doc).strip() for doc in (status.get("docs") or []) if str(doc).strip()}
        if not docs:
            return ""

        lines = [str(text.intro or "")]
        for doc_name, field in cls._DOC_ORDER:
            if doc_name not in docs:
                continue
            value = str(getattr(text, field) or "").strip()
            if value:
                lines.append(value)
        lines.append(str(text.outro or ""))
        return "\n".join(lines).strip()
