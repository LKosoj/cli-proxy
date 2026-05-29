from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from utils.paths import cli_proxy_artifact_path

logger = logging.getLogger(__name__)

_SUBDIRS = ("input", "steps", "draft", "output")


def resolve_analyst_runs_root(session: Any) -> str:
    """Return the base path for analyst run directories inside session workdir."""
    workdir = str(getattr(session, "workdir", "") or "").strip()
    if workdir:
        return cli_proxy_artifact_path(workdir, ".analyst_runs")
    return cli_proxy_artifact_path(os.getcwd(), ".analyst_runs")


class AnalystRunDirectory:
    """Manages an isolated directory for a single analyst pipeline run."""

    def __init__(self, base_path: str, run_id: str | None = None) -> None:
        """
        Args:
            base_path: root for storing runs (usually session workdir / .analyst_runs)
            run_id: if None a new one is generated (date + short hash)
        """
        self._base_path = os.path.abspath(str(base_path or "."))
        self._run_id = run_id or self.generate_run_id()

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    @staticmethod
    def generate_run_id() -> str:
        """Generate a run_id in the format: 2026-04-13_a3f8b2"""
        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hash_part = uuid.uuid4().hex[:6]
        return f"{date_part}_{hash_part}"

    # ------------------------------------------------------------------
    # Directory creation
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        analysis_profile: str,
        document_kind: str,
        detail_level: str,
        template_id: str,
        summary: str,
        user_request: str,
        session_id: str | None = None,
    ) -> None:
        """
        Create directory structure and initial meta.json.

        Creates subdirectories: input/, steps/, draft/, output/
        Writes input/user_request.md with the original user request.
        """
        if os.path.isfile(self.meta_path):
            logger.warning("Run directory already exists: %s", self.run_path)
            return

        os.makedirs(self.run_path, exist_ok=True)
        for subdir in _SUBDIRS:
            os.makedirs(os.path.join(self.run_path, subdir), exist_ok=True)

        meta: dict[str, Any] = {
            "run_id": self._run_id,
            "status": "running",
            "analysis_profile": str(analysis_profile or ""),
            "document_kind": str(document_kind or ""),
            "detail_level": str(detail_level or ""),
            "template_id": str(template_id or ""),
            "summary": str(summary or ""),
            "user_request": str(user_request or ""),
            "session_id": str(session_id or ""),
            "clarification_answers": [],
            "current_phase": "classify",
            "gate2_executed": False,
            "ask_user_locked": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "steps": [],
        }
        self.save_meta(meta)

        # Write user request to input file
        request_path = self.user_request_path()
        with open(request_path, "w", encoding="utf-8") as fh:
            fh.write(str(user_request or ""))

    # ------------------------------------------------------------------
    # Path properties
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def run_path(self) -> str:
        """Absolute path to the run directory."""
        return os.path.join(self._base_path, self._run_id)

    @property
    def meta_path(self) -> str:
        """Path to meta.json."""
        return os.path.join(self.run_path, "meta.json")

    # ------------------------------------------------------------------
    # meta.json I/O
    # ------------------------------------------------------------------

    def load_meta(self) -> dict[str, Any]:
        """Load meta.json. Returns empty dict if the file does not exist."""
        path = self.meta_path
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.exception("Failed to load meta.json at %s", path)
            return {}

    def save_meta(self, meta: dict[str, Any]) -> None:
        """Save meta.json atomically (write to temp file then rename)."""
        os.makedirs(os.path.dirname(self.meta_path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(self.meta_path),
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.meta_path)
        except BaseException:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def update_meta(self, **kwargs: Any) -> dict[str, Any]:
        """Load + merge + save. Returns the updated meta dict."""
        meta = self.load_meta()
        meta.update(kwargs)
        self.save_meta(meta)
        return meta

    def update_phase(self, phase: str) -> None:
        """Update current_phase in meta.json."""
        self.update_meta(current_phase=str(phase or ""))

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def add_step(self, step_id: str, *, status: str = "pending") -> str:
        """
        Register a new step in meta.json.

        If a step with the same *step_id* already exists it is **not** duplicated;
        the existing entry is returned as-is.

        Returns:
            Absolute path to the artifact file: steps/{step_id}.md
        """
        meta = self.load_meta()
        steps: list[dict[str, Any]] = list(meta.get("steps") or [])

        # Guard against duplicate step_id
        for existing in steps:
            if existing.get("id") == step_id:
                logger.debug("Step %s already registered, skipping add", step_id)
                return self.step_artifact_path(step_id)

        step_entry: dict[str, Any] = {
            "id": str(step_id),
            "status": str(status or "pending"),
            "artifact": f"steps/{self._normalize_step_id(step_id)}.md",
            "attempts": 0,
        }
        steps.append(step_entry)
        meta["steps"] = steps
        self.save_meta(meta)
        return self.step_artifact_path(step_id)

    def update_step(
        self,
        step_id: str,
        *,
        status: str,
        gap: str | None = None,
        attempts: int | None = None,
    ) -> None:
        """Update step status in meta.json."""
        meta = self.load_meta()
        steps: list[dict[str, Any]] = list(meta.get("steps") or [])
        for step in steps:
            if step.get("id") == step_id:
                step["status"] = str(status)
                if gap is not None:
                    step["gap"] = str(gap)
                elif "gap" in step and status == "completed":
                    del step["gap"]
                if attempts is not None:
                    step["attempts"] = int(attempts)
                break
        meta["steps"] = steps
        self.save_meta(meta)

    @staticmethod
    def _normalize_step_id(step_id: str) -> str:
        token = str(step_id or "").strip()
        token = re.sub(r"[^0-9A-Za-z._-]+", "-", token)
        token = token.strip("-.")
        return token or "step"

    @staticmethod
    def _clean_text_list(values: Any) -> List[str]:
        items: List[str] = []
        seen: set[str] = set()
        if not isinstance(values, list):
            return items
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            items.append(text)
        return items

    def _collect_reviewed_sources(self, entry: Dict[str, Any]) -> List[str]:
        sources: List[str] = []
        seen: set[str] = set()

        def _push(candidate: Any) -> None:
            text = str(candidate or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            sources.append(text)

        for key in ("orchestrator_artifact", "artifact", "source", "path", "file_path", "url", "href"):
            _push(entry.get(key))

        for collection_key in ("outputs", "artifacts"):
            collection = entry.get(collection_key) or []
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                for key in ("path", "file_path", "source", "url", "href", "name"):
                    _push(item.get(key))

        claims = entry.get("claims") or []
        if isinstance(claims, list):
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                evidence = claim.get("evidence") or claim.get("sources") or []
                if isinstance(evidence, list):
                    for item in evidence:
                        if isinstance(item, dict):
                            for key in ("path", "file_path", "source", "url", "href", "name"):
                                _push(item.get(key))
                        else:
                            _push(item)
                for key in ("source", "path", "file_path", "url", "href", "name"):
                    _push(claim.get(key))

        return sources

    def _collect_confirmed_facts(self, entry: Dict[str, Any]) -> List[str]:
        facts: List[str] = []
        seen: set[str] = set()

        def _push(candidate: Any) -> None:
            text = " ".join(str(candidate or "").split()).strip()
            if not text or text in seen:
                return
            seen.add(text)
            facts.append(text)

        summary = str(entry.get("summary") or "").strip()
        if summary:
            _push(summary)

        outputs = entry.get("outputs") or []
        if isinstance(outputs, list):
            for output in outputs:
                if not isinstance(output, dict):
                    continue
                if str(output.get("type") or "").strip() != "text":
                    continue
                preview = str(output.get("content_preview") or output.get("content") or "").strip()
                if preview:
                    _push(preview)

        claims = entry.get("claims") or []
        if isinstance(claims, list):
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                status = str(claim.get("status") or "").strip().lower()
                if status not in {"confirmed", "ok", "pass", "done", "true"}:
                    continue
                for key in ("text", "statement", "summary", "content", "claim", "description", "fact"):
                    value = str(claim.get(key) or "").strip()
                    if value:
                        _push(value)
                        break

        return facts

    def _collect_unconfirmed_gaps(self, entry: Dict[str, Any]) -> List[str]:
        gaps: List[str] = []
        seen: set[str] = set()

        def _push(candidate: Any) -> None:
            text = " ".join(str(candidate or "").split()).strip()
            if not text or text in seen:
                return
            seen.add(text)
            gaps.append(text)

        gap = str(entry.get("gap") or "").strip()
        if gap:
            _push(gap)

        status = str(entry.get("status") or "").strip().lower()
        if status and status not in {"ok", "done", "completed"}:
            _push(f"status={status}")

        claims = entry.get("claims") or []
        if isinstance(claims, list):
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                claim_status = str(claim.get("status") or "").strip().lower()
                if claim_status in {"confirmed", "ok", "pass", "done", "true"}:
                    continue
                for key in ("text", "statement", "summary", "content", "claim", "description", "fact"):
                    value = str(claim.get(key) or "").strip()
                    if value:
                        _push(value)
                        break

        if not gaps:
            _push("не зафиксированы")
        return gaps

    def _render_step_history_section(self, attempts: List[Dict[str, Any]]) -> str:
        if len(attempts) <= 1:
            return ""
        lines: List[str] = ["", "## Attempts / History", ""]
        for index, attempt in enumerate(attempts, start=1):
            attempt_no = int(attempt.get("attempt") or index)
            lines.extend(
                [
                    f"### Attempt {attempt_no}",
                    "",
                    f"- status: {str(attempt.get('status') or '').strip() or 'unknown'}",
                    f"- summary: {str(attempt.get('summary') or '').strip() or '[нет summary]'}",
                ]
            )
            reviewed_sources = self._clean_text_list(attempt.get("reviewed_sources"))
            if reviewed_sources:
                lines.append("- reviewed_sources:")
                for source in reviewed_sources:
                    lines.append(f"  - {source}")
            confirmed_facts = self._clean_text_list(attempt.get("confirmed_facts"))
            if confirmed_facts:
                lines.append("- confirmed_facts:")
                for fact in confirmed_facts:
                    lines.append(f"  - {fact}")
            unconfirmed_gaps = self._clean_text_list(attempt.get("unconfirmed_gaps"))
            if unconfirmed_gaps:
                lines.append("- unconfirmed_gaps:")
                for item in unconfirmed_gaps:
                    lines.append(f"  - {item}")
            lines.append("")
        return "\n".join(lines).strip()

    def _render_step_markdown(self, step: Dict[str, Any]) -> str:
        step_id = str(step.get("id") or "").strip() or "step"
        title = str(step.get("title") or "").strip() or step_id
        step_type = str(step.get("step_type") or "").strip() or "task"
        status = str(step.get("status") or "").strip() or "unknown"
        attempts = int(step.get("attempts") or 0)
        reviewed_sources = self._clean_text_list(step.get("reviewed_sources"))
        confirmed_facts = self._clean_text_list(step.get("confirmed_facts"))
        unconfirmed_gaps = self._clean_text_list(step.get("unconfirmed_gaps"))
        lines: List[str] = [
            f"# Шаг {step_id}",
            "",
            "## Goal",
            "",
            title,
            "",
            f"- step_type: {step_type}",
            f"- status: {status}",
            f"- attempts: {attempts}",
            "",
            "## Reviewed files/sources",
            "",
        ]
        if reviewed_sources:
            for source in reviewed_sources:
                lines.append(f"- {source}")
        else:
            lines.append("- не зафиксированы")
        lines.extend(["", "## Confirmed facts", ""])
        if confirmed_facts:
            for fact in confirmed_facts:
                lines.append(f"- {fact}")
        else:
            lines.append("- не зафиксированы")
        lines.extend(["", "## Unconfirmed gaps", ""])
        if unconfirmed_gaps:
            for item in unconfirmed_gaps:
                lines.append(f"- {item}")
        else:
            lines.append("- не зафиксированы")
        history_block = self._render_step_history_section(list(step.get("history") or []))
        if history_block:
            lines.extend(["", history_block])
        return "\n".join(lines).strip()

    def _step_evidence_summary(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        step_count = len(steps)
        attempts_total = sum(max(0, int(step.get("attempts") or 0)) for step in steps)
        with_sources = sum(1 for step in steps if self._clean_text_list(step.get("reviewed_sources")))
        with_facts = sum(1 for step in steps if self._clean_text_list(step.get("confirmed_facts")))
        with_gaps = sum(1 for step in steps if self._clean_text_list(step.get("unconfirmed_gaps")))
        artifact_count = sum(1 for step in steps if str(step.get("artifact") or "").strip())
        completed_statuses = {"ok", "done", "completed"}

        def _requires_evidence(step: Dict[str, Any]) -> bool:
            step_type = str(step.get("step_type") or "").strip().lower()
            return "cli" in step_type or "research" in step_type or "search" in step_type

        evidence_required_steps = sum(1 for step in steps if _requires_evidence(step))
        evidence_steps_with_sources = sum(
            1 for step in steps if _requires_evidence(step) and self._clean_text_list(step.get("reviewed_sources"))
        )
        completed_evidence_steps = sum(
            1
            for step in steps
            if _requires_evidence(step) and str(step.get("status") or "").strip().lower() in completed_statuses
        )
        completed_evidence_steps_with_sources = sum(
            1
            for step in steps
            if _requires_evidence(step)
            and str(step.get("status") or "").strip().lower() in completed_statuses
            and self._clean_text_list(step.get("reviewed_sources"))
        )
        completed_steps = sum(
            1 for step in steps
            if str(step.get("status") or "").strip().lower() in completed_statuses
        )
        completed_steps_with_sources = sum(
            1
            for step in steps
            if str(step.get("status") or "").strip().lower() in completed_statuses
            and self._clean_text_list(step.get("reviewed_sources"))
        )
        warning_reasons: List[str] = []
        missing_sources = evidence_required_steps - evidence_steps_with_sources
        if missing_sources > 0:
            warning_reasons.append(
                f"Исследовательские CLI-шаги без reviewed files/sources: {missing_sources}"
            )
        missing_completed_sources = completed_evidence_steps - completed_evidence_steps_with_sources
        if missing_completed_sources > 0:
            warning_reasons.append(
                f"Завершенные исследовательские CLI-шаги без reviewed files/sources: {missing_completed_sources}"
            )
        return {
            "step_count": step_count,
            "attempt_count": attempts_total,
            "artifact_count": artifact_count,
            "completed_steps": completed_steps,
            "completed_steps_with_sources": completed_steps_with_sources,
            "evidence_required_steps": evidence_required_steps,
            "evidence_steps_with_sources": evidence_steps_with_sources,
            "completed_evidence_steps": completed_evidence_steps,
            "completed_evidence_steps_with_sources": completed_evidence_steps_with_sources,
            "steps_with_sources": with_sources,
            "steps_with_confirmed_facts": with_facts,
            "steps_with_unconfirmed_gaps": with_gaps,
            "artifact_coverage": float(artifact_count) / float(step_count) if step_count else 0.0,
            "sources_coverage": float(with_sources) / float(step_count) if step_count else 0.0,
            "facts_coverage": float(with_facts) / float(step_count) if step_count else 0.0,
            "gaps_coverage": float(with_gaps) / float(step_count) if step_count else 0.0,
            "warning_reasons": warning_reasons,
        }

    def sync_step_results(self, step_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Normalize orchestrator step_results into meta.steps and markdown artifacts."""
        meta = self.load_meta()
        grouped: dict[str, List[Dict[str, Any]]] = {}
        order: list[str] = []

        for index, item in enumerate(step_results or [], start=1):
            if not isinstance(item, dict):
                continue
            step_id = self._normalize_step_id(str(item.get("task_id") or item.get("id") or f"step-{index}"))
            if step_id not in grouped:
                grouped[step_id] = []
                order.append(step_id)
            attempt = len(grouped[step_id]) + 1
            grouped[step_id].append(
                {
                    "attempt": attempt,
                    "status": str(item.get("status") or "").strip() or "unknown",
                    "summary": str(item.get("summary") or "").strip(),
                    "title": str(item.get("title") or "").strip() or step_id,
                    "step_type": str(item.get("step_type") or "").strip() or "task",
                    "reviewed_sources": self._collect_reviewed_sources(item),
                    "confirmed_facts": self._collect_confirmed_facts(item),
                    "unconfirmed_gaps": self._collect_unconfirmed_gaps(item),
                    "orchestrator_artifact": str(item.get("orchestrator_artifact") or "").strip(),
                }
            )

        synced_steps: List[Dict[str, Any]] = []
        for step_id in order:
            attempts = grouped.get(step_id) or []
            if not attempts:
                continue
            latest = attempts[-1]
            normalized_step = {
                "id": step_id,
                "title": str(latest.get("title") or "").strip() or step_id,
                "step_type": str(latest.get("step_type") or "").strip() or "task",
                "status": str(latest.get("status") or "").strip() or "unknown",
                "attempts": len(attempts),
                "artifact": f"steps/{self._normalize_step_id(step_id)}.md",
                "summary": str(latest.get("summary") or "").strip(),
                "reviewed_sources": list(latest.get("reviewed_sources") or []),
                "confirmed_facts": list(latest.get("confirmed_facts") or []),
                "unconfirmed_gaps": list(latest.get("unconfirmed_gaps") or []),
                "history": attempts,
            }
            artifact_path = self.step_artifact_path(step_id)
            os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as fh:
                fh.write(self._render_step_markdown(normalized_step).rstrip() + "\n")
            synced_steps.append(normalized_step)

        meta["steps"] = synced_steps
        meta["evidence_trail"] = self._step_evidence_summary(synced_steps)
        meta["evidence_trail"]["synced_at"] = datetime.now(timezone.utc).isoformat()
        self.save_meta(meta)
        return dict(meta["evidence_trail"])

    def step_artifact_path(self, step_id: str) -> str:
        """Return the absolute path to the step artifact file."""
        return os.path.join(self.run_path, "steps", f"{self._normalize_step_id(step_id)}.md")

    def steps_with_status(self, status: str) -> list[dict[str, Any]]:
        """Return steps with the given status from meta.json."""
        meta = self.load_meta()
        target = str(status)
        return [
            dict(s) for s in (meta.get("steps") or [])
            if s.get("status") == target
        ]

    # ------------------------------------------------------------------
    # Draft
    # ------------------------------------------------------------------

    def draft_path(self, version: int = 1) -> str:
        """Path to draft/v{version}.md"""
        return os.path.join(self.run_path, "draft", f"v{version}.md")

    def current_draft_version(self) -> int:
        """Current draft version (determined by existing files). Returns 0 if none."""
        draft_dir = os.path.join(self.run_path, "draft")
        if not os.path.isdir(draft_dir):
            return 0
        max_ver = 0
        for name in os.listdir(draft_dir):
            if name.startswith("v") and name.endswith(".md"):
                try:
                    ver = int(name[1:-3])
                    if ver > max_ver:
                        max_ver = ver
                except ValueError:
                    continue
        return max_ver

    def next_draft_path(self) -> str:
        """Path to the next draft version."""
        return self.draft_path(self.current_draft_version() + 1)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def output_path(self) -> str:
        """Path to output/final.md"""
        return os.path.join(self.run_path, "output", "final.md")

    def finalize(self, draft_version: int | None = None) -> str:
        """
        Copy draft/vN.md to output/final.md.
        Update meta.json status to completed.

        Args:
            draft_version: version to finalize; defaults to current_draft_version()

        Returns:
            Path to final.md
        """
        version = draft_version if draft_version is not None else self.current_draft_version()
        if version < 1:
            raise FileNotFoundError("No draft to finalize")

        src = self.draft_path(version)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Draft file not found: {src}")
        dst = self.output_path()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

        self.update_meta(status="completed", current_phase="deliver")
        return dst

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def user_request_path(self) -> str:
        """Path to input/user_request.md"""
        return os.path.join(self.run_path, "input", "user_request.md")

    def codebase_context_path(self) -> str:
        """Path to input/codebase_context.md"""
        return os.path.join(self.run_path, "input", "codebase_context.md")

    def append_clarification_answers(self, answers: List[str]) -> None:
        """
        Append answers to input/user_request.md
        and update clarification_answers in meta.json.
        """
        if not answers:
            return

        clean_answers = [str(a).strip() for a in answers if str(a).strip()]
        if not clean_answers:
            return

        # Append to user_request.md
        request_path = self.user_request_path()
        os.makedirs(os.path.dirname(request_path), exist_ok=True)

        # Only write the section header if it hasn't been written yet
        header_needed = True
        if os.path.isfile(request_path):
            with open(request_path, "r", encoding="utf-8") as fh:
                header_needed = "## Уточнения пользователя" not in fh.read()

        with open(request_path, "a", encoding="utf-8") as fh:
            if header_needed:
                fh.write("\n\n---\n\n## Уточнения пользователя\n\n")
            else:
                fh.write("\n")
            for i, answer in enumerate(clean_answers, 1):
                fh.write(f"{i}. {answer}\n")

        # Update meta.json
        meta = self.load_meta()
        existing: list[str] = list(meta.get("clarification_answers") or [])
        existing.extend(clean_answers)
        meta["clarification_answers"] = existing
        self.save_meta(meta)

    # ------------------------------------------------------------------
    # Class-level lookups
    # ------------------------------------------------------------------

    @classmethod
    def latest_run(cls, base_path: str, *, session_id: str | None = None) -> AnalystRunDirectory | None:
        """Find the latest run by sorting directory names (date prefix ensures order)."""
        base = os.path.abspath(str(base_path or "."))
        if not os.path.isdir(base):
            return None
        entries = []
        for name in os.listdir(base):
            full = os.path.join(base, name)
            meta_path = os.path.join(full, "meta.json")
            if not os.path.isdir(full) or not os.path.isfile(meta_path):
                continue
            if session_id:
                try:
                    with open(meta_path, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                except Exception:
                    logger.exception("Failed to inspect run meta at %s", meta_path)
                    continue
                if str(meta.get("session_id") or "").strip() != str(session_id):
                    continue
            entries.append(name)
        if not entries:
            return None
        entries.sort()
        return cls(base, run_id=entries[-1])

    @classmethod
    def find_run(cls, base_path: str, run_id: str) -> AnalystRunDirectory | None:
        """Find a specific run by run_id."""
        base = os.path.abspath(str(base_path or "."))
        run_dir = os.path.join(base, str(run_id))
        if os.path.isdir(run_dir) and os.path.isfile(os.path.join(run_dir, "meta.json")):
            return cls(base, run_id=str(run_id))
        return None
