from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from modes.codebase_mapper_constants import CODEBASE_MAPPER_GRAPH_STATE, CODEBASE_MAPPER_RESULT_STATUS
from modes.sdk.runtime.contracts import DevTask, ProjectPlan

from .artifacts import render_tasks_md
from .packs.detectors import meaningful_files
from .packs.registry import load_pack_registry, save_project_pack_index, write_pack_definition
from .packs.render import render_pack_manifest_md
from .packs.render import render_validation_md
from .packs.selector import select_packs
from .state import get_sdd_state

_IGNORED_ROOTS = {
    ".git",
    ".hg",
    ".svn",
    ".cli-proxy",
    "specs",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
}


@dataclass(frozen=True)
class ProjectClassification:
    kind: str
    meaningful_paths: List[str]

    @property
    def is_existing_codebase(self) -> bool:
        return self.kind == "existing_codebase"


@dataclass(frozen=True)
class ProjectInitResult:
    kind: str
    status: str
    created_files: List[str]
    project_profile_path: str = ""
    snapshot_path: str = ""
    pack_manifest_path: str = ""
    message: str = ""


def classify_project(workdir: str) -> ProjectClassification:
    root = Path(str(workdir or "").strip())
    if not root.exists() or not root.is_dir():
        raise ValueError("workdir_not_found")
    paths = [
        path for path in meaningful_files(str(root))
        if not _is_init_ignored_path(path)
    ]
    return ProjectClassification(
        kind="existing_codebase" if paths else "empty_repo",
        meaningful_paths=paths,
    )


async def run_project_initialization(
    *,
    session: Any,
    runtime_getter: Any,
    persist: Any,
) -> ProjectInitResult:
    sdd = get_sdd_state(session)
    workdir = str(getattr(session, "workdir", "") or "").strip()
    if not workdir or not os.path.isdir(workdir):
        raise ValueError("workdir_not_found")
    started_at = time.time()
    sdd.project_init_status = "running"
    sdd.project_init_step = "preflight"
    sdd.project_init_error = ""
    sdd.project_init_started_at = started_at
    sdd.project_init_finished_at = None
    _persist(persist)

    created_files: List[str] = []
    try:
        classification = classify_project(workdir)
        sdd.project_init_kind = classification.kind

        if classification.is_existing_codebase:
            sdd.project_init_step = "code_map"
            _persist(persist)
            mapper_context = await _prepare_code_map(
                session=session,
                workdir=workdir,
                runtime_getter=runtime_getter,
            )
            sdd.project_init_step = "snapshot"
            _persist(persist)
            snapshot_path = _write_snapshot(workdir=workdir, classification=classification)
            created_files.append(snapshot_path)
            sdd.project_init_snapshot_path = _relative(workdir, snapshot_path)
            _persist(persist)
            sdd.project_init_step = "packs"
            _persist(persist)
            pack_result = _select_and_persist_packs(
                workdir=workdir,
                codebase_context=mapper_context,
                created_files=created_files,
            )
            sdd.project_init_step = "artifacts"
            _persist(persist)
            profile_path = _write_project_artifacts(
                workdir=workdir,
                classification=classification,
                mapper_context=mapper_context,
                pack_manifest=pack_result,
                created_files=created_files,
            )
            sdd.project_profile_path = _relative(workdir, profile_path)
            status_msg = "project_profile_ready"
        else:
            sdd.project_init_step = "snapshot"
            _persist(persist)
            snapshot_path = _write_snapshot(workdir=workdir, classification=classification)
            created_files.append(snapshot_path)
            sdd.project_init_snapshot_path = _relative(workdir, snapshot_path)
            _persist(persist)
            sdd.project_init_step = "templates"
            _persist(persist)
            profile_path = _write_empty_templates(workdir=workdir, created_files=created_files)
            sdd.project_profile_path = _relative(workdir, profile_path)
            status_msg = "templates_ready"

        sdd.project_init_status = "done"
        sdd.project_init_step = "done"
        sdd.project_init_finished_at = time.time()
        _persist(persist)
        return ProjectInitResult(
            kind=classification.kind,
            status="done",
            created_files=created_files,
            project_profile_path=sdd.project_profile_path,
            snapshot_path=sdd.project_init_snapshot_path,
            message=status_msg,
        )
    except Exception as exc:
        sdd.project_init_status = "failed"
        sdd.project_init_step = "failed"
        sdd.project_init_error = str(exc or "project_init_failed")
        sdd.project_init_finished_at = time.time()
        _persist(persist)
        raise


async def _prepare_code_map(*, session: Any, workdir: str, runtime_getter: Any) -> str:
    if not callable(runtime_getter):
        raise RuntimeError("codebase_mapper_runtime_not_configured")
    mapper_status = runtime_getter("codebase_mapper_status")
    mapper_run = runtime_getter("codebase_mapper_run")
    mapper_context = runtime_getter("codebase_mapper_context")
    if mapper_status is None or mapper_run is None:
        raise RuntimeError("codebase_mapper_runtime_not_configured")
    before = mapper_status.get_status(workdir=workdir)
    operation = _choose_mapper_operation(before)
    defaults = getattr(getattr(session, "config", None), "defaults", None)
    usage = str(getattr(defaults, "codebase_mapper_usage", "auto") or "auto")
    result = await mapper_run.maybe_run(
        session=session,
        workdir=workdir,
        usage=usage,
        operation=operation,
        sync_agents=operation in {"init", "verify"},
    )
    status = str((result or {}).get("status") or "").strip()
    if status in {CODEBASE_MAPPER_RESULT_STATUS["FAILED"], CODEBASE_MAPPER_RESULT_STATUS["DISABLED"]}:
        reason = str((result or {}).get("reason") or status)
        raise RuntimeError(f"codebase_mapper_failed:{reason}")
    after = mapper_status.get_status(workdir=workdir)
    _assert_healthy_code_map(after)
    context = ""
    if mapper_context is not None and hasattr(mapper_context, "build_runtime_context"):
        context = str(mapper_context.build_runtime_context(workdir=workdir, max_chars=8000) or "")
    elif hasattr(mapper_status, "build_runtime_context"):
        context = str(mapper_status.build_runtime_context(workdir=workdir, max_chars=8000) or "")
    if not context.strip():
        raise RuntimeError("codebase_mapper_context_empty")
    return context


def _choose_mapper_operation(status: Dict[str, Any]) -> str:
    if not bool((status or {}).get("graph_initialized")):
        return "init"
    graph_state = str((status or {}).get("graph_state") or "").strip()
    if graph_state not in {
        CODEBASE_MAPPER_GRAPH_STATE["READY"],
        CODEBASE_MAPPER_GRAPH_STATE["VALIDATED"],
    }:
        return "verify"
    if (status or {}).get("repair_queue") or (status or {}).get("degraded_nodes"):
        return "verify"
    return "run"


def _assert_healthy_code_map(status: Dict[str, Any]) -> None:
    if str((status or {}).get("status") or "").strip() != CODEBASE_MAPPER_RESULT_STATUS["READY"]:
        raise RuntimeError(f"codebase_map_not_ready:{(status or {}).get('status')}")
    if not bool((status or {}).get("graph_initialized")):
        raise RuntimeError("codebase_map_graph_not_initialized")
    if not list((status or {}).get("docs") or []):
        raise RuntimeError("codebase_map_docs_empty")
    graph_state = str((status or {}).get("graph_state") or "").strip()
    if graph_state not in {
        CODEBASE_MAPPER_GRAPH_STATE["READY"],
        CODEBASE_MAPPER_GRAPH_STATE["VALIDATED"],
    }:
        raise RuntimeError(f"codebase_map_unhealthy:{graph_state}")
    if (status or {}).get("repair_queue"):
        raise RuntimeError("codebase_map_repair_queue_not_empty")
    if (status or {}).get("degraded_nodes"):
        raise RuntimeError("codebase_map_degraded_nodes")


def _select_and_persist_packs(*, workdir: str, codebase_context: str, created_files: List[str]) -> Dict[str, Any]:
    registry = load_pack_registry(workdir=workdir)
    selection = select_packs(registry=registry, workdir=workdir, codebase_context=codebase_context)
    if selection.status == "ambiguous":
        raise RuntimeError(f"pack_selection_ambiguous:{selection.reason or 'unknown'}")
    for pack in selection.proposed:
        created_files.append(write_pack_definition(workdir=workdir, pack=pack, lifecycle="proposed"))
        registry.add(pack)
    created_files.append(save_project_pack_index(workdir=workdir, packs=registry.all()))
    return selection.to_manifest()


def _write_project_artifacts(
    *,
    workdir: str,
    classification: ProjectClassification,
    mapper_context: str,
    pack_manifest: Dict[str, Any],
    created_files: List[str],
) -> str:
    out_dir = _safe_output_dir(workdir, "specs", "_project")
    profile_path = out_dir / "project-profile.generated.md"
    profile_text = _render_project_profile(
        classification=classification,
        mapper_context=mapper_context,
        pack_manifest=pack_manifest,
    )
    _write_text(profile_path, profile_text, overwrite=True, workdir=workdir)
    created_files.append(str(profile_path))
    manifest_path = out_dir / "pack_manifest.json"
    _write_json(manifest_path, pack_manifest, workdir=workdir)
    created_files.append(str(manifest_path))
    manifest_md_path = out_dir / "artifact-pack-manifest.md"
    _write_text(manifest_md_path, render_pack_manifest_md(pack_manifest), overwrite=True, workdir=workdir)
    created_files.append(str(manifest_md_path))
    init_path = out_dir / "project-init.json"
    _write_json(
        init_path,
        {
            "kind": classification.kind,
            "meaningful_files": classification.meaningful_paths[:200],
            "project_profile_path": _relative(workdir, str(profile_path)),
            "pack_manifest_path": _relative(workdir, str(manifest_path)),
        },
        workdir=workdir,
    )
    created_files.append(str(init_path))
    validation_path = out_dir / "validation.md"
    _write_text(validation_path, render_validation_md(pack_manifest), overwrite=True, workdir=workdir)
    created_files.append(str(validation_path))
    return str(profile_path)


def _write_empty_templates(*, workdir: str, created_files: List[str]) -> str:
    out_dir = _safe_output_dir(workdir, "specs", "_templates")
    templates = {
        "spec.md": (
            "# Feature Specification Template\n\n"
            "## Intent\n\n[Describe what should be built.]\n\n"
            "## Requirements\n\n- REQ-1: [Requirement]\n\n"
            "## Acceptance Criteria\n\n"
            "- WHEN [condition], THE SYSTEM SHALL [behavior].\n"
        ),
        "plan.md": (
            "# Technical Plan Template\n\n"
            "## Architecture\n\n[Describe the implementation approach.]\n\n"
            "## Stack\n\n[Technologies and constraints.]\n\n"
            "## Risks\n\n[Known risks and mitigations.]\n"
        ),
        "tasks.md": render_tasks_md(
            ProjectPlan(
                project_goal="[Describe what should be built.]",
                tasks=[
                    DevTask(
                        id="TASK-1",
                        title="[Implementation task]",
                        description="[Describe the implementation task.]",
                        acceptance_criteria=["WHEN [condition], THE SYSTEM SHALL [behavior]."],
                        covers_requirements=["REQ-1"],
                        depends_on=[],
                    )
                ],
                status="active",
            )
        ),
        "project-profile.generated.md": (
            "# Project Profile Template\n\n"
            "No codebase was detected. Fill this profile before starting SDD work.\n"
        ),
    }
    profile_path = ""
    for name, text in templates.items():
        path = out_dir / name
        _write_text(path, text, overwrite=False, workdir=workdir)
        created_files.append(str(path))
        if name == "project-profile.generated.md":
            profile_path = str(path)
    init_path = out_dir / "project-init.json"
    _write_json(
        init_path,
        {
            "kind": "empty_repo",
            "templates_dir": _relative(workdir, str(out_dir)),
            "project_profile_path": _relative(workdir, profile_path),
        },
        workdir=workdir,
    )
    created_files.append(str(init_path))
    return profile_path


def _write_snapshot(*, workdir: str, classification: ProjectClassification) -> str:
    out_dir = _safe_output_dir(workdir, "specs", "_project")
    path = out_dir / "project-init-snapshot.json"
    payload = {
        "kind": classification.kind,
        "meaningful_files": classification.meaningful_paths[:500],
        "git": _git_snapshot(workdir),
        "created_at": time.time(),
    }
    _write_json(path, payload, workdir=workdir)
    return str(path)


def _render_project_profile(
    *,
    classification: ProjectClassification,
    mapper_context: str,
    pack_manifest: Dict[str, Any],
) -> str:
    lines = [
        "# Project Profile",
        "",
        f"Project kind: `{classification.kind}`",
        "",
        "## Code Map Context",
        "",
        mapper_context.strip() or "_No code map context._",
        "",
        "## Selected Packs",
        "",
    ]
    selected = list(pack_manifest.get("selected") or [])
    for pack in selected:
        lines.append(f"- `{pack.get('pack_id')}` score `{pack.get('score')}`")
    proposed = list(pack_manifest.get("proposed") or [])
    if proposed:
        lines.extend(["", "## Proposed Packs", ""])
        for pack in proposed:
            lines.append(f"- `{pack.get('pack_id')}` requires confirmation before promotion")
    lines.extend(["", "## Open Questions", "", "- Confirm whether inferred packs match the project intent."])
    return "\n".join(lines).rstrip() + "\n"


def _safe_output_dir(workdir: str, *parts: str) -> Path:
    root = Path(workdir)
    root_resolved = root.resolve(strict=True)
    out_dir = root.joinpath(*parts)
    _assert_safe_child(root_resolved, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _assert_safe_child(root_resolved, out_dir)
    return out_dir


def _assert_safe_child(root_resolved: Path, path: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root_resolved)
    except Exception as exc:
        raise RuntimeError(f"unsafe_sdd_output_path:{path}") from exc


def _write_json(path: Path, payload: Dict[str, Any], *, workdir: str = "") -> None:
    root_resolved = Path(workdir).resolve(strict=True) if workdir else None
    if root_resolved is not None:
        _assert_safe_child(root_resolved, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if root_resolved is not None:
        _assert_safe_child(root_resolved, path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str, *, overwrite: bool, workdir: str = "") -> None:
    root_resolved = Path(workdir).resolve(strict=True) if workdir else None
    if root_resolved is not None:
        _assert_safe_child(root_resolved, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if root_resolved is not None:
        _assert_safe_child(root_resolved, path)
    if path.exists() and not overwrite:
        existing = path.read_text(encoding="utf-8")
        if existing != text:
            raise RuntimeError(f"template_conflict:{path}")
        return
    path.write_text(text, encoding="utf-8")


def _git_snapshot(workdir: str) -> Dict[str, Any]:
    return {
        "head": _git(["rev-parse", "HEAD"], workdir),
        "status_short": _git(["status", "--short"], workdir),
    }


def _git(args: List[str], workdir: str) -> str:
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=workdir,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return str(result.stdout or "").strip()


def _persist(persist: Any) -> None:
    if callable(persist):
        persist()


def _relative(workdir: str, path: str) -> str:
    try:
        return os.path.relpath(path, workdir)
    except Exception:
        return str(path or "")


def _is_init_ignored_path(path: str) -> bool:
    token = str(path or "").replace("\\", "/").strip("/")
    if not token:
        return True
    first = token.split("/", 1)[0]
    return first in _IGNORED_ROOTS
