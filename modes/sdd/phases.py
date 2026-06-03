from __future__ import annotations

import datetime
import logging
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from modes.sdk.runtime.json_normalizer import loads_safe
from modes.sdk.runtime.contracts import DevTask, ProjectPlan

from .schemas import SPEC_OUTPUT_SCHEMA, PLAN_OUTPUT_SCHEMA, TASKS_OUTPUT_SCHEMA, validate_sdd_payload

_log = logging.getLogger(__name__)

PHASE_ORDER = ("specify", "plan", "tasks", "analyze")

ModelCall = Callable[[str, str], Awaitable[str]]
# (work_type, system, user, schema) -> validated JSON payload. Encapsulates the routed
# CLI call plus the LLM-API fallback (implemented in mode.py); phases stay transport-blind.
CliCall = Callable[[str, str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


def next_phase(phase: str) -> Optional[str]:
    """Return the phase that follows *phase*, or None if it is the last."""
    try:
        idx = PHASE_ORDER.index(phase)
    except ValueError:
        return None
    if idx + 1 < len(PHASE_ORDER):
        return PHASE_ORDER[idx + 1]
    return None


def slugify(intent: str) -> str:
    """Convert an arbitrary intent string to a safe kebab-case slug."""
    text = str(intent or "feature").lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:40] or "feature"


def allocate_spec_dir(workdir: str, slug: str) -> str:
    """Return specs/<NNN>-<slug> path; reuse existing dir for same slug, else next number."""
    specs_root = os.path.join(workdir, "specs")
    if os.path.isdir(specs_root):
        for entry in sorted(os.listdir(specs_root)):
            parts = entry.split("-", 1)
            if len(parts) == 2 and parts[1] == slug and os.path.isdir(os.path.join(specs_root, entry)):
                return os.path.join(specs_root, entry)
        existing_numbers: List[int] = []
        for entry in os.listdir(specs_root):
            m = re.match(r"^(\d+)-", entry)
            if m:
                existing_numbers.append(int(m.group(1)))
        next_num = max(existing_numbers, default=0) + 1
    else:
        next_num = 1
    return os.path.join(specs_root, f"{next_num:03d}-{slug}")


def normalize_spec_dir(workdir: str, spec_dir: str) -> Optional[str]:
    """Normalize restored spec_dir and keep it inside <workdir>/specs."""
    workdir_text = str(workdir or "").strip()
    if not workdir_text:
        return None
    root = Path(workdir_text).resolve(strict=False)
    raw = str(spec_dir or "").strip()
    if not raw:
        return None

    specs_root = root / "specs"
    raw_path = Path(raw)
    if raw_path.is_absolute():
        candidate = raw_path
    elif raw_path.parts and raw_path.parts[0] == "specs":
        candidate = root / raw_path
    else:
        candidate = specs_root / raw_path

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
        resolved.relative_to(specs_root)
    except ValueError:
        return None
    return str(resolved)


_AUDIT_PORCELAIN_RE = re.compile(r"^[ MADRCU?!]{1,2}\s+(.+)$")
_AUDIT_EXT_RE = re.compile(r"\S+\.(?:py|yaml|md|txt|json|toml|cfg|ini|ts|js)$")
# Тестовый путь: компонент tests/ или test_ /  _test. на границе пути, а не подстрока
# "test" внутри слов вроде latest/ contest/ protest/.
_TEST_PATH_RE = re.compile(r"(?:^|/)tests?[_/]|[_/]test[_.]|^test[_.]")


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path.lower()))


def _extract_files_from_audit(audit: str) -> List[str]:
    """Best-effort: extract changed file paths from manager_change_audit text."""
    if not audit:
        return []
    seen: dict = {}

    def _add(path: str) -> None:
        path = path.strip()
        # git porcelain rename: "old -> new" — берём новый путь.
        if " -> " in path:
            path = path.split(" -> ")[-1].strip()
        if path and path not in seen:
            seen[path] = None

    for line in audit.splitlines():
        line = line.rstrip()
        m = _AUDIT_PORCELAIN_RE.match(line)
        if m:
            _add(m.group(1))
            continue
        m2 = _AUDIT_EXT_RE.search(line)
        if m2:
            _add(m2.group(0))
    return list(seen)[:30]


def parse_spec_requirements(spec_md: str) -> List[Dict[str, str]]:
    """Parse the ## Requirements section of spec.md and return [{id, text}, ...].

    Returns an empty list if the section is absent.
    """
    _REQ_LINE_RE = re.compile(r"^\s*-\s+\*\*(?P<id>REQ-\d+)\*\*:\s*(?P<text>.+)$")
    results: List[Dict[str, str]] = []
    in_section = False
    for line in spec_md.splitlines():
        stripped = line.strip()
        if re.match(r"^##\s+requirements\b", stripped, re.IGNORECASE):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", stripped):
            break
        if in_section:
            m = _REQ_LINE_RE.match(line)
            if m:
                results.append({"id": m.group("id"), "text": m.group("text").strip()})
    return results


def _parse_bullet_section(text: str, heading: str) -> List[str]:
    """Parse '- ' bullet items under a '## <heading>' markdown section. [] if absent."""
    items: List[str] = []
    in_section = False
    head_re = re.compile(r"^##\s+" + re.escape(heading) + r"\b", re.IGNORECASE)
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if head_re.match(stripped):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", stripped):
            break
        if in_section and stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def parse_out_of_scope(spec_md: str) -> List[str]:
    """Parse the ## Out of Scope section of spec.md. Returns [] if absent."""
    return _parse_bullet_section(spec_md, "out of scope")


def parse_affected_modules(plan_md: str) -> List[str]:
    """Parse the ## Affected Modules section of plan.md. Returns [] if absent."""
    return _parse_bullet_section(plan_md, "affected modules")


def _fill_prompt(template: Any, **fields: str) -> str:
    """Substitute {key} placeholders via str.replace (literal-safe, unlike str.format)."""
    out = str(template or "")
    for key, val in fields.items():
        out = out.replace("{" + key + "}", str(val or ""))
    return out


def render_trace_md(plan: ProjectPlan, requirements: List[Dict[str, str]]) -> str:
    """Build traceability matrix markdown: REQ → task(s) → status → files → tests."""
    lines = [
        "# Трассируемость требований",
        "",
        "| REQ | Требование | Задача | Статус | Изменённые файлы | Тесты |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def _esc(s: str) -> str:
        return str(s).replace("|", r"\|").replace("\n", "<br>").replace("\r", "")

    def _trunc(s: str, n: int = 80) -> str:
        return s if len(s) <= n else s[:n - 1] + "…"

    def _task_cells(task: DevTask) -> Tuple[str, str, str, str]:
        """Возвращает (task_cell, status_cell, files_cell, tests_cell) — все экранированы."""
        all_files = _extract_files_from_audit(task.manager_change_audit or "")
        test_files = [f for f in all_files if _is_test_file(f)]
        non_test_files = [f for f in all_files if not _is_test_file(f)]
        files_cell = "<br>".join(_esc(f) for f in non_test_files) if non_test_files else "—"
        tests_cell = "<br>".join(_esc(f) for f in test_files) if test_files else "—"
        task_cell = _esc(_trunc(f"{task.id}: {task.title}", 100))
        status_cell = _esc(task.status)
        return task_cell, status_cell, files_cell, tests_cell

    # Build REQ → tasks mapping
    req_to_tasks: Dict[str, List[DevTask]] = {}
    for req in requirements:
        req_to_tasks[req["id"]] = []
    for task in plan.tasks:
        for req_id in task.covers_requirements:
            if req_id not in req_to_tasks:
                req_to_tasks[req_id] = []
            req_to_tasks[req_id].append(task)

    # Rows for each known REQ
    covered_req_ids = {req["id"] for req in requirements}
    for req in requirements:
        req_id = _esc(req["id"])
        req_text = _esc(_trunc(req["text"]))
        tasks_for_req = req_to_tasks.get(req["id"], [])
        if not tasks_for_req:
            lines.append(f"| {req_id} | {req_text} | (не покрыто) | — | — | — |")
        else:
            for task in tasks_for_req:
                task_cell, status_cell, files_cell, tests_cell = _task_cells(task)
                lines.append(f"| {req_id} | {req_text} | {task_cell} | {status_cell} | {files_cell} | {tests_cell} |")

    # Tasks not covering any known REQ
    orphan_tasks = [t for t in plan.tasks if not any(r in covered_req_ids for r in t.covers_requirements)]
    if orphan_tasks:
        lines.append("")
        lines.append("## Задачи вне требований")
        lines.append("")
        lines.append("| Задача | Статус | Изменённые файлы | Тесты |")
        lines.append("| --- | --- | --- | --- |")
        for task in orphan_tasks:
            task_cell, status_cell, files_cell, tests_cell = _task_cells(task)
            lines.append(f"| {task_cell} | {status_cell} | {files_cell} | {tests_cell} |")

    lines.append("")
    return "\n".join(lines)


def analyze_coverage(requirements: List[Dict[str, str]], plan: ProjectPlan) -> Dict[str, Any]:
    """Deterministic REQ↔task coverage for the analyze phase (no LLM).

    A task is an orphan when none of its covers_requirements maps to a known REQ id
    (empty covers_requirements counts as orphan). Returns covered/uncovered REQ ids,
    orphan task ids, and totals.
    """
    req_ids = [str(r.get("id") or "") for r in (requirements or []) if str(r.get("id") or "")]
    req_set = set(req_ids)
    covered_set: set = set()
    orphan_task_ids: List[str] = []
    for task in plan.tasks:
        hit = [str(c) for c in (task.covers_requirements or []) if str(c) in req_set]
        if hit:
            covered_set.update(hit)
        else:
            orphan_task_ids.append(str(task.id))
    return {
        "covered": [r for r in req_ids if r in covered_set],
        "uncovered": [r for r in req_ids if r not in covered_set],
        "orphan_task_ids": orphan_task_ids,
        "total_reqs": len(req_ids),
        "total_tasks": len(plan.tasks),
    }


def render_analyze_md(report: Dict[str, Any], requirements: List[Dict[str, str]], plan: ProjectPlan) -> str:
    """Human-readable coverage report for the analyze gate (bulleted, no audit data)."""
    req_text = {str(r.get("id") or ""): str(r.get("text") or "") for r in (requirements or [])}
    task_title = {str(t.id): str(t.title) for t in plan.tasks}
    uncovered = list(report.get("uncovered") or [])
    orphans = list(report.get("orphan_task_ids") or [])
    lines = [
        "# Анализ покрытия",
        "",
        f"- Требований: {int(report.get('total_reqs') or 0)}",
        f"- Покрыто: {len(report.get('covered') or [])}",
        f"- Не покрыто: {len(uncovered)}",
        f"- Задач: {int(report.get('total_tasks') or 0)}",
        f"- Задач вне требований: {len(orphans)}",
        "",
    ]
    if uncovered:
        lines += ["## Непокрытые требования", ""]
        lines += [f"- {rid}: {req_text.get(rid, '')}" for rid in uncovered]
        lines.append("")
    if orphans:
        lines += ["## Задачи вне требований", ""]
        lines += [f"- {tid}: {task_title.get(tid, '')}" for tid in orphans]
        lines.append("")
    if not uncovered and not orphans:
        lines += ["✅ Все требования покрыты задачами; задач вне требований нет.", ""]
    return "\n".join(lines)


def parse_plan_decisions(plan_md: str) -> List[str]:
    """Extract key decisions from plan.md for the decisions.md ADR block:
    the Architecture paragraph (capped) plus each Constraints bullet.
    """
    decisions: List[str] = []
    arch_re = re.compile(r"^##\s+architecture\b", re.IGNORECASE)
    in_arch = False
    arch_lines: List[str] = []
    for line in str(plan_md or "").splitlines():
        stripped = line.strip()
        if arch_re.match(stripped):
            in_arch = True
            continue
        if in_arch:
            if re.match(r"^##\s+", stripped):
                break
            arch_lines.append(line)
    arch_text = " ".join(s.strip() for s in arch_lines if s.strip())
    if arch_text:
        decisions.append(arch_text[:300])
    decisions += _parse_bullet_section(plan_md, "constraints")
    return decisions


def _render_spec_md(payload: dict, intent: str) -> str:
    lines = [f"# Specification: {payload.get('feature_slug', '')}"]
    lines.append(f"\n**Intent:** {intent}\n")
    stories = payload.get("stories") or []
    if stories:
        lines.append("## User Stories\n")
        for s in stories:
            lines.append(f"- {s}")
        lines.append("")
    requirements = payload.get("requirements") or []
    if requirements:
        lines.append("## Requirements\n")
        for r in requirements:
            lines.append(f"- **{r['id']}**: {r['text']}")
        lines.append("")
    criteria = payload.get("acceptance_criteria") or []
    if criteria:
        lines.append("## Acceptance Criteria\n")
        for c in criteria:
            lines.append(f"- {c['req_id']}: {c['ears']}")
        lines.append("")
    out_of_scope = payload.get("out_of_scope") or []
    if out_of_scope:
        lines.append("## Out of Scope\n")
        for item in out_of_scope:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)


def _render_plan_md(payload: dict) -> str:
    lines = ["# Technical Plan\n"]
    lines.append(f"## Architecture\n\n{payload.get('architecture', '')}\n")
    stack = payload.get("stack") or []
    if stack:
        lines.append("## Stack\n")
        for s in stack:
            lines.append(f"- {s}")
        lines.append("")
    constraints = payload.get("constraints") or []
    if constraints:
        lines.append("## Constraints\n")
        for c in constraints:
            lines.append(f"- {c}")
        lines.append("")
    risks = payload.get("risks") or []
    if risks:
        lines.append("## Risks\n")
        for r in risks:
            lines.append(f"- {r}")
        lines.append("")
    affected = payload.get("affected_modules") or []
    if affected:
        lines.append("## Affected Modules\n")
        for m in affected:
            lines.append(f"- {m}")
        lines.append("")
    return "\n".join(lines)


def _parse_llm_json(raw: str, *, contract: str) -> dict:
    try:
        data = loads_safe(str(raw or ""), strict_first=False)
    except Exception:
        _log.exception("sdd json parse failed contract=%s", contract)
        raise RuntimeError(f"LLM returned invalid JSON for {contract}")
    if not isinstance(data, dict):
        raise RuntimeError(f"LLM returned non-object JSON for {contract}")
    return data


async def generate_spec(
    model: ModelCall,
    *,
    intent: str,
    constitution: str,
    prompts: dict,
    revision: str = "",
) -> Tuple[str, dict]:
    """Generate spec.md content. Returns (spec_md, payload)."""
    system = str(prompts.get("specify", "")).replace("{constitution}", constitution)
    user = f"Feature intent: {intent}"
    if revision:
        user += f"\n\nREVISION REQUEST FROM USER:\n{revision}"
    raw = await model(system, user)
    payload = _parse_llm_json(raw, contract="specify")
    validate_sdd_payload(payload, SPEC_OUTPUT_SCHEMA, contract="specify")
    spec_md = _render_spec_md(payload, intent)
    return spec_md, payload


async def generate_plan(
    cli_call: CliCall,
    *,
    spec_md: str,
    constitution: str,
    project_profile: str = "",
    relevant_nodes: str = "",
    out_of_scope: str = "",
    decisions: str = "",
    prompts: dict,
    revision: str = "",
) -> Tuple[str, dict]:
    """Generate plan.md via routed CLI (work_type=planning) with LLM-API fallback.

    *cli_call* already validates the payload against PLAN_OUTPUT_SCHEMA and encapsulates
    the fallback, so this function does not re-validate. Returns (plan_md, payload).
    """
    system = _fill_prompt(
        prompts.get("plan", ""),
        constitution=constitution,
        project_profile=project_profile,
        relevant_nodes=relevant_nodes,
        out_of_scope=out_of_scope,
        decisions=decisions,
    )
    user = f"Feature specification:\n\n{spec_md}"
    if revision:
        user += f"\n\nREVISION REQUEST FROM USER:\n{revision}"
    payload = await cli_call("planning", system, user, PLAN_OUTPUT_SCHEMA)
    plan_md = _render_plan_md(payload)
    return plan_md, payload


async def generate_tasks(
    cli_call: CliCall,
    *,
    spec_md: str,
    plan_md: str,
    constitution: str,
    project_profile: str = "",
    relevant_nodes: str = "",
    out_of_scope: str = "",
    decisions: str = "",
    prompts: dict,
    revision: str = "",
) -> ProjectPlan:
    """Generate tasks breakdown via routed CLI (work_type=planning) with LLM-API fallback.

    *cli_call* already validates against TASKS_OUTPUT_SCHEMA. Returns a ProjectPlan.
    """
    system = _fill_prompt(
        prompts.get("tasks", ""),
        constitution=constitution,
        project_profile=project_profile,
        relevant_nodes=relevant_nodes,
        out_of_scope=out_of_scope,
        decisions=decisions,
    )
    user = f"Feature specification:\n\n{spec_md}\n\nTechnical plan:\n\n{plan_md}"
    if revision:
        user += f"\n\nREVISION REQUEST FROM USER:\n{revision}"
    payload = await cli_call("planning", system, user, TASKS_OUTPUT_SCHEMA)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    tasks_raw = payload.get("tasks") or []
    dev_tasks = [
        DevTask(
            id=str(t.get("id", f"TASK-{i+1}")),
            title=str(t.get("title", "")),
            description=str(t.get("description", "")),
            acceptance_criteria=list(t.get("acceptance_criteria") or []),
            covers_requirements=list(t.get("covers_requirements") or []),
            depends_on=list(t.get("depends_on") or []),
            status="pending",
        )
        for i, t in enumerate(tasks_raw)
    ]
    return ProjectPlan(
        project_goal=str(payload.get("project_goal", "")),
        tasks=dev_tasks,
        status="active",
        created_at=now,
        updated_at=now,
    )
