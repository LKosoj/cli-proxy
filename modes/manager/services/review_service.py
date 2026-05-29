from __future__ import annotations

import hashlib
import os
import stat
import time
from typing import Any, Callable, Dict, List, Optional

from modes.manager.services.plan_service import PlanManagementService
from modes.sdk.runtime.contracts import ProjectPlan


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class ReviewAndMergeService:
    """Review/reconcile helpers for Manager mode (diff audit + plan adjustments)."""

    _AUTO_APPROVED_COMMENT = "Автоматически закрыта: работа выполнена в рамках предыдущей задачи"

    _IGNORED_DIRS = {
        ".git",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".vite",
        ".cli-proxy",
        ".manager",
        ".manager_archive",
    }

    def __init__(self, *, plan_service: Optional[PlanManagementService] = None) -> None:
        self._plan_service = plan_service or PlanManagementService()

    @classmethod
    def should_ignore_dirname(cls, name: str) -> bool:
        return str(name or "") in cls._IGNORED_DIRS

    @staticmethod
    def should_ignore_relpath(rel_path: str) -> bool:
        p = str(rel_path or "").replace("\\", "/")
        if "/.git/" in p or p.startswith(".git/"):
            return True
        if "/.venv/" in p or p.startswith(".venv/"):
            return True
        if "/.cli-proxy/" in p or p.startswith(".cli-proxy/"):
            return True
        if "/.manager/" in p or p.startswith(".manager/"):
            return True
        if "/.manager_archive/" in p or p.startswith(".manager_archive/"):
            return True
        if "/__pycache__/" in p or p.startswith("__pycache__/"):
            return True
        if "/node_modules/" in p or p.startswith("node_modules/"):
            return True
        if "/dist/" in p or p.startswith("dist/"):
            return True
        if "/build/" in p or p.startswith("build/"):
            return True
        if "/coverage/" in p or p.startswith("coverage/"):
            return True
        if "/.next/" in p or p.startswith(".next/"):
            return True
        if "/.nuxt/" in p or p.startswith(".nuxt/"):
            return True
        if "/.svelte-kit/" in p or p.startswith(".svelte-kit/"):
            return True
        if "/.turbo/" in p or p.startswith(".turbo/"):
            return True
        if "/.vite/" in p or p.startswith(".vite/"):
            return True
        return False

    @staticmethod
    def hash_file(path: str, *, max_bytes: int) -> Optional[str]:
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            if st.st_size > max_bytes:
                return None
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(128 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def snapshot_workdir(
        self,
        workdir: str,
        *,
        max_files: int = 50_000,
        hash_max_bytes: int = 256 * 1024,
    ) -> Dict[str, Dict[str, object]]:
        root = os.path.abspath(workdir or ".")
        snap: Dict[str, Dict[str, object]] = {}
        if not os.path.isdir(root):
            return snap

        file_count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not self.should_ignore_dirname(d)]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except Exception:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                if self.should_ignore_relpath(rel):
                    continue
                sha = self.hash_file(full, max_bytes=hash_max_bytes)
                snap[rel] = {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns), "sha256": sha}
                file_count += 1
                if file_count >= max_files:
                    return snap
        return snap

    @staticmethod
    def diff_snapshots(
        before: Dict[str, Dict[str, object]],
        after: Dict[str, Dict[str, object]],
    ) -> Dict[str, object]:
        created: List[str] = []
        modified: List[str] = []
        deleted: List[str] = []

        before_keys = set(before.keys())
        after_keys = set(after.keys())

        for p in sorted(after_keys - before_keys):
            created.append(p)
        for p in sorted(before_keys - after_keys):
            deleted.append(p)
        for p in sorted(before_keys & after_keys):
            b = before.get(p) or {}
            a = after.get(p) or {}
            if b.get("size") != a.get("size"):
                modified.append(p)
                continue
            b_sha = b.get("sha256")
            a_sha = a.get("sha256")
            if b_sha and a_sha and b_sha != a_sha:
                modified.append(p)
                continue
            if b.get("mtime_ns") != a.get("mtime_ns"):
                modified.append(p)
                continue

        return {"created": created, "modified": modified, "deleted": deleted}

    @staticmethod
    def format_change_audit(diff: Dict[str, object], *, max_list: int = 50) -> str:
        created = list(diff.get("created") or [])
        modified = list(diff.get("modified") or [])
        deleted = list(diff.get("deleted") or [])

        def _fmt(title: str, items: List[str]) -> List[str]:
            if not items:
                return [f"- {title}: 0"]
            head = items[:max_list]
            more = len(items) - len(head)
            lines = [f"- {title}: {len(items)}"]
            lines += [f"  - {p}" for p in head]
            if more > 0:
                lines.append(f"  - ... (+{more} more)")
            return lines

        lines: List[str] = []
        lines.append("### Изменения в файлах (аудит без git)")
        lines.append(f"Создано: {len(created)} | Изменено: {len(modified)} | Удалено: {len(deleted)}")
        lines.append("")
        lines += _fmt("Созданные", created)
        lines += _fmt("Измененные", modified)
        lines += _fmt("Удаленные", deleted)
        return "\n".join(lines).strip()

    @staticmethod
    def remaining_tasks_info(plan: ProjectPlan) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for task in list(plan.tasks or []):
            if task.status in ("approved", "failed", "blocked"):
                continue
            out.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "acceptance_criteria": task.acceptance_criteria,
                    "status": task.status,
                    "depends_on": task.depends_on,
                }
            )
        return out

    def apply_reconcile_payload(
        self,
        plan: ProjectPlan,
        payload: Dict[str, Any],
        *,
        now_iso: Optional[Callable[[], str]] = None,
    ) -> Dict[str, Any]:
        now = now_iso or _now_iso
        result: Dict[str, Any] = {
            "analysis_changed": False,
            "completed_ids": [],
            "adjusted_ids": [],
            "adjustment_log": [],
            "changes_made": False,
        }
        if not isinstance(payload, dict):
            return result

        upd_analysis = payload.get("updated_analysis")
        if isinstance(upd_analysis, dict):
            analysis_changed = self._plan_service.update_plan_analysis(plan, upd_analysis)
            result["analysis_changed"] = bool(analysis_changed)
            result["changes_made"] = bool(result["changes_made"] or analysis_changed)

        tasks_by_id = {t.id: t for t in plan.tasks}

        completed_ids_raw = payload.get("completed_task_ids") or []
        if isinstance(completed_ids_raw, list):
            for raw_tid in completed_ids_raw:
                tid = str(raw_tid or "").strip()
                if not tid:
                    continue
                task = tasks_by_id.get(tid)
                if task and task.status not in ("approved", "failed"):
                    if self._plan_service.mark_task_completed(plan, tid, now_iso=now):
                        task.review_verdict = "approved"
                        task.review_comments = self._AUTO_APPROVED_COMMENT
                        result["completed_ids"].append(tid)
                        result["changes_made"] = True

        adjustments = payload.get("adjustments") or []
        if not isinstance(adjustments, list):
            adjustments = []
        for adj in adjustments:
            if not isinstance(adj, dict):
                continue
            tid = str(adj.get("task_id") or "").strip()
            task = tasks_by_id.get(tid)
            if not tid or not task or task.status in ("approved", "failed"):
                continue

            was_changed = False
            new_desc = adj.get("updated_description")
            new_criteria = adj.get("updated_acceptance_criteria")
            done_note = adj.get("already_done_note")

            if isinstance(new_desc, str) and new_desc.strip():
                task.description = new_desc.strip()
                was_changed = True
            if isinstance(new_criteria, list) and new_criteria:
                task.acceptance_criteria = [str(c) for c in new_criteria if str(c or "").strip()]
                was_changed = True
            if isinstance(done_note, str) and done_note.strip():
                existing = task.partial_work_note or ""
                if existing:
                    task.partial_work_note = f"{existing}\n{done_note.strip()}"
                else:
                    task.partial_work_note = done_note.strip()
                was_changed = True

            result["adjustment_log"].append(
                {"task_id": tid, "reason": str(adj.get("reason") or "").strip()}
            )
            if was_changed:
                result["adjusted_ids"].append(tid)
                result["changes_made"] = True

        return result


__all__ = ["ReviewAndMergeService"]
