from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from modes.sdk.runtime.contracts import DevTask, ProjectPlan

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------
#
# tasks.md is the living source of truth for the SDD task set. It must round-trip
# losslessly through render -> parse for every serialised field, including
# unresolved findings (failed/blocked tasks with a partial_work_note).
#
# Task metadata (status / deps / covers) lives on its own labelled lines rather
# than being packed into the header line. This keeps the free-text title immune
# to collisions with bracket/keyword sequences that the title may legitimately
# contain (e.g. "Fix [deps: T00] handler" or "Update status: parser").
#
# Layout:
#
#   # Tasks — <project_goal>
#   status: <plan.status>
#   created_at: <plan.created_at>          (may contain spaces, e.g. ISO with space)
#   updated_at: <plan.updated_at>
#   current_task: <id>                     (omitted when None)
#
#   ## <id> — <title>                      (### for a subtask: dotted id)
#   status: <task.status>
#   deps: a, b                             (omitted when empty)
#   covers: REQ-1, REQ-2                   (omitted when empty)
#   description: <first line>              (omitted when empty)
#     <continuation lines, every line — incl. blank — prefixed with 2 spaces>
#   - [x] <acceptance criterion>           (done marker -> ":done" suffix)
#   - [ ] <acceptance criterion>
#   review_verdict: <verdict>              (omitted when None)
#   partial_work_note: <first line>        (omitted when None)
#     <continuation lines>
#
# ---------------------------------------------------------------------------

_DONE_SUFFIX = ":done"

_HEADER_RE = re.compile(r"^#\s+Tasks\s+[—–-]\s+(.*)$")
_TASK_H2_RE = re.compile(r"^##\s+(\S+)\s+[—–-]\s+(.*)$")
_SUBTASK_H3_RE = re.compile(r"^###\s+(\S+)\s+[—–-]\s+(.*)$")

_STATUS_RE = re.compile(r"^status:\s*(.*)$")
_CREATED_RE = re.compile(r"^created_at:\s*(.*)$")
_UPDATED_RE = re.compile(r"^updated_at:\s*(.*)$")
_CURRENT_RE = re.compile(r"^current_task:\s*(.*)$")
_DEPS_RE = re.compile(r"^deps:\s*(.*)$")
_COVERS_RE = re.compile(r"^covers:\s*(.*)$")
_DESC_RE = re.compile(r"^description:(.*)$")
_VERDICT_RE = re.compile(r"^review_verdict:\s*(.*)$")
_PARTIAL_RE = re.compile(r"^partial_work_note:(.*)$")
_CONT_RE = re.compile(r"^  (.*)$")
_AC_DONE_RE = re.compile(r"^- \[x\] (.*)$", re.IGNORECASE)
_AC_PENDING_RE = re.compile(r"^- \[ \] (.*)$")


def _split_csv(raw: Optional[str]) -> List[str]:
    if not raw or not raw.strip():
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _strip_one_space(value: str) -> str:
    """Remove exactly one leading space written by the renderer (preserves the rest)."""
    return value[1:] if value.startswith(" ") else value


# ---------------------------------------------------------------------------
# render_tasks_md
# ---------------------------------------------------------------------------

def _render_multiline(lines: List[str], label: str, value: str) -> None:
    """Emit a labelled field whose value may span multiple lines."""
    parts = value.split("\n")
    lines.append(f"{label}: {parts[0]}")
    for cont in parts[1:]:
        lines.append(f"  {cont}")


def render_tasks_md(plan: ProjectPlan) -> str:
    lines: List[str] = []

    # ---- plan header ----
    lines.append(f"# Tasks — {plan.project_goal}")
    lines.append(f"status: {plan.status}")
    lines.append(f"created_at: {plan.created_at}")
    lines.append(f"updated_at: {plan.updated_at}")
    if plan.current_task_id is not None:
        lines.append(f"current_task: {plan.current_task_id}")
    lines.append("")

    # ---- tasks ----
    for task in (plan.tasks or []):
        is_subtask = "." in task.id
        level = "###" if is_subtask else "##"
        lines.append(f"{level} {task.id} — {task.title}")
        lines.append(f"status: {task.status}")

        if task.depends_on:
            lines.append(f"deps: {', '.join(task.depends_on)}")
        if task.covers_requirements:
            lines.append(f"covers: {', '.join(task.covers_requirements)}")

        if task.description:
            _render_multiline(lines, "description", task.description)

        for ac in (task.acceptance_criteria or []):
            if ac.endswith(_DONE_SUFFIX):
                lines.append(f"- [x] {ac[:-len(_DONE_SUFFIX)]}")
            else:
                lines.append(f"- [ ] {ac}")

        if task.review_verdict is not None:
            lines.append(f"review_verdict: {task.review_verdict}")
        if task.partial_work_note is not None:
            _render_multiline(lines, "partial_work_note", task.partial_work_note)

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# parse_tasks_md
# ---------------------------------------------------------------------------

def _make_default_task(task_id: str, title: str) -> DevTask:
    """Create a DevTask whose non-serialised fields keep their dataclass defaults."""
    return DevTask(
        id=task_id,
        title=title,
        description="",
        acceptance_criteria=[],
        covers_requirements=[],
        depends_on=[],
        status="pending",
        attempt=0,
        max_attempts=3,
    )


def _match_task_header(line: str) -> Optional[Tuple[str, str, bool]]:
    """Return (id, title, is_subtask) for a task/subtask header line, else None."""
    m = _SUBTASK_H3_RE.match(line)
    if m:
        return m.group(1), m.group(2).strip(), True
    m = _TASK_H2_RE.match(line)
    if m:
        return m.group(1), m.group(2).strip(), False
    return None


def parse_tasks_md(text: str) -> ProjectPlan:
    """Parse a tasks.md string back into a ProjectPlan."""
    lines = text.splitlines()

    project_goal = ""
    plan_status = "active"
    created_at = ""
    updated_at = ""
    current_task_id: Optional[str] = None
    tasks: List[DevTask] = []

    last_root_id: Optional[str] = None

    cur: Optional[DevTask] = None
    cur_is_subtask = False
    cur_parent_id: Optional[str] = None
    multiline: Optional[str] = None  # None | "description" | "partial_work_note"

    header_found = False
    in_plan_header = False

    def _flush() -> None:
        nonlocal cur, cur_is_subtask, cur_parent_id, multiline
        if cur is not None:
            if cur_is_subtask and not cur.depends_on:
                if last_root_id is not None and last_root_id == cur_parent_id:
                    cur.depends_on = [cur_parent_id]
                elif last_root_id is not None:
                    _log.warning(
                        "Subtask %s: parent %s not directly preceding (last root: %s); "
                        "auto-dep not added", cur.id, cur_parent_id, last_root_id,
                    )
                else:
                    _log.warning("Subtask %s: no parent seen yet, auto-dep skipped", cur.id)
            tasks.append(cur)
        cur = None
        cur_is_subtask = False
        cur_parent_id = None
        multiline = None

    def _append_multiline(field: str, value: str) -> None:
        # multiline выставляется только при наличии cur; guard вместо assert,
        # чтобы поведение не зависело от флага -O интерпретатора.
        if cur is None:
            return
        prev = getattr(cur, field) or ""
        setattr(cur, field, prev + "\n" + value)

    for line in lines:
        # ---- plan header line ----
        if not header_found:
            m = _HEADER_RE.match(line)
            if m:
                project_goal = m.group(1).strip()
                header_found = True
                in_plan_header = True
            continue

        # ---- active multiline field (highest priority) ----
        if multiline is not None:
            mc = _CONT_RE.match(line)
            if mc:
                _append_multiline(multiline, mc.group(1))
                continue
            multiline = None  # fall through and reprocess this line

        # ---- task / subtask header ----
        parsed = _match_task_header(line)
        if parsed is not None:
            _flush()
            in_plan_header = False
            task_id, title, is_subtask = parsed
            if "[" in title:
                _log.warning("Task %s: title contains '[': %r", task_id, title)
            cur = _make_default_task(task_id, title)
            cur_is_subtask = is_subtask
            if is_subtask:
                cur_parent_id = task_id.rsplit(".", 1)[0]
            else:
                last_root_id = task_id
            continue

        # ---- plan-header region (before the first task) ----
        if in_plan_header:
            m = _STATUS_RE.match(line)
            if m:
                plan_status = m.group(1).strip() or plan_status
                continue
            m = _CREATED_RE.match(line)
            if m:
                created_at = m.group(1).strip()
                continue
            m = _UPDATED_RE.match(line)
            if m:
                updated_at = m.group(1).strip()
                continue
            m = _CURRENT_RE.match(line)
            if m:
                current_task_id = m.group(1).strip() or None
                continue
            continue  # blank/unknown line in header region

        # `current_task:` may also appear after the header block
        m = _CURRENT_RE.match(line)
        if cur is None and m:
            current_task_id = m.group(1).strip() or None
            continue

        if cur is None:
            continue

        # ---- task body fields ----
        m = _STATUS_RE.match(line)
        if m:
            cur.status = m.group(1).strip() or cur.status
            continue
        m = _DEPS_RE.match(line)
        if m:
            cur.depends_on = _split_csv(m.group(1))
            continue
        m = _COVERS_RE.match(line)
        if m:
            cur.covers_requirements = _split_csv(m.group(1))
            continue
        m = _DESC_RE.match(line)
        if m:
            cur.description = _strip_one_space(m.group(1))
            multiline = "description"
            continue
        m = _AC_DONE_RE.match(line)
        if m:
            cur.acceptance_criteria.append(m.group(1) + _DONE_SUFFIX)
            continue
        m = _AC_PENDING_RE.match(line)
        if m:
            cur.acceptance_criteria.append(m.group(1))
            continue
        m = _VERDICT_RE.match(line)
        if m:
            cur.review_verdict = m.group(1).strip()
            continue
        m = _PARTIAL_RE.match(line)
        if m:
            cur.partial_work_note = _strip_one_space(m.group(1))
            multiline = "partial_work_note"
            continue
        # unknown line inside a task — ignore

    _flush()

    return ProjectPlan(
        project_goal=project_goal,
        tasks=tasks,
        status=plan_status,
        created_at=created_at,
        updated_at=updated_at,
        current_task_id=current_task_id,
    )
