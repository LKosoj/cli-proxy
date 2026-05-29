from __future__ import annotations

import asyncio
import ast
import concurrent.futures
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from modes.codebase_mapper_constants import CODEBASE_MAPPER_GRAPH_STATE, CODEBASE_MAPPER_RESULT_STATUS
from utils.paths import cli_proxy_artifact_path

_DOC_NAMES = (
    "STACK.md",
    "INTEGRATIONS.md",
    "ARCHITECTURE.md",
    "STRUCTURE.md",
    "CONVENTIONS.md",
    "TESTING.md",
    "CONCERNS.md",
)

_GRAPH_INDEX = "INDEX.md"
_GRAPH_NODES_DIR = "nodes"
_GRAPH_API_DIR = "api"
_GRAPH_FILE = "graph.json"
_GRAPH_RULES_FILE = "rules.yaml"
_GRAPH_STATE_FILE = "state.json"
_MAP_DIR_NAME = ".codebase_map"
_MAP_REL_ROOT = ".cli-proxy/.codebase_map"
_MAP_REL_ROOT_SLASH = f"{_MAP_REL_ROOT}/"
_LEGACY_MAP_REL_ROOT_SLASH = ".codebase_map/"
_CLI_PARALLELISM_LIMIT = 2
_SCAN_EXCLUDED_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
_PROMPT_CHANGED_CAP_FAST = 8
_PROMPT_INDEX_CAP_FAST = 24
_PROMPT_CHANGED_CAP_DEEP = 40
_PROMPT_INDEX_CAP_DEEP = 80
_PROMPT_CHANGED_CAP_DEFAULT = 20
_PROMPT_INDEX_CAP_DEFAULT = 40
_SOURCE_SAMPLES_MIN = 12
_SOURCE_SAMPLES_MAX = 24
_SOURCE_SAMPLES_MIN_DEEP = 16
_SOURCE_SAMPLES_MAX_DEEP = 40
_UNFINISHED_MARKER_REGEX = "|".join(("TO" "DO", "FIX" "ME", "HACK", "XXX"))
_UNFINISHED_MARKER_LABEL = "unfinished/HACK/XXX markers"

_FOCUS_TO_DOCS: Dict[str, tuple[str, ...]] = {
    "tech": ("STACK.md", "INTEGRATIONS.md"),
    "arch": ("ARCHITECTURE.md", "STRUCTURE.md"),
    "quality": ("CONVENTIONS.md", "TESTING.md"),
    "concerns": ("CONCERNS.md",),
}

DocRunner = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


@dataclass
class CodebaseMapResult:
    status: str
    mode: str
    reason: str = ""
    map_dir: str = ""
    meta_path: str = ""
    changed_files: List[str] = field(default_factory=list)
    updated_docs: List[str] = field(default_factory=list)
    graph_state: str = ""
    graph_nodes: int = 0
    graph_tree: List[str] = field(default_factory=list)
    operation: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": str(self.status or "").strip(),
            "mode": str(self.mode or "").strip(),
            "reason": str(self.reason or "").strip(),
            "map_dir": str(self.map_dir or "").strip(),
            "meta_path": str(self.meta_path or "").strip(),
            "changed_files": list(self.changed_files or []),
            "updated_docs": list(self.updated_docs or []),
            "graph_state": str(self.graph_state or "").strip(),
            "graph_nodes": int(self.graph_nodes or 0),
            "graph_tree": list(self.graph_tree or []),
            "operation": str(self.operation or "").strip(),
        }


@dataclass
class _RunPlan:
    run_mode: str
    changed_files: List[str]
    docs_to_update: Set[str]
    head_commit: str
    full_scan: bool
    file_markers: Optional[Dict[str, Any]] = None
    skip_reason: str = ""


class CodebaseMapperRuntime:
    """Mode-owned runtime for `.cli-proxy/.codebase_map/` generation and read-only context usage."""

    capabilities = frozenset({"codebase_mapper_run", "codebase_mapper_status", "codebase_mapper_context"})

    def __init__(self, mode: Any = None) -> None:
        self._log = logging.getLogger(__name__)
        self.mode = mode

    @property
    def run_artifacts(self) -> Any:
        return self.mode.get_service("run_artifacts") if self.mode else None

    @property
    def run_observability(self) -> Any:
        return self.mode.get_service("run_observability") if self.mode else None

    @property
    def run_doctor(self) -> Any:
        return self.mode.get_service("run_doctor") if self.mode else None

    @property
    def run_boundary_validation(self) -> Any:
        return self.mode.get_service("run_boundary_validation") if self.mode else None

    @staticmethod
    def _map_dir(root: str) -> str:
        return cli_proxy_artifact_path(str(root or ""), _MAP_DIR_NAME)

    @staticmethod
    def _graph_state_path(map_dir: str) -> str:
        root = str(map_dir or "").strip()
        if not root:
            return ""
        return os.path.join(root, _GRAPH_STATE_FILE)

    def _read_graph_state(self, map_dir: str) -> Dict[str, Any]:
        return self._read_json_file(self._graph_state_path(map_dir))

    def _write_graph_state(self, map_dir: str, state: Dict[str, Any]) -> None:
        path = self._graph_state_path(map_dir)
        if not path:
            return
        payload = dict(state) if isinstance(state, dict) else {}
        self._write_json_atomic(path, payload)

    def graph_state_path(self, *, workdir: str) -> str:
        return self._graph_state_path(self._map_dir(str(workdir or "").strip()))

    def read_graph_state(self, *, workdir: str) -> Dict[str, Any]:
        map_dir = self._map_dir(str(workdir or "").strip())
        return self._read_graph_state(map_dir)

    def write_graph_state(self, *, workdir: str, state: Dict[str, Any]) -> None:
        map_dir = self._map_dir(str(workdir or "").strip())
        self._write_graph_state(map_dir, state)

    @staticmethod
    def _is_map_relative_path(path: str) -> bool:
        p = str(path or "").replace("\\", "/").strip("/")
        return p.startswith(_MAP_REL_ROOT_SLASH) or p.startswith(_LEGACY_MAP_REL_ROOT_SLASH)

    def set_config(self, config: Any) -> None:
        _ = config

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities

    async def maybe_run(
        self,
        *,
        session: Any = None,
        workdir: str,
        usage: str,
        force: bool = False,
        prompt_templates: Optional[Dict[str, str]] = None,
        cli_runner: Optional[DocRunner] = None,
        operation: str = "run",
        sync_agents: bool = False,
    ) -> Dict[str, Any]:
        mode = self._normalize_mode(usage)
        if mode == "disabled":
            return CodebaseMapResult(status="disabled", mode=mode, reason="disabled_by_config").as_dict()
        operation_norm = self._normalize_operation(operation)
        root = str(workdir or "").strip()
        map_dir_before = self._map_dir(root) if root else ""
        had_graph_before = bool(
            map_dir_before
            and os.path.exists(os.path.join(map_dir_before, _GRAPH_INDEX))
            and os.path.exists(self._graph_state_path(map_dir_before))
        )

        run_handle = None
        if getattr(self, "run_artifacts", None) and session:
            run_handle = self.run_artifacts.start_run(
                session=session,
                mode_id="codebase_mapper",
                phase="operation",
                mode_context={
                    "operation": operation_norm,
                    "usage": usage,
                    "force": force,
                    "map_dir": map_dir_before,
                    "sync_agents": sync_agents,
                }
            )
            self.run_artifacts.save_plan(
                run_handle,
                {
                    "mode_id": "codebase_mapper",
                    "plan_kind": "mode_run",
                    "task_family": "codebase_mapper_operation",
                    "operation": operation_norm,
                    "usage": str(usage or ""),
                    "units": [
                        {
                            "id": f"mapper_{operation_norm}",
                            "phase": "operation",
                            "kind": operation_norm,
                        }
                    ],
                    "boundary_map": [{"phase": "operation", "validator": "codebase_mapper_operation"}],
                    "validation_contracts": ["codebase_mapper:operation"],
                    "expected_graph_artifacts": ["meta.json", "state.json", "graph.json", "INDEX.md"],
                },
            )
            self.run_artifacts.append_event(
                run_handle,
                {
                    "event_type": "codebase_mapper_operation_start",
                    "operation": operation_norm,
                    "usage": str(usage or ""),
                    "map_dir": map_dir_before,
                    "sync_agents": bool(sync_agents),
                },
            )
            if getattr(self, "run_observability", None):
                self.run_observability.record_unit_start(
                    run=run_handle,
                    unit_id=f"mapper_{operation_norm}",
                    phase="operation",
                )

        try:
            if operation_norm == "repair":
                validate_payload = await self._run_validate_operation(
                    root=root,
                    mode=mode,
                    cli_runner=cli_runner,
                    trigger_repair=False,
                )
                if str(validate_payload.get("status") or "").strip() == "failed":
                    if sync_agents:
                        self._sync_agents_md(root=root, map_dir=str(validate_payload.get("map_dir") or ""))
                    payload = validate_payload
                    return payload
                repair_payload = await self._run_repair_operation(
                    root=root,
                    mode=mode,
                    cli_runner=cli_runner,
                )
                repair_payload["validate_queue"] = list(validate_payload.get("validate_queue") or [])
                if sync_agents:
                    self._sync_agents_md(root=root, map_dir=str(repair_payload.get("map_dir") or ""))
                payload = repair_payload
                return payload
            if operation_norm == "validate":
                validate_payload = await self._run_validate_operation(
                    root=root,
                    mode=mode,
                    cli_runner=cli_runner,
                    trigger_repair=False,
                )
                if sync_agents:
                    self._sync_agents_md(root=root, map_dir=str(validate_payload.get("map_dir") or ""))
                payload = validate_payload
                return payload
            run_force = bool(force or operation_norm in {"init_full"})
            if cli_runner is None:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.run(workdir=str(workdir or ""), usage=mode, force=run_force)
                )
            else:
                result = await self._run_parallel_cli(
                    workdir=str(workdir or ""),
                    usage=mode,
                    force=run_force,
                    prompt_templates=dict(prompt_templates or {}),
                    cli_runner=cli_runner,
                    operation=operation_norm,
                )
            map_dir = str(result.map_dir or self._map_dir(root)).strip()
            reason = str(result.reason or "").strip().lower()
            skip_heavy_sync = bool(
                operation_norm == "run"
                and not run_force
                and reason in {"incremental", "no_git_incremental"}
                and not self._graph_keys_missing(map_dir=map_dir)
            )
            if skip_heavy_sync:
                graph_info = self._read_graph_info(map_dir=map_dir)
            else:
                if root and os.path.isdir(root):
                    snapshot = self._scan_workspace(root)
                else:
                    snapshot = {
                        "files": [],
                        "top_level": {},
                        "ext_counts": {},
                        "todo_count": 0,
                    }
                graph_info = self._sync_instruction_graph(
                    root=root,
                    map_dir=map_dir,
                    snapshot=snapshot,
                    changed_files=list(result.changed_files or []),
                    operation=operation_norm,
                    force=run_force,
                    assume_first_init=bool(operation_norm in {"init", "init_full"} and not had_graph_before),
                )
            is_deep_operation = operation_norm in {"verify", "init_full"}
            should_enrich_nodes = bool(
                cli_runner is not None
                and is_deep_operation
            )
            if should_enrich_nodes and cli_runner is not None:
                state = self._read_graph_state(map_dir)
                review_items = [str(x) for x in list(state.get("review_items") or []) if str(x).strip()]
                targets = review_items
                await self._enrich_graph_nodes_with_cli(
                    root=root,
                    map_dir=map_dir,
                    node_paths=targets,
                    changed_files=list(result.changed_files or []),
                    cli_runner=cli_runner,
                    max_items=120 if operation_norm == "init_full" else 80,
                )
            should_trigger_repair = bool(cli_runner is not None and is_deep_operation)
            if should_trigger_repair:
                validation_payload = await self._run_validate_operation(
                    root=root,
                    mode=mode,
                    cli_runner=cli_runner,
                    trigger_repair=True,
                )
                graph_info["state"] = str(validation_payload.get("graph_state") or graph_info.get("state") or "")
                graph_info["nodes_count"] = int(validation_payload.get("graph_nodes") or graph_info.get("nodes_count") or 0)
                graph_info["tree"] = list(validation_payload.get("graph_tree") or graph_info.get("tree") or [])
            if operation_norm == "verify" and cli_runner is not None:
                repaired_state = self._read_graph_state(map_dir)
                payload = result.as_dict()
                payload["operation"] = operation_norm
                payload["graph_state"] = str((repaired_state or {}).get("state") or str(graph_info.get("state") or ""))
                payload["graph_nodes"] = int(graph_info.get("nodes_count") or 0)
                payload["graph_tree"] = list(graph_info.get("tree") or [])
                payload["validate_queue"] = list((repaired_state or {}).get("validate_queue") or [])
                payload["repair_queue"] = list((repaired_state or {}).get("repair_queue") or [])
                if payload.get("status") == "skipped":
                    payload["status"] = CODEBASE_MAPPER_RESULT_STATUS["GRAPH_VERIFIED"]
                    payload["reason"] = "verify_completed"
                if sync_agents:
                    self._sync_agents_md(root=root, map_dir=map_dir)
                return payload
            if sync_agents and operation_norm in {"init", "init_full", "verify"}:
                self._sync_agents_md(root=root, map_dir=map_dir)
            payload = result.as_dict()
            payload["operation"] = operation_norm
            payload["graph_state"] = str(graph_info.get("state") or "")
            payload["graph_nodes"] = int(graph_info.get("nodes_count") or 0)
            payload["graph_tree"] = list(graph_info.get("tree") or [])
            if operation_norm == "verify" and payload.get("status") == "skipped":
                payload["status"] = CODEBASE_MAPPER_RESULT_STATUS["GRAPH_VERIFIED"]
                payload["reason"] = "verify_completed"
            return payload
        except Exception:
            self._log.exception("codebase map failed")
            payload = CodebaseMapResult(status="failed", mode=mode, reason="runtime_error").as_dict()
            return payload
        finally:
            if run_handle and getattr(self, "run_artifacts", None):
                try:
                    state = self.run_artifacts.load_state(run_handle)
                    ctx = dict(state.get("mode_context") or {})
                    status_val = str(locals().get("payload", {}).get("status") or "")
                    ctx["status"] = status_val

                    p_map_dir = str(locals().get("payload", {}).get("map_dir") or "")
                    if p_map_dir:
                        ctx["map_dir"] = p_map_dir
                        g_state = self._read_graph_state(p_map_dir)
                        ctx["needs_review"] = list(g_state.get("needs_review") or [])
                        ctx["validate_queue"] = list(g_state.get("validate_queue") or locals().get(
                            "payload", {}).get("validate_queue") or [])
                        ctx["repair_queue"] = list(g_state.get("repair_queue") or locals().get("payload", {}).get("repair_queue") or [])
                        ctx["graph_state"] = str(g_state.get("state") or locals().get("payload", {}).get("graph_state") or "")
                        ctx["graph_nodes"] = int(g_state.get("nodes_count") or locals().get("payload", {}).get("graph_nodes") or 0)

                    state["mode_context"] = ctx
                    if status_val == "failed":
                        state["status"] = "failed"
                    else:
                        state["status"] = "completed"

                    self.run_artifacts.save_state(run_handle, state)
                    final_status = str(state.get("status") or "completed")

                    boundary_report = None
                    if getattr(self, "run_boundary_validation", None):
                        boundary_report = self.run_boundary_validation.validate(
                            run_handle,
                            mode_id="codebase_mapper",
                            phase="operation",
                        )
                        if str(boundary_report.status or "") != "ok":
                            final_status = "failed"
                            state["status"] = final_status
                            ctx["boundary_validation"] = boundary_report.to_dict()
                            state["mode_context"] = ctx
                            self.run_artifacts.save_state(run_handle, state)

                    checkpoint = self.run_artifacts.append_checkpoint(
                        run_handle,
                        {
                            "phase": "operation",
                            "unit_id": f"mapper_{operation_norm}",
                            "status": "passed" if final_status == "completed" else "failed",
                            "started_at": state.get("started_at"),
                            "finished_at": dt.datetime.now(dt.timezone.utc).timestamp(),
                            "summary": str(locals().get("payload", {}).get("reason") or final_status),
                        },
                    )
                    state["checkpoint_index"] = int(checkpoint.get("index") or state.get("checkpoint_index") or 0)
                    if final_status == "completed":
                        state["last_successful_phase"] = "operation"
                    self.run_artifacts.save_state(run_handle, state)

                    if getattr(self, "run_observability", None):
                        self.run_observability.record_unit_end(
                            run=run_handle,
                            unit_id=f"mapper_{operation_norm}",
                            phase="operation",
                            status=final_status,
                        )
                    self.run_artifacts.append_event(
                        run_handle,
                        {
                            "event_type": "codebase_mapper_operation_end",
                            "operation": operation_norm,
                            "status": final_status,
                            "map_dir": str(ctx.get("map_dir") or ""),
                            "graph_state": str(ctx.get("graph_state") or ""),
                            "graph_nodes": int(ctx.get("graph_nodes") or 0),
                            "updated_docs": list(locals().get("payload", {}).get("changed_files") or []),
                        },
                    )
                    self.run_artifacts.mark_finished(run_handle, status=final_status, phase="operation")

                    doctor = getattr(self, "run_doctor", None)
                    if (
                        final_status != "completed"
                        and doctor is not None
                        and callable(getattr(doctor, "is_enabled", None))
                        and bool(doctor.is_enabled())
                    ):
                        doctor.diagnose(run_handle, mode_id="codebase_mapper", phase="operation")
                except Exception:
                    self._log.exception("codebase_mapper maybe_run run_artifacts finalize failed")

    def get_status(self, *, workdir: str) -> Dict[str, Any]:
        root = str(workdir or "").strip()
        if not root or not os.path.isdir(root):
            return {"status": "failed", "reason": "workdir_not_found"}
        map_dir = self._map_dir(root)
        meta_path = os.path.join(map_dir, "meta.json")
        meta = self._read_meta(meta_path)
        graph_json_path = os.path.join(map_dir, _GRAPH_FILE)
        graph_state = self._read_graph_state(map_dir)
        graph_json = self._read_json_file(graph_json_path)
        graph_tree = list((graph_state or {}).get("tree") or (graph_json or {}).get("tree") or [])
        graph_initialized = bool(os.path.exists(os.path.join(map_dir, _GRAPH_INDEX)))
        graph_nodes_count = int(len((graph_json or {}).get("nodes") or []))
        needs_review = list((graph_state or {}).get("needs_review") or [])
        reviewed = dict((graph_state or {}).get("reviewed") or {})
        review_items = list((graph_state or {}).get("review_items") or [])
        inferred_rules = list((graph_state or {}).get("inferred_rules") or [])
        active_rules = [r for r in inferred_rules if str((r or {}).get("status") or "").strip() == "active"]
        proposed_rules = [r for r in inferred_rules if str((r or {}).get("status") or "").strip() == "proposed"]
        rules_needs_review = list((graph_state or {}).get("rules_needs_review") or [])
        combined_needs_review = needs_review + [x for x in rules_needs_review if x not in needs_review]
        validate_queue = list((graph_state or {}).get("validate_queue") or [])
        repair_queue = list((graph_state or {}).get("repair_queue") or [])
        nodes_status = dict((graph_state or {}).get("nodes_status") or {})
        degraded_nodes = [k for k, v in nodes_status.items() if str((v or {}).get("status") or "") == "degraded"]
        nodes_status_counts = self._count_nodes_status(nodes_status)
        docs = []
        for name in _DOC_NAMES:
            p = os.path.join(map_dir, name)
            if os.path.exists(p):
                docs.append(name)
        if not docs:
            return {
                "status": "empty",
                "map_dir": map_dir,
                "meta_path": meta_path,
                "docs": [],
                "head_commit": str((meta or {}).get("head_commit") or "").strip(),
                "generated_at": str((meta or {}).get("generated_at") or "").strip(),
                "graph_initialized": graph_initialized,
                "graph_state": str((graph_state or {}).get("state") or "empty").strip(),
                "graph_nodes": graph_nodes_count,
                "graph_tree": graph_tree,
                "rules_needs_review": len(combined_needs_review),
                "needs_review_items": combined_needs_review[:20],
                "review_items": review_items,
                "reviewed_total": len([k for k, v in reviewed.items() if bool(v)]),
                "inferred_rules_total": len(inferred_rules),
                "inferred_rules_active": len(active_rules),
                "inferred_rules_proposed": len(proposed_rules),
                "inferred_rules_needs_review": rules_needs_review[:20],
                "validate_queue": validate_queue[:30],
                "repair_queue": repair_queue[:30],
                "degraded_nodes": degraded_nodes[:30],
                "nodes_status_counts": nodes_status_counts,
            }
        return {
            "status": CODEBASE_MAPPER_RESULT_STATUS["READY"],
            "map_dir": map_dir,
            "meta_path": meta_path,
            "docs": docs,
            "head_commit": str((meta or {}).get("head_commit") or "").strip(),
            "generated_at": str((meta or {}).get("generated_at") or "").strip(),
            "graph_initialized": graph_initialized,
            "graph_state": str(
                (graph_state or {}).get("state")
                or (
                    CODEBASE_MAPPER_GRAPH_STATE["READY"]
                    if graph_initialized
                    else CODEBASE_MAPPER_GRAPH_STATE["EMPTY"]
                )
            ).strip(),
            "graph_nodes": graph_nodes_count,
            "graph_tree": graph_tree,
            "rules_needs_review": len(combined_needs_review),
            "needs_review_items": combined_needs_review[:20],
            "review_items": review_items,
            "reviewed_total": len([k for k, v in reviewed.items() if bool(v)]),
            "inferred_rules_total": len(inferred_rules),
            "inferred_rules_active": len(active_rules),
            "inferred_rules_proposed": len(proposed_rules),
            "inferred_rules_needs_review": rules_needs_review[:20],
            "validate_queue": validate_queue[:30],
            "repair_queue": repair_queue[:30],
            "degraded_nodes": degraded_nodes[:30],
            "nodes_status_counts": nodes_status_counts,
        }

    def list_review_items(self, *, workdir: str) -> Dict[str, Any]:
        status = self.get_status(workdir=workdir)
        map_dir = str(status.get("map_dir") or "").strip()
        state = self._read_graph_state(map_dir)
        items = list(state.get("review_items") or status.get("review_items") or [])
        rule_items = [str(x) for x in list(state.get("rules_review_items") or []) if str(x).strip()]
        all_items = items + [x for x in rule_items if x not in items]
        needs_review = list(state.get("needs_review") or status.get("needs_review_items") or [])
        needs_review_rules = [str(x) for x in list(state.get("rules_needs_review") or []) if str(x).strip()]
        all_needs_review = needs_review + [x for x in needs_review_rules if x not in needs_review]
        reviewed = dict(state.get("reviewed") or {})
        reviewed_rules = dict(state.get("rules_reviewed") or {})
        reviewed_all = dict(reviewed)
        for key, value in reviewed_rules.items():
            reviewed_all[str(key)] = bool(value)
        return {
            "items": all_items,
            "needs_review": all_needs_review,
            "reviewed": reviewed_all,
            "map_dir": map_dir,
        }

    def confirm_review_item(self, *, workdir: str, item: str) -> Dict[str, Any]:
        status = self.get_status(workdir=workdir)
        map_dir = str(status.get("map_dir") or "").strip()
        if not map_dir:
            return {"ok": False, "reason": "map_dir_not_found"}
        state = self._read_graph_state(map_dir)
        review_items = [str(x) for x in list(state.get("review_items") or [])]
        rules_review_items = [str(x) for x in list(state.get("rules_review_items") or [])]
        target = str(item or "").strip()
        if not target:
            return {"ok": False, "reason": "empty_item"}
        if target not in review_items and target not in rules_review_items:
            return {"ok": False, "reason": "item_not_found"}
        reviewed = dict(state.get("reviewed") or {})
        rules_reviewed = dict(state.get("rules_reviewed") or {})
        needs_review = [x for x in list(state.get("needs_review") or []) if x != target]
        rules_needs_review = [x for x in list(state.get("rules_needs_review") or []) if x != target]
        inferred_rules = list(state.get("inferred_rules") or [])
        if target in review_items:
            reviewed[target] = True
        if target in rules_review_items:
            rules_reviewed[target] = True
            updated_rules: List[Dict[str, Any]] = []
            for rule in inferred_rules:
                cur = dict(rule or {})
                if str(cur.get("id") or "") == target:
                    cur["status"] = "active"
                    cur["needs_review"] = False
                updated_rules.append(cur)
            inferred_rules = updated_rules
        all_needs_review = needs_review + [x for x in rules_needs_review if x not in needs_review]
        state["reviewed"] = reviewed
        state["rules_reviewed"] = rules_reviewed
        state["needs_review"] = needs_review
        state["rules_needs_review"] = rules_needs_review
        state["inferred_rules"] = inferred_rules
        repair_queue = [str(x) for x in list(state.get("repair_queue") or []) if str(x).strip()]
        state["state"] = (
            CODEBASE_MAPPER_GRAPH_STATE["READY"]
            if not all_needs_review and not repair_queue
            else CODEBASE_MAPPER_GRAPH_STATE["NEEDS_REVIEW"]
        )
        state["updated_at"] = self._utc_now_iso()
        self._write_graph_state(map_dir, state)
        return {
            "ok": True,
            "remaining": len(all_needs_review),
            "state": str(state.get("state") or ""),
        }

    def build_runtime_context(self, *, workdir: str, max_chars: int = 2000) -> str:
        status = self.get_status(workdir=workdir)
        if str(status.get("status") or "").strip() != CODEBASE_MAPPER_RESULT_STATUS["READY"]:
            return ""
        map_dir = str(status.get("map_dir") or "").strip()
        docs = list(status.get("docs") or [])
        if not map_dir or not docs:
            return ""

        preferred_order = [
            "ARCHITECTURE.md",
            "STACK.md",
            "INTEGRATIONS.md",
            "CONVENTIONS.md",
            "TESTING.md",
            "CONCERNS.md",
            "STRUCTURE.md",
        ]
        ordered_docs: List[str] = [d for d in preferred_order if d in docs] + [d for d in docs if d not in preferred_order]
        total_budget = max(500, int(max_chars or 2000))
        per_doc_budget = max(120, total_budget // max(1, len(ordered_docs)))

        parts: List[str] = []
        generated_at = str(status.get("generated_at") or "").strip()
        if generated_at:
            parts.append(f"Codebase map generated at: {generated_at}")
        head_commit = str(status.get("head_commit") or "").strip()
        if head_commit:
            parts.append(f"Codebase map commit: {head_commit}")

        for name in ordered_docs:
            p = os.path.join(map_dir, name)
            if not os.path.exists(p):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read().strip()
            except Exception:
                self._log.exception("codebase map: failed to read doc %s", p)
                continue
            if not content:
                continue
            snippet = content[:per_doc_budget]
            if len(content) > per_doc_budget:
                snippet += "\n...[truncated]"
            parts.append(f"[{name}]\n{snippet}")
            joined = "\n\n".join(parts)
            if len(joined) >= total_budget:
                return joined[:total_budget]

        return "\n\n".join(parts)[:total_budget]

    def run(self, *, workdir: str, usage: str, force: bool = False) -> CodebaseMapResult:
        """
        Local fallback mode for tests/offline execution when no CLI runner is provided.
        """
        mode = self._normalize_mode(usage)
        if mode == "disabled":
            return CodebaseMapResult(status="disabled", mode=mode, reason="disabled_by_config")

        root = str(workdir or "").strip()
        if not root or not os.path.isdir(root):
            return CodebaseMapResult(status="failed", mode=mode, reason="workdir_not_found")

        map_dir = self._map_dir(root)
        meta_path = os.path.join(map_dir, "meta.json")
        os.makedirs(map_dir, exist_ok=True)

        plan = self._build_plan(root=root, map_dir=map_dir, meta_path=meta_path, force=force)
        if plan.skip_reason:
            graph_info = self._read_graph_info(map_dir=map_dir)
            if self._graph_keys_missing(map_dir=map_dir):
                graph_info = self._sync_instruction_graph(
                    root=root,
                    map_dir=map_dir,
                    snapshot=self._scan_workspace(root),
                    changed_files=[],
                    operation="run",
                    force=False,
                )
            return CodebaseMapResult(
                status="skipped",
                mode=mode,
                reason=plan.skip_reason,
                map_dir=map_dir,
                meta_path=meta_path,
                changed_files=plan.changed_files,
                updated_docs=[],
                graph_state=str(graph_info.get("state") or ""),
                graph_nodes=int(graph_info.get("nodes_count") or 0),
                graph_tree=list(graph_info.get("tree") or []),
                operation="run",
            )

        snapshot = self._scan_workspace_for_docs(root, plan.docs_to_update)
        generated: Dict[str, str] = {
            "STACK.md": self._render_stack(root, snapshot),
            "INTEGRATIONS.md": self._render_integrations(root, snapshot),
            "ARCHITECTURE.md": self._render_architecture(root, snapshot),
            "STRUCTURE.md": self._render_structure(root, snapshot),
            "CONVENTIONS.md": self._render_conventions(root, snapshot),
            "TESTING.md": self._render_testing(root, snapshot),
            "CONCERNS.md": self._render_concerns(root, snapshot),
        }

        updated_docs: List[str] = []
        for name in _DOC_NAMES:
            if name not in plan.docs_to_update:
                continue
            out_path = os.path.join(map_dir, name)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(generated[name])
            updated_docs.append(name)
        self._sanitize_concerns_doc(map_dir=map_dir)

        self._write_meta(
            meta_path,
            head_commit=plan.head_commit,
            changed_files=plan.changed_files,
            run_mode=plan.run_mode,
            full_scan=plan.full_scan,
            docs_updated=updated_docs,
            file_markers=plan.file_markers,
        )
        if plan.run_mode == "incremental" and not force and not self._graph_keys_missing(map_dir=map_dir):
            graph_info = self._read_graph_info(map_dir=map_dir)
        else:
            graph_info = self._sync_instruction_graph(
                root=root,
                map_dir=map_dir,
                snapshot=snapshot,
                changed_files=plan.changed_files,
                operation="run",
                force=bool(force),
            )
        status = (
            CODEBASE_MAPPER_RESULT_STATUS["FULL_UPDATED"]
            if len(updated_docs) == len(_DOC_NAMES)
            else CODEBASE_MAPPER_RESULT_STATUS["PARTIAL_UPDATED"]
        )
        if plan.run_mode in {"bootstrap", "force", "no_git"}:
            status = CODEBASE_MAPPER_RESULT_STATUS["FULL_UPDATED"]

        return CodebaseMapResult(
            status=status,
            mode=mode,
            reason=plan.run_mode,
            map_dir=map_dir,
            meta_path=meta_path,
            changed_files=plan.changed_files,
            updated_docs=updated_docs,
            graph_state=str(graph_info.get("state") or ""),
            graph_nodes=int(graph_info.get("nodes_count") or 0),
            graph_tree=list(graph_info.get("tree") or []),
            operation="run",
        )

    async def _run_parallel_cli(
        self,
        *,
        workdir: str,
        usage: str,
        force: bool,
        prompt_templates: Dict[str, str],
        cli_runner: DocRunner,
        operation: str,
    ) -> CodebaseMapResult:
        mode = self._normalize_mode(usage)
        if mode == "disabled":
            return CodebaseMapResult(status="disabled", mode=mode, reason="disabled_by_config")

        root = str(workdir or "").strip()
        if not root or not os.path.isdir(root):
            return CodebaseMapResult(status="failed", mode=mode, reason="workdir_not_found")

        map_dir = self._map_dir(root)
        meta_path = os.path.join(map_dir, "meta.json")
        os.makedirs(map_dir, exist_ok=True)

        plan = self._build_plan(root=root, map_dir=map_dir, meta_path=meta_path, force=force)
        if plan.skip_reason:
            graph_info = self._read_graph_info(map_dir=map_dir)
            if self._graph_keys_missing(map_dir=map_dir):
                graph_info = self._sync_instruction_graph(
                    root=root,
                    map_dir=map_dir,
                    snapshot=self._scan_workspace(root),
                    changed_files=[],
                    operation="run",
                    force=False,
                )
            return CodebaseMapResult(
                status="skipped",
                mode=mode,
                reason=plan.skip_reason,
                map_dir=map_dir,
                meta_path=meta_path,
                changed_files=plan.changed_files,
                updated_docs=[],
                graph_state=str(graph_info.get("state") or ""),
                graph_nodes=int(graph_info.get("nodes_count") or 0),
                graph_tree=list(graph_info.get("tree") or []),
                operation="run",
            )

        focuses = self._focuses_for_docs(plan.docs_to_update)
        file_index = self._list_files(root) if plan.full_scan else []
        tasks: List[Dict[str, Any]] = []
        for focus in focuses:
            docs = list(_FOCUS_TO_DOCS.get(focus, ()))
            target_docs = [d for d in docs if d in plan.docs_to_update]
            if not target_docs:
                continue
            prompt = self._build_focus_prompt(
                root=root,
                map_dir=map_dir,
                focus=focus,
                target_docs=target_docs,
                full_scan=plan.full_scan,
                changed_files=plan.changed_files,
                file_index=file_index,
                templates=prompt_templates,
                operation=operation,
            )
            tasks.append(
                {
                    "focus": focus,
                    "target_docs": target_docs,
                    "prompt": prompt,
                    "map_dir": map_dir,
                    "full_scan": plan.full_scan,
                    "changed_files": list(plan.changed_files),
                }
            )

        sem = asyncio.Semaphore(_CLI_PARALLELISM_LIMIT)

        async def _run_task(task: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                return await cli_runner(task)

        results = await asyncio.gather(*[_run_task(task) for task in tasks], return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self._log.exception("mapper cli focus failed focus=%s", tasks[i].get("focus"))
                raise result
            if not isinstance(result, dict):
                self._log.warning("mapper cli focus returned non-dict focus=%s", tasks[i].get("focus"))

        updated_docs: List[str] = []
        for doc in _DOC_NAMES:
            if doc not in plan.docs_to_update:
                continue
            p = os.path.join(map_dir, doc)
            if os.path.exists(p):
                try:
                    size = os.path.getsize(p)
                except Exception:
                    size = 0
                if size > 0:
                    updated_docs.append(doc)

        missing_docs = [d for d in plan.docs_to_update if d not in set(updated_docs)]
        if missing_docs:
            self._log.warning("mapper cli did not update docs=%s; fallback local render", ", ".join(sorted(missing_docs)))
            snapshot = self._scan_workspace_for_docs(root, set(missing_docs))
            generated: Dict[str, str] = {
                "STACK.md": self._render_stack(root, snapshot),
                "INTEGRATIONS.md": self._render_integrations(root, snapshot),
                "ARCHITECTURE.md": self._render_architecture(root, snapshot),
                "STRUCTURE.md": self._render_structure(root, snapshot),
                "CONVENTIONS.md": self._render_conventions(root, snapshot),
                "TESTING.md": self._render_testing(root, snapshot),
                "CONCERNS.md": self._render_concerns(root, snapshot),
            }
            for doc in sorted(missing_docs):
                out_path = os.path.join(map_dir, doc)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(generated[doc])
                if doc not in updated_docs:
                    updated_docs.append(doc)
        self._sanitize_concerns_doc(map_dir=map_dir)

        self._write_meta(
            meta_path,
            head_commit=plan.head_commit,
            changed_files=plan.changed_files,
            run_mode=plan.run_mode,
            full_scan=plan.full_scan,
            docs_updated=updated_docs,
            file_markers=plan.file_markers,
        )
        if plan.run_mode == "incremental" and not force and not self._graph_keys_missing(map_dir=map_dir):
            graph_info = self._read_graph_info(map_dir=map_dir)
        else:
            graph_snapshot = self._scan_workspace(root)
            graph_info = self._sync_instruction_graph(
                root=root,
                map_dir=map_dir,
                snapshot=graph_snapshot,
                changed_files=plan.changed_files,
                operation="run",
                force=bool(force),
            )
        status = (
            CODEBASE_MAPPER_RESULT_STATUS["FULL_UPDATED"]
            if len(updated_docs) == len(_DOC_NAMES)
            else CODEBASE_MAPPER_RESULT_STATUS["PARTIAL_UPDATED"]
        )
        if plan.run_mode in {"bootstrap", "force", "no_git"}:
            status = CODEBASE_MAPPER_RESULT_STATUS["FULL_UPDATED"]

        return CodebaseMapResult(
            status=status,
            mode=mode,
            reason=plan.run_mode,
            map_dir=map_dir,
            meta_path=meta_path,
            changed_files=plan.changed_files,
            updated_docs=updated_docs,
            graph_state=str(graph_info.get("state") or ""),
            graph_nodes=int(graph_info.get("nodes_count") or 0),
            graph_tree=list(graph_info.get("tree") or []),
            operation="run",
        )

    def _build_plan(self, *, root: str, map_dir: str, meta_path: str, force: bool) -> _RunPlan:
        meta = self._read_meta(meta_path)
        git = self._git_info(root)
        head = str(git.get("head") or "").strip()

        missing_docs = [name for name in _DOC_NAMES if not os.path.exists(os.path.join(map_dir, name))]
        baseline = str((meta or {}).get("head_commit") or "").strip()

        changed_files: List[str] = []
        docs_to_update: Set[str] = set()
        run_mode = "incremental"
        full_scan = False

        if force:
            run_mode = "force"
            full_scan = True
            docs_to_update = set(_DOC_NAMES)
        elif missing_docs:
            run_mode = "bootstrap"
            full_scan = True
            docs_to_update = set(_DOC_NAMES)
            if not git.get("ok"):
                _changed, current_markers = self._manifest_changed_files(
                    root=root,
                    previous_markers=self._extract_file_markers(meta),
                )
                return _RunPlan(
                    run_mode=run_mode,
                    changed_files=[],
                    docs_to_update=docs_to_update,
                    head_commit=head,
                    full_scan=full_scan,
                    file_markers=current_markers,
                )
        elif not git.get("ok"):
            prev_markers = self._extract_file_markers(meta)
            changed_files, current_markers = self._manifest_changed_files(root=root, previous_markers=prev_markers)
            changed_files = self._filter_changed_files(changed_files)
            if not prev_markers:
                run_mode = "bootstrap"
                full_scan = True
                docs_to_update = set(_DOC_NAMES)
            elif not changed_files:
                self._write_meta(
                    meta_path,
                    head_commit=head,
                    changed_files=[],
                    run_mode="no_git_incremental",
                    full_scan=False,
                    docs_updated=[],
                    file_markers=current_markers,
                )
                return _RunPlan(
                    run_mode="no_git_incremental",
                    changed_files=[],
                    docs_to_update=set(),
                    head_commit=head,
                    full_scan=False,
                    file_markers=current_markers,
                    skip_reason="manifest_diff_empty",
                )
            else:
                run_mode = "no_git_incremental"
                full_scan = False
                docs_to_update = self._docs_for_changes(changed_files)
                if not docs_to_update:
                    docs_to_update = {"CONCERNS.md"}
            return _RunPlan(
                run_mode=run_mode,
                changed_files=changed_files,
                docs_to_update=docs_to_update,
                head_commit=head,
                full_scan=full_scan,
                file_markers=current_markers,
            )
        elif not baseline:
            run_mode = "bootstrap"
            full_scan = True
            docs_to_update = set(_DOC_NAMES)
        elif baseline == head:
            return _RunPlan(
                run_mode=run_mode,
                changed_files=[],
                docs_to_update=set(),
                head_commit=head,
                full_scan=False,
                skip_reason="up_to_date",
            )
        else:
            changed_files = self._filter_changed_files(self._git_changed_files(root, baseline, head))
            if not changed_files:
                self._write_meta(
                    meta_path,
                    head_commit=head,
                    changed_files=[],
                    run_mode="incremental",
                    full_scan=False,
                    docs_updated=[],
                )
                return _RunPlan(
                    run_mode=run_mode,
                    changed_files=[],
                    docs_to_update=set(),
                    head_commit=head,
                    full_scan=False,
                    skip_reason="git_diff_empty",
                )
            docs_to_update = self._docs_for_changes(changed_files)
            if not docs_to_update:
                docs_to_update = {"CONCERNS.md"}

        return _RunPlan(
            run_mode=run_mode,
            changed_files=changed_files,
            docs_to_update=docs_to_update,
            head_commit=head,
            full_scan=full_scan,
        )

    @staticmethod
    def _extract_file_markers(meta: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        raw = dict((meta or {}).get("file_markers") or {})
        out: Dict[str, Dict[str, Any]] = {}
        for key, payload in raw.items():
            path = str(key or "").replace("\\", "/").strip()
            if not path or CodebaseMapperRuntime._is_map_relative_path(path):
                continue
            if not isinstance(payload, dict):
                continue
            out[path] = {
                "size": int(payload.get("size") or 0),
                "mtime_ns": int(payload.get("mtime_ns") or 0),
                "hash": str(payload.get("hash") or "").strip(),
            }
        return out

    def _manifest_changed_files(
        self,
        *,
        root: str,
        previous_markers: Dict[str, Dict[str, Any]],
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        files = self._list_files(root)
        current_basic: Dict[str, Dict[str, Any]] = {}
        for rel in files:
            abs_path = os.path.join(root, rel)
            try:
                stat = os.stat(abs_path)
            except Exception:
                continue
            current_basic[rel] = {
                "size": int(getattr(stat, "st_size", 0) or 0),
                "mtime_ns": int(getattr(stat, "st_mtime_ns", 0) or 0),
            }

        changed: Set[str] = set()
        previous_paths = set(previous_markers.keys())
        current_paths = set(current_basic.keys())
        changed.update(previous_paths - current_paths)

        candidates = {
            p for p in current_paths
            if p not in previous_paths
            or int(current_basic[p].get("size") or 0) != int((previous_markers.get(p) or {}).get("size") or 0)
            or int(current_basic[p].get("mtime_ns") or 0) != int((previous_markers.get(p) or {}).get("mtime_ns") or 0)
        }
        hashes = self._compute_hashes(root=root, paths=sorted(candidates))

        current_markers: Dict[str, Dict[str, Any]] = {}
        for path in files:
            base = dict(current_basic.get(path) or {})
            marker = {
                "size": int(base.get("size") or 0),
                "mtime_ns": int(base.get("mtime_ns") or 0),
            }
            if path in hashes:
                marker["hash"] = str(hashes[path] or "")
            else:
                prev_hash = str((previous_markers.get(path) or {}).get("hash") or "").strip()
                if prev_hash:
                    marker["hash"] = prev_hash
            current_markers[path] = marker

        for path in candidates:
            prev = dict(previous_markers.get(path) or {})
            if not prev:
                changed.add(path)
                continue
            cur_hash = str(hashes.get(path) or "").strip()
            prev_hash = str(prev.get("hash") or "").strip()
            if not prev_hash or not cur_hash or cur_hash != prev_hash:
                changed.add(path)

        return sorted(changed), current_markers

    @staticmethod
    def _compute_hashes(*, root: str, paths: Sequence[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for rel in list(paths or []):
            abs_path = os.path.join(root, rel)
            if not os.path.exists(abs_path):
                continue
            try:
                h = hashlib.blake2b(digest_size=16)
                with open(abs_path, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                out[rel] = h.hexdigest()
            except Exception:
                continue
        return out

    @staticmethod
    def _filter_changed_files(changed_files: Sequence[str]) -> List[str]:
        out: List[str] = []
        for raw in list(changed_files or []):
            p = str(raw or "").replace("\\", "/").strip()
            if p.startswith("./"):
                p = p[2:]
            if not p:
                continue
            if CodebaseMapperRuntime._is_map_relative_path(p):
                continue
            out.append(p)
        return out

    @staticmethod
    def _graph_keys_missing(*, map_dir: str) -> bool:
        root = str(map_dir or "").strip()
        if not root:
            return True
        return not (
            os.path.exists(os.path.join(root, _GRAPH_INDEX))
            and os.path.exists(CodebaseMapperRuntime._graph_state_path(root))
        )

    def _read_graph_info(self, *, map_dir: str) -> Dict[str, Any]:
        graph_state = self._read_graph_state(map_dir)
        graph_json = self._read_json_file(os.path.join(map_dir, _GRAPH_FILE))
        tree = list((graph_state or {}).get("tree") or (graph_json or {}).get("tree") or [])
        if tree:
            return {
                "state": str((graph_state or {}).get("state") or "").strip(),
                "nodes_count": int((graph_state or {}).get("nodes_count") or len((graph_json or {}).get("nodes") or [])),
                "tree": tree,
            }
        return {
            "state": str((graph_state or {}).get("state") or "").strip(),
            "nodes_count": int((graph_state or {}).get("nodes_count") or len((graph_json or {}).get("nodes") or [])),
            "tree": [],
        }

    def _focuses_for_docs(self, docs: Set[str]) -> List[str]:
        out: List[str] = []
        for focus, focus_docs in _FOCUS_TO_DOCS.items():
            if any(doc in docs for doc in focus_docs):
                out.append(focus)
        return out

    @staticmethod
    def _normalize_mode(raw: str) -> str:
        s = str(raw or "auto").strip().lower()
        if s == "disabled":
            return "disabled"
        if s in {"enabled", "auto"}:
            return "enabled"
        return "enabled"

    @staticmethod
    def _utc_now_iso() -> str:
        return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _read_meta(self, meta_path: str) -> Dict[str, Any]:
        if not os.path.exists(meta_path):
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            self._log.exception("codebase map: failed to read meta %s", meta_path)
            return {}

    def _read_json_file(self, path: str) -> Dict[str, Any]:
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            self._log.exception("codebase map: failed to read json %s", path)
            return {}

    def _write_meta(
        self,
        meta_path: str,
        *,
        head_commit: str,
        changed_files: List[str],
        run_mode: str,
        full_scan: bool,
        docs_updated: Sequence[str],
        file_markers: Optional[Dict[str, Any]] = None,
    ) -> None:
        existing = self._read_meta(meta_path)
        markers_payload: Dict[str, Any] = {}
        if isinstance(file_markers, dict):
            markers_payload = dict(file_markers)
        else:
            prev = (existing or {}).get("file_markers")
            if isinstance(prev, dict):
                markers_payload = dict(prev)
        payload = {
            "generated_at": self._utc_now_iso(),
            "head_commit": str(head_commit or "").strip(),
            "changed_files": list(changed_files or []),
            "docs": list(_DOC_NAMES),
            "run_mode": str(run_mode or "").strip(),
            "full_scan": bool(full_scan),
            "updated_docs": list(docs_updated or []),
            "file_markers": markers_payload,
        }
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, meta_path)

    def _git_info(self, workdir: str) -> Dict[str, Any]:
        root_proc = self._run_cmd(["git", "-C", workdir, "rev-parse", "--show-toplevel"])
        if root_proc.returncode != 0:
            return {"ok": False, "root": "", "head": ""}
        repo_root = str(root_proc.stdout or "").strip()
        head_proc = self._run_cmd(["git", "-C", workdir, "rev-parse", "HEAD"])
        head = str(head_proc.stdout or "").strip() if head_proc.returncode == 0 else ""
        return {"ok": True, "root": repo_root, "head": head}

    def _git_changed_files(self, workdir: str, base: str, head: str) -> List[str]:
        if not base or not head:
            return []
        proc = self._run_cmd(["git", "-C", workdir, "diff", "--name-only", f"{base}..{head}"])
        if proc.returncode != 0:
            return []
        out = []
        for line in (proc.stdout or "").splitlines():
            p = str(line or "").strip()
            if p:
                out.append(p)
        return out

    def _docs_for_changes(self, changed_files: Iterable[str]) -> Set[str]:
        out: Set[str] = set()
        for raw in changed_files:
            p = str(raw or "").replace("\\", "/").lower()
            base = p.split("/")[-1]
            if base in {
                "requirements.txt",
                "pyproject.toml",
                "poetry.lock",
                "package.json",
                "package-lock.json",
                "pnpm-lock.yaml",
                "yarn.lock",
                "dockerfile",
                "docker-compose.yml",
                "docker-compose.yaml",
            }:
                out.update({"STACK.md", "INTEGRATIONS.md", "CONCERNS.md"})
            if p.startswith("tests/") or p.startswith("test/"):
                out.update({"TESTING.md", "CONVENTIONS.md", "CONCERNS.md"})
            if p.startswith("docs/") or p.endswith("readme.md"):
                out.update({"STRUCTURE.md", "CONVENTIONS.md"})
            if p.startswith("modes/") or p.startswith("agent/") or p.startswith("app/"):
                out.update({"ARCHITECTURE.md", "STRUCTURE.md", "CONCERNS.md"})
            if p.endswith(".py") or p.endswith(".ts") or p.endswith(".tsx") or p.endswith(".js"):
                out.update({"ARCHITECTURE.md", "CONVENTIONS.md", "CONCERNS.md"})
        return out

    def _build_focus_prompt(
        self,
        *,
        root: str,
        map_dir: str,
        focus: str,
        target_docs: Sequence[str],
        full_scan: bool,
        changed_files: Sequence[str],
        file_index: Sequence[str],
        templates: Dict[str, str],
        operation: str,
    ) -> str:
        changed_cap, index_cap = self._prompt_caps(operation=operation)
        docs_list = "\n".join(f"- {d}" for d in target_docs)
        mode_label = "полный скан" if full_scan else "инкрементальный апдейт"
        changed_block = "\n".join(f"- `{p}`" for p in changed_files[:changed_cap])
        if not changed_block:
            changed_block = "- (нет списка изменений)"
        index_block = "\n".join(f"- `{p}`" for p in file_index[:index_cap]) if full_scan else ""

        guidance = str(
            templates.get("guidance")
            or (
                "Ты под-агент Codebase Mapper."
                " Обнови только целевые файлы в `.cli-proxy/.codebase_map/`."
                " Пиши на диск; в ответе только короткий отчет."
            )
        )

        if full_scan:
            scan_policy = str(
                templates.get("scan_policy_full")
                or (
                    "Режим: полный скан. Используй `rg --files` и выборочное чтение ключевых файлов по фокусу, "
                    "чтобы сформировать полную и актуальную картину."
                )
            )
        else:
            scan_policy = str(
                templates.get("scan_policy_incremental")
                or (
                    "Режим: инкрементальный апдейт."
                    " Сначала changed files."
                    " Доп. чтение только при прямой связи."
                    " Полный обход запрещен."
                )
            )

        write_rules_tpl = str(
            templates.get("write_rules")
            or (
                "Правила записи:\n"
                "1. Обнови только документы:\n{docs_list}\n"
                "2. Путь для записи: `{map_dir}`\n"
                "3. Формат: короткий markdown с конкретными file-path.\n"
                "4. Если данных мало, явно укажи ограничения."
            )
        )
        write_rules = write_rules_tpl.format(docs_list=docs_list, map_dir=map_dir)

        sections = [
            guidance,
            f"Фокус: `{focus}`",
            f"Проект: `{root}`",
            scan_policy,
            f"Контекст запуска: {mode_label}",
            "Измененные файлы (git diff):",
            changed_block,
            write_rules,
        ]

        if full_scan:
            sections.extend([
                "Индекс файлов (результат rg --files):",
                index_block or "- (пусто)",
            ])

        sections.append(
            str(
                templates.get("report_format")
                or "Верни итог: фокус, обновленные файлы, что дополнительно проверено."
            )
        )
        return "\n\n".join(sections).strip()

    @staticmethod
    def _prompt_caps(*, operation: str) -> Tuple[int, int]:
        op = str(operation or "").strip().lower()
        if op == "run":
            return _PROMPT_CHANGED_CAP_FAST, _PROMPT_INDEX_CAP_FAST
        if op in {"verify", "init_full"}:
            return _PROMPT_CHANGED_CAP_DEEP, _PROMPT_INDEX_CAP_DEEP
        return _PROMPT_CHANGED_CAP_DEFAULT, _PROMPT_INDEX_CAP_DEFAULT

    def _scan_workspace(self, workdir: str) -> Dict[str, Any]:
        files = self._list_files(workdir)
        with concurrent.futures.ThreadPoolExecutor(max_workers=_CLI_PARALLELISM_LIMIT) as pool:
            ext_future = pool.submit(self._collect_ext_counts, files)
            top_future = pool.submit(self._collect_top_level, files)
            todo_future = pool.submit(self._rg_count, workdir, _UNFINISHED_MARKER_REGEX)
            ext_counts = ext_future.result()
            top_level = top_future.result()
            todo_count = todo_future.result()
        return {
            "files": files,
            "ext_counts": ext_counts,
            "top_level": top_level,
            "todo_count": todo_count,
        }

    def _scan_workspace_for_docs(self, workdir: str, docs_to_update: Set[str]) -> Dict[str, Any]:
        files = self._list_files(workdir)
        docs = set(str(x) for x in list(docs_to_update or []) if str(x).strip())
        needs_ext = "STACK.md" in docs
        needs_top = bool({"ARCHITECTURE.md", "CONCERNS.md", "STRUCTURE.md"} & docs)
        needs_todo = "CONCERNS.md" in docs

        ext_counts: Dict[str, int] = {}
        top_level: Dict[str, int] = {}
        todo_count = 0

        if needs_ext or needs_top or needs_todo:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_CLI_PARALLELISM_LIMIT) as pool:
                ext_future = pool.submit(self._collect_ext_counts, files) if needs_ext else None
                top_future = pool.submit(self._collect_top_level, files) if needs_top else None
                todo_future = pool.submit(self._rg_count, workdir, _UNFINISHED_MARKER_REGEX) if needs_todo else None
                if ext_future is not None:
                    ext_counts = ext_future.result()
                if top_future is not None:
                    top_level = top_future.result()
                if todo_future is not None:
                    todo_count = int(todo_future.result() or 0)

        return {
            "files": files,
            "ext_counts": ext_counts,
            "top_level": top_level,
            "todo_count": todo_count,
        }

    @staticmethod
    def _collect_ext_counts(files: Sequence[str]) -> Dict[str, int]:
        ext_counts: Dict[str, int] = {}
        for p in files:
            base = os.path.basename(p)
            _name, ext = os.path.splitext(base)
            if ext:
                ext_counts[ext.lower()] = ext_counts.get(ext.lower(), 0) + 1
        return ext_counts

    @staticmethod
    def _collect_top_level(files: Sequence[str]) -> Dict[str, int]:
        top_level: Dict[str, int] = {}
        for p in files:
            first = p.split("/", 1)[0]
            top_level[first] = top_level.get(first, 0) + 1
        return top_level

    def _list_files(self, workdir: str) -> List[str]:
        """
        Список файлов в workdir.

        Приоритеты:
        1. ripgrep (rg --files) — быстро и уважает .gitignore
        2. os.walk + pathspec — fallback с поддержкой .gitignore
        3. os.walk — последний fallback без .gitignore

        Важно: ripgrep должен быть установлен в системе. Если нет — будет выведено предупреждение.
        """
        # Попытка #1: ripgrep (предпочтительно — быстро и уважает .gitignore)
        rg = self._run_cmd(["rg", "--files"], cwd=workdir)
        if rg.returncode == 0:
            files = [s.strip() for s in (rg.stdout or "").splitlines() if s.strip()]
            return [p for p in files if self._is_mappable_file(p)]

        # Предупреждение об отсутствии ripgrep
        self._log.warning(
            "ripgrep (rg) не найден в PATH. Codebase Mapper будет работать медленнее "
            "и может не уважать .gitignore. Установите: sudo apt-get install ripgrep "
            "(Ubuntu/Debian) или brew install ripgrep (macOS)."
        )

        # Попытка #2: os.walk + pathspec для .gitignore (параллельно по top-level директориям)
        files: List[str] = []
        top_dirs: List[str] = []
        try:
            for entry in os.scandir(workdir):
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _SCAN_EXCLUDED_DIRS:
                        continue
                    top_dirs.append(entry.name)
                    continue
                if entry.is_file(follow_symlinks=False):
                    p = entry.name.replace("\\", "/")
                    if self._is_mappable_file(p):
                        files.append(p)
        except Exception:
            self._log.exception("codebase mapper scan: failed to list top-level entries")
            top_dirs = []

        def _scan_subtree(top_dir: str) -> List[str]:
            subtree_files: List[str] = []
            start = os.path.join(workdir, top_dir)
            for root, dirs, names in os.walk(start):
                dirs[:] = [d for d in dirs if d not in _SCAN_EXCLUDED_DIRS]
                rel_root = os.path.relpath(root, workdir)
                for n in names:
                    rel = n if rel_root == "." else os.path.join(rel_root, n)
                    p = rel.replace("\\", "/")
                    if self._is_mappable_file(p):
                        subtree_files.append(p)
            return subtree_files

        if top_dirs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_CLI_PARALLELISM_LIMIT) as pool:
                futures = [pool.submit(_scan_subtree, d) for d in top_dirs]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        files.extend(list(future.result() or []))
                    except Exception:
                        self._log.exception("codebase mapper scan: subtree scan failed")

        # Применяем .gitignore если доступен pathspec
        gitignore_path = os.path.join(workdir, ".gitignore")
        if os.path.exists(gitignore_path):
            try:
                import pathspec
                with open(gitignore_path, "r", encoding="utf-8") as f:
                    spec = pathspec.PathSpec.from_lines("gitwildmatch", f)
                files = [f for f in files if not spec.match_file(f)]
            except ImportError:
                # pathspec не установлен — предупреждаем в лог
                self._log.warning(
                    ".gitignore найден, но pathspec не установлен. "
                    "Установите: pip install pathspec"
                )
            except Exception:
                self._log.exception(".gitignore parse failed")

        return sorted(set(files))

    def _list_all_files(self, workdir: str) -> List[str]:
        rg = self._run_cmd(["rg", "--files"], cwd=workdir)
        if rg.returncode == 0:
            return [s.strip() for s in (rg.stdout or "").splitlines() if s.strip()]

        files: List[str] = []
        for root, dirs, names in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in _SCAN_EXCLUDED_DIRS]
            rel_root = os.path.relpath(root, workdir)
            for n in names:
                rel = n if rel_root == "." else os.path.join(rel_root, n)
                files.append(rel.replace("\\", "/"))
        return files

    @staticmethod
    def _is_mappable_file(path: str) -> bool:
        p = str(path or "").replace("\\", "/").strip("/")
        if not p:
            return False
        segments = [seg for seg in p.split("/") if seg]
        if any(seg in {".git", ".venv", ".pytest_cache", ".codebase_map", "node_modules", "__pycache__"} for seg in segments):
            return False
        if CodebaseMapperRuntime._is_map_relative_path(p):
            return False
        base = os.path.basename(p).lower()
        if base.endswith((".pyc", ".pyo")):
            return False
        if base.endswith(".md"):
            return False
        if base in {"license", "license.txt", "copying", "copying.txt"}:
            return False
        return True

    def _extract_symbols(self, abs_path: str) -> List[Dict[str, Any]]:
        """Извлекает высокоуровневые символы (классы, методы) из файла."""
        if not os.path.exists(abs_path):
            return []

        ext = os.path.splitext(abs_path)[1].lower()
        if ext == ".py":
            return self._extract_python_symbols(abs_path)
        elif ext in {".ts", ".tsx", ".js", ".jsx"}:
            return self._extract_js_ts_symbols(abs_path)
        elif ext == ".go":
            return self._extract_go_symbols(abs_path)
        elif ext == ".rs":
            return self._extract_rust_symbols(abs_path)
        return []

    def _extract_python_symbols(self, abs_path: str) -> List[Dict[str, Any]]:
        symbols = []
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            tree = ast.parse(content)

            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    bases = [ast.unparse(b) for b in node.bases]
                    doc = ast.get_docstring(node)
                    symbols.append({
                        "type": "class",
                        "name": node.name,
                        "bases": bases,
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "doc": doc.split("\n")[0] if doc else None,
                        "methods": self._extract_python_methods(node)
                    })
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith("_") and not (node.name.startswith("__") and node.name.endswith("__")):
                        continue
                    doc = ast.get_docstring(node)
                    symbols.append({
                        "type": "function",
                        "name": node.name,
                        "args": [a.arg for a in node.args.args],
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "doc": doc.split("\n")[0] if doc else None,
                        "async": isinstance(node, ast.AsyncFunctionDef)
                    })
        except Exception:
            self._log.exception("codebase map: python symbol extraction failed %s", abs_path)
        return symbols

    def _extract_python_methods(self, class_node: ast.ClassDef) -> List[Dict[str, Any]]:
        methods = []
        for node in class_node.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") and node.name not in ("__init__", "__call__"):
                    continue
                doc = ast.get_docstring(node)
                methods.append({
                    "name": node.name,
                    "args": [a.arg for a in node.args.args if a.arg != "self"],
                    "line": int(getattr(node, "lineno", 0) or 0),
                    "doc": doc.split("\n")[0] if doc else None,
                    "async": isinstance(node, ast.AsyncFunctionDef)
                })
        return methods

    def _extract_js_ts_symbols(self, abs_path: str) -> List[Dict[str, Any]]:
        symbols = []
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Regex для экспортируемых сущностей
            # export (class|interface|type|function|const) Name
            pattern = re.compile(r"export\s+(class|interface|type|enum|function|const)\s+([a-zA-Z0-9_]+)")
            for m in pattern.finditer(content):
                symbols.append({
                    "type": m.group(1),
                    "name": m.group(2),
                    "line": self._line_of_offset(content, m.start()),
                })
        except Exception:
            self._log.exception("codebase map: js/ts symbol extraction failed %s", abs_path)
        return symbols

    def _extract_go_symbols(self, abs_path: str) -> List[Dict[str, Any]]:
        symbols = []
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            # func Name(...)
            pattern_func = re.compile(r"^func\s+([A-Z][a-zA-Z0-9_]*)", re.MULTILINE)
            # type Name struct|interface
            pattern_type = re.compile(r"^type\s+([A-Z][a-zA-Z0-9_]*)\s+(struct|interface)", re.MULTILINE)

            for m in pattern_func.finditer(content):
                symbols.append({
                    "type": "function",
                    "name": m.group(1),
                    "line": self._line_of_offset(content, m.start()),
                })
            for m in pattern_type.finditer(content):
                symbols.append({
                    "type": m.group(2),
                    "name": m.group(1),
                    "line": self._line_of_offset(content, m.start()),
                })
        except Exception:
            self._log.exception("codebase map: go symbol extraction failed %s", abs_path)
        return symbols

    def _extract_rust_symbols(self, abs_path: str) -> List[Dict[str, Any]]:
        symbols = []
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            # pub (struct|enum|trait|fn) Name
            pattern = re.compile(r"pub(?:\s*\([^)]+\))?\s+(struct|enum|trait|fn|type)\s+([a-zA-Z0-9_]+)")
            for m in pattern.finditer(content):
                symbols.append({
                    "type": m.group(1),
                    "name": m.group(2),
                    "line": self._line_of_offset(content, m.start()),
                })
        except Exception:
            self._log.exception("codebase map: rust symbol extraction failed %s", abs_path)
        return symbols

    @staticmethod
    def _line_of_offset(content: str, offset: int) -> int:
        try:
            pos = max(0, int(offset))
        except Exception:
            pos = 0
        return int((content or "").count("\n", 0, pos) + 1)

    def _generate_api_spec(self, root: str, map_dir: str, rel_path: str) -> Optional[str]:
        """Генерирует MD-файл с API в зеркальную папку api/."""
        abs_path = os.path.join(root, rel_path)
        symbols = self._extract_symbols(abs_path)
        if not symbols:
            return None

        # Формируем путь: app/services/auth.py -> api/app/services/auth-py.md
        name_parts = os.path.splitext(rel_path)
        mirror_rel = name_parts[0] + name_parts[1].replace(".", "-") + ".md"
        mirror_abs = os.path.join(map_dir, _GRAPH_API_DIR, mirror_rel)

        content = self._render_api_markdown(rel_path, symbols)
        self._write_text(mirror_abs, content)
        return os.path.join(_GRAPH_API_DIR, mirror_rel).replace("\\", "/")

    def _cleanup_stale_api_specs(self, *, map_dir: str, expected_api_paths: Set[str]) -> None:
        api_root = os.path.join(map_dir, _GRAPH_API_DIR)
        if not os.path.isdir(api_root):
            return
        expected = {str(p).replace("\\", "/").strip("/") for p in list(expected_api_paths or set()) if str(p).strip()}
        for dirpath, _dirnames, filenames in os.walk(api_root, topdown=False):
            for name in filenames:
                if not str(name).lower().endswith(".md"):
                    continue
                abs_path = os.path.join(dirpath, name)
                rel_from_map = os.path.relpath(abs_path, map_dir).replace("\\", "/").strip("/")
                if rel_from_map in expected:
                    continue
                try:
                    os.remove(abs_path)
                except Exception:
                    self._log.exception("failed to remove stale api spec %s", abs_path)
            if dirpath != api_root:
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except Exception:
                    self._log.exception("failed to cleanup empty api dir %s", dirpath)

    def _render_api_markdown(self, rel_path: str, symbols: List[Dict[str, Any]]) -> str:
        lines = [f"# API Spec: `{rel_path}`", "", f"Generated: {self._utc_now_iso()}", ""]

        classes = [s for s in symbols if s["type"] == "class"]
        others = [s for s in symbols if s["type"] != "class"]

        if classes:
            lines.append("## Classes")
            for c in classes:
                base_str = f"({', '.join(c['bases'])})" if c.get("bases") else ""
                class_line = int(c.get("line") or 0)
                class_suffix = f" (line {class_line})" if class_line > 0 else ""
                lines.append(f"### `class {c['name']}{base_str}`{class_suffix}")
                if c.get("doc"):
                    lines.append(f"*{c['doc']}*")

                for m in c.get("methods", []):
                    async_prefix = "async " if m.get("async") else ""
                    method_line = int(m.get("line") or 0)
                    method_suffix = f" (line {method_line})" if method_line > 0 else ""
                    lines.append(f"- `{async_prefix}def {m['name']}({', '.join(m['args'])})`{method_suffix}")
                    if m.get("doc"):
                        lines.append(f"  - *{m['doc']}*")
                lines.append("")

        if others:
            lines.append("## Symbols")
            for s in others:
                symbol_line = int(s.get("line") or 0)
                symbol_suffix = f" (line {symbol_line})" if symbol_line > 0 else ""
                if s["type"] == "function":
                    async_prefix = "async " if s.get("async") else ""
                    lines.append(f"- `{async_prefix}def {s['name']}({', '.join(s.get('args', []))})`{symbol_suffix}")
                else:
                    lines.append(f"- `{s['type']} {s['name']}`{symbol_suffix}")
                if s.get("doc"):
                    lines.append(f"  - *{s['doc']}*")
            lines.append("")

        return "\n".join(lines)

    def _rg_count(self, workdir: str, pattern: str) -> int:
        proc = self._run_cmd(["rg", "-n", pattern, workdir])
        if proc.returncode not in (0, 1):
            return 0
        return len([x for x in (proc.stdout or "").splitlines() if x.strip()])

    @staticmethod
    def _run_cmd(args: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

    @staticmethod
    def _normalize_operation(operation: str) -> str:
        s = str(operation or "run").strip().lower()
        if s in {"run", "force", "init", "init_full", "verify", "validate", "repair"}:
            return s
        return "run"

    @staticmethod
    def _top_items(items: Dict[str, int], limit: int = 12) -> List[tuple[str, int]]:
        return sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    def _extract_operational_commands(self, workdir: str, snap: Dict[str, Any]) -> List[str]:
        files = set(snap.get("files", []))
        cmds = []

        # 1. Makefile
        if "Makefile" in files:
            path = os.path.join(workdir, "Makefile")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                targets = re.findall(r"^([a-zA-Z0-9_-]+):", content, re.MULTILINE)
                if targets:
                    valid = [t for t in targets if t not in ("all", ".PHONY")]
                    if valid:
                        cmds.append(f"- `make <target>`: targets: {', '.join(valid[:10])}")
            except Exception:
                self._log.exception("codebase map: failed to read Makefile commands %s", path)

        # 2. package.json
        if "package.json" in files:
            path = os.path.join(workdir, "package.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                scripts = data.get("scripts", {})
                if scripts:
                    names = sorted(scripts.keys())
                    cmds.append(f"- `npm run <script>`: {', '.join(names[:10])}")
            except Exception:
                self._log.exception("codebase map: failed to read package.json scripts %s", path)

        # 3. pyproject.toml
        if "pyproject.toml" in files:
            path = os.path.join(workdir, "pyproject.toml")
            try:
                import tomllib
                with open(path, "rb") as f:
                    data = tomllib.load(f)

                tool = data.get("tool", {})
                scripts = []
                if "poetry" in tool and "scripts" in tool["poetry"]:
                    scripts.extend(tool["poetry"]["scripts"].keys())

                hints = []
                if "pytest" in tool:
                    hints.append("pytest")
                if "ruff" in tool:
                    hints.append("ruff")
                if "black" in tool:
                    hints.append("black")
                if "mypy" in tool:
                    hints.append("mypy")

                if scripts or hints:
                    info = []
                    if scripts:
                        info.append(f"poetry scripts: {', '.join(scripts[:10])}")
                    if hints:
                        info.append(f"configured: {', '.join(hints)}")
                    cmds.append(f"- `pyproject.toml`: {' | '.join(info)}")
            except Exception:
                self._log.exception("codebase map: failed to read pyproject.toml commands %s", path)

        # 4. Ecosystem-specific Fallbacks
        exts = snap.get("ext_counts", {})

        # Python
        if ".py" in exts or "requirements.txt" in files or "pyproject.toml" in files:
            if "requirements.txt" in files:
                cmds.append("- `pip install -r requirements.txt`: install dependencies")
            if "pytest.ini" in files or any(f.startswith("tests/") and f.endswith(".py") for f in files):
                cmds.append("- `pytest`: run python tests")
            if ".flake8" in files:
                cmds.append("- `flake8 .`: lint python code")

        # Go
        if ".go" in exts or "go.mod" in files:
            cmds.append("- `go build ./...`: compile go project")
            cmds.append("- `go test ./...`: run go tests")

        # Rust
        if ".rs" in exts or "Cargo.toml" in files:
            cmds.append("- `cargo build`: build rust project")
            cmds.append("- `cargo test`: run rust tests")

        # JavaScript / TypeScript (if package.json was missing or empty)
        if (".js" in exts or ".ts" in exts) and not any(c.startswith("- `npm") for c in cmds):
            if "package.json" in files:
                cmds.append("- `npm install`: install node dependencies")
            cmds.append("- `npm test`: run node tests (if configured)")

        return cmds

    def _render_stack(self, workdir: str, snap: Dict[str, Any]) -> str:
        lines = [
            "# STACK",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            "## Languages (by extension)",
        ]
        exts = self._top_items(snap.get("ext_counts", {}), limit=10)
        if not exts:
            lines.append("- no source files detected")
        else:
            for ext, cnt in exts:
                lines.append(f"- `{ext}`: {cnt}")

        markers = {
            "Python": ["requirements.txt", "pyproject.toml"],
            "Node.js": ["package.json"],
            "Docker": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
        }
        files = set(snap.get("files", []))
        lines.extend(["", "## Toolchains"])
        for name, probes in markers.items():
            hit = any(p in files for p in probes)
            lines.append(f"- {name}: {'yes' if hit else 'no'}")

        ops = self._extract_operational_commands(workdir, snap)
        if ops:
            lines.extend(["", "## Commands"])
            lines.extend(ops[:8])

        lines.append("")
        return "\n".join(lines)

    def _render_integrations(self, workdir: str, snap: Dict[str, Any]) -> str:
        files = set(snap.get("files", []))
        hints = []
        if "config.yaml" in files:
            hints.append("- `config.yaml`: central runtime configuration (tools/openai/mcp/miniapp)")
        if any(p.startswith("agent/mcp/") for p in files):
            hints.append("- `agent/mcp/*`: MCP client/manager integration")
        if any(p.startswith("miniapp/") for p in files):
            hints.append("- `miniapp/*`: embedded HTTP MiniApp")
        if not hints:
            hints.append("- explicit integration hints not detected")
        return "\n".join([
            "# INTEGRATIONS",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            *hints[:8],
            "",
        ])

    def _render_architecture(self, workdir: str, snap: Dict[str, Any]) -> str:
        top = self._top_items(snap.get("top_level", {}), limit=20)
        lines = [
            "# ARCHITECTURE",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            "## Top-level areas",
        ]
        for name, cnt in top[:12]:
            lines.append(f"- `{name}`: {cnt} files")
        if not top:
            lines.append("- no files detected")
        lines.append("")
        return "\n".join(lines)

    def _render_structure(self, workdir: str, snap: Dict[str, Any]) -> str:
        files = list(snap.get("files", []))
        top = self._top_items(snap.get("top_level", {}), limit=12)
        representative = files[:20]
        lines = [
            "# STRUCTURE",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            f"- total indexed files: {len(files)}",
            "",
            "## Top-level areas",
        ]
        if top:
            for name, cnt in top:
                lines.append(f"- `{name}`: {cnt}")
        else:
            lines.append("- no files detected")
        if representative:
            lines.extend(["", "## Representative paths"])
            for p in representative:
                lines.append(f"- `{p}`")
        lines.append("")
        return "\n".join(lines)

    def _render_conventions(self, workdir: str, snap: Dict[str, Any]) -> str:
        files = set(snap.get("files", []))
        lines = [
            "# CONVENTIONS",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            f"- `flake8`: {'yes' if '.flake8' in files else 'unknown'}",
            f"- `pytest` layout (`tests/`): {'yes' if any(p.startswith('tests/') for p in files) else 'no'}",
            "",
        ]
        return "\n".join(lines)

    def _render_testing(self, workdir: str, snap: Dict[str, Any]) -> str:
        files = list(snap.get("files", []))
        tests = [p for p in files if p.startswith("tests/")]
        test_roots: Dict[str, int] = {}
        for path in tests:
            rel = str(path).split("/", 1)[1] if "/" in str(path) else ""
            root = rel.split("/", 1)[0] if rel else "(root)"
            test_roots[root] = int(test_roots.get(root) or 0) + 1
        lines = [
            "# TESTING",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            f"- test files under `tests/`: {len(tests)}",
        ]
        if test_roots:
            lines.extend(["", "## Test areas"])
            for name, cnt in sorted(test_roots.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
                lines.append(f"- `tests/{name}`: {cnt}")
        if tests:
            lines.extend(["", "## Representative tests"])
            for path in tests[:8]:
                lines.append(f"- `{path}`")
        lines.append("")
        return "\n".join(lines)

    def _render_concerns(self, workdir: str, snap: Dict[str, Any]) -> str:
        todo_count = int(snap.get("todo_count", 0) or 0)
        top = self._top_items(snap.get("top_level", {}), limit=8)
        lines = [
            "# CONCERNS",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            "## Potential concerns",
            f"- {_UNFINISHED_MARKER_LABEL}: {todo_count}",
            "- Auto-generated map; verify critical assumptions manually before refactors.",
            "",
            "## Largest top-level areas",
        ]
        for name, cnt in top:
            lines.append(f"- `{name}`: {cnt} files")
        if not top:
            lines.append("- no files detected")
        lines.append("")
        return "\n".join(lines)

    def _restore_missing_static_docs(self, *, map_dir: str, root: str) -> List[str]:
        """Восстанавливает отсутствующие статические документы.

        Проверяет наличие всех документов из _DOC_NAMES и создаёт отсутствующие.
        Использует тот же snapshot, что и основной pipeline (через _scan_workspace).

        Returns:
            List[str] — список восстановленных документов.
        """
        restored: List[str] = []

        # Проверка отсутствующих документов
        missing = [name for name in _DOC_NAMES if not os.path.exists(os.path.join(map_dir, name))]
        if not missing:
            return restored

        # Сканирование workspace (использует rg --files с уважением .gitignore)
        snapshot = self._scan_workspace(root)

        # Рендеринг только отсутствующих документов
        renderers = {
            "STACK.md": self._render_stack,
            "INTEGRATIONS.md": self._render_integrations,
            "ARCHITECTURE.md": self._render_architecture,
            "STRUCTURE.md": self._render_structure,
            "CONVENTIONS.md": self._render_conventions,
            "TESTING.md": self._render_testing,
            "CONCERNS.md": self._render_concerns,
        }

        for name in missing:
            renderer = renderers.get(name)
            if renderer is None:
                self._log.warning("codebase map: unknown doc %s", name)
                continue
            doc_path = os.path.join(map_dir, name)
            content = renderer(root, snapshot)
            with open(doc_path, "w", encoding="utf-8") as f:
                f.write(content)
            restored.append(name)
            self._log.info("codebase map: restored missing doc %s", name)

        return restored

    @staticmethod
    def _slugify_name(name: str) -> str:
        raw = str(name or "").strip().lower()
        out = []
        prev_dash = False
        for ch in raw:
            if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
                out.append(ch)
                prev_dash = False
            elif ch in {"_", "-", "/", ".", " "}:
                if not prev_dash:
                    out.append("-")
                    prev_dash = True
        slug = "".join(out).strip("-")
        return slug or "root"

    @staticmethod
    def _is_ignored_graph_segment(name: str) -> bool:
        s = str(name or "").strip()
        return s in {".git", ".venv", ".pytest_cache", "__pycache__", ".codebase_map", "node_modules"}

    def _sync_instruction_graph(
        self,
        *,
        root: str,
        map_dir: str,
        snapshot: Dict[str, Any],
        changed_files: Sequence[str],
        operation: str,
        force: bool,
        assume_first_init: bool = False,
    ) -> Dict[str, Any]:
        os.makedirs(map_dir, exist_ok=True)
        nodes_dir = os.path.join(map_dir, _GRAPH_NODES_DIR)
        os.makedirs(nodes_dir, exist_ok=True)
        top_level = dict(snapshot.get("top_level", {}) or {})
        files = list(snapshot.get("files", []) or [])

        domains: List[tuple[str, int]] = []
        for name, count in sorted(top_level.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            if self._is_ignored_graph_segment(name):
                continue
            domains.append((str(name), int(count)))
        if not domains:
            domains = [("workspace", len(files))]
        domain_files = self._build_domain_file_index(files=files, domains=[name for name, _count in domains])
        domain_relations = self._infer_domain_relations(root=root, domain_files=domain_files)
        domain_to_node_path = {
            str(name): f"{_GRAPH_NODES_DIR}/{self._slugify_name(name)}.md"
            for name, _count in domains
        }

        node_entries: List[Dict[str, Any]] = []
        expected_api_specs: Set[str] = set()
        tree_lines: List[str] = [".cli-proxy/.codebase_map/", "  INDEX.md", "  nodes/"]
        for domain, count in domains:
            slug = self._slugify_name(domain)
            rel_node_path = f"{_GRAPH_NODES_DIR}/{slug}.md"
            abs_node_path = os.path.join(map_dir, rel_node_path)
            related_map = dict(domain_relations.get(domain) or {})
            related_domains = sorted(
                rel for rel in list(related_map.keys())
                if rel != domain and rel in domain_to_node_path
            )
            related_nodes = [str(domain_to_node_path.get(rel) or "") for rel in related_domains]
            related_globs = [self._area_to_source_glob(rel) for rel in related_domains]
            related_relation_notes = [
                self._format_relation_note(target=rel, relation=dict(related_map.get(rel) or {}))
                for rel in related_domains
            ]
            source_samples = self._select_domain_source_samples(
                domain=domain,
                domain_files=list(domain_files.get(domain) or []),
                changed_files=changed_files,
                operation=operation,
            )

            # Generate API mirror for samples
            api_links = []
            for sample_p in source_samples:
                try:
                    api_rel = self._generate_api_spec(root, map_dir, sample_p)
                    if api_rel:
                        expected_api_specs.add(str(api_rel).replace("\\", "/").strip("/"))
                        # Path from nodes/*.md to api/... is ../api/...
                        api_links.append((sample_p, f"../{api_rel}"))
                except Exception:
                    self._log.exception("failed to generate api spec for %s", sample_p)

            changed_hits = [
                p for p in changed_files
                if str(p).replace("\\", "/") == domain or str(p).replace("\\", "/").startswith(f"{domain}/")
            ]
            self._write_text(
                abs_node_path,
                self._render_graph_node(
                    domain=domain,
                    rel_node_path=rel_node_path,
                    source_glob=self._area_to_source_glob(domain),
                    file_count=count,
                    source_samples=source_samples,
                    related_node_paths=related_nodes,
                    related_source_globs=related_globs,
                    related_relation_notes=related_relation_notes,
                    changed_hits=changed_hits,
                    api_links=api_links,
                ),
            )
            node_entries.append(
                {
                    "id": f"node:{slug}",
                    "type": "instruction_node",
                    "title": domain,
                    "path": rel_node_path,
                    "source_glob": self._area_to_source_glob(domain),
                    "file_count": count,
                }
            )
            tree_lines.append(f"    {slug}.md")

        self._cleanup_stale_api_specs(map_dir=map_dir, expected_api_paths=expected_api_specs)

        index_path = os.path.join(map_dir, _GRAPH_INDEX)
        self._write_text(
            index_path,
            self._render_graph_index(
                node_entries=node_entries,
                map_dir=map_dir,
                changed_files=changed_files,
            ),
        )
        existing_state = self._read_graph_state(map_dir)
        reviewed = dict(existing_state.get("reviewed") or {})
        rules_reviewed = dict(existing_state.get("rules_reviewed") or {})
        existing_review_items = [str(x) for x in list(existing_state.get("review_items") or []) if str(x).strip()]
        is_first_init = bool(
            assume_first_init or (operation in {"init", "init_full"} and not existing_review_items)
        )
        inferred_rules = self._infer_organizational_rules(
            root=root,
            snapshot=snapshot,
            node_entries=node_entries,
            reviewed_rules=rules_reviewed,
        )
        rules_review_items = [str(rule.get("id") or "") for rule in inferred_rules if str(rule.get("id") or "").strip()]
        rules_needs_review = [
            rule_id
            for rule_id in rules_review_items
            if any(
                str((rule or {}).get("id") or "") == rule_id and bool((rule or {}).get("needs_review"))
                for rule in inferred_rules
            )
        ]
        graph_payload = {
            "version": 1,
            "generated_at": self._utc_now_iso(),
            "operation": operation,
            "force": bool(force),
            "nodes": node_entries,
            "organizational_rules": inferred_rules,
            "edges": self._build_graph_edges(node_entries=node_entries, domain_relations=domain_relations),
            "tree": tree_lines,
        }
        review_items = [str(item.get("path") or "") for item in node_entries if str(item.get("path") or "").strip()]
        touched_items = self._review_touched_items(
            review_items=review_items,
            changed_files=list(changed_files or []),
            operation=operation,
            is_first_init=is_first_init,
        )
        for item in review_items:
            if item not in reviewed:
                reviewed[item] = True
        for item in touched_items:
            reviewed.pop(item, None)
        needs_review = [p for p in review_items if not bool(reviewed.get(p))]
        validation = self._validate_instruction_graph(
            map_dir=map_dir,
            node_entries=node_entries,
            tree_lines=tree_lines,
        )
        validate_queue = [node for node in list(validation.get("invalid_nodes") or []) if node in review_items]
        existing_nodes_status = dict(existing_state.get("nodes_status") or {})
        nodes_status = self._merge_nodes_status(
            node_entries=node_entries,
            existing_status=existing_nodes_status,
            invalid_nodes=validate_queue,
            invalid_reasons=dict(validation.get("invalid_reasons") or {}),
        )
        repair_queue = [n for n, payload in nodes_status.items() if str((payload or {}).get("status") or "") == "needs_repair"]
        all_needs_review = (
            needs_review
            + [x for x in rules_needs_review if x not in needs_review]
            + [x for x in repair_queue if x not in needs_review]
        )
        graph_state = (
            CODEBASE_MAPPER_GRAPH_STATE["READY"]
            if not all_needs_review
            else CODEBASE_MAPPER_GRAPH_STATE["NEEDS_REVIEW"]
        )
        rules_path = os.path.join(map_dir, _GRAPH_RULES_FILE)
        self._write_text(
            rules_path,
            self._render_graph_rules(node_entries=node_entries, inferred_rules=inferred_rules),
        )
        self._write_json_atomic(os.path.join(map_dir, _GRAPH_FILE), graph_payload)
        self._write_graph_state(
            map_dir,
            {
                "state": graph_state,
                "updated_at": self._utc_now_iso(),
                "operation": operation,
                "nodes_count": len(node_entries),
                "tree": tree_lines,
                "review_items": review_items,
                "needs_review": needs_review,
                "reviewed": reviewed,
                "rules_review_items": rules_review_items,
                "rules_needs_review": rules_needs_review,
                "rules_reviewed": rules_reviewed,
                "inferred_rules": inferred_rules,
                "validate_queue": validate_queue,
                "repair_queue": repair_queue,
                "validation_report": dict(validation),
                "nodes_status": nodes_status,
                "relation_graph": self._relation_graph_payload(domain_relations=domain_relations),
            },
        )
        return {
            "state": graph_state,
            "nodes_count": len(node_entries),
            "tree": tree_lines,
        }

    def _validate_instruction_graph(
        self,
        *,
        map_dir: str,
        node_entries: Sequence[Dict[str, Any]],
        tree_lines: Sequence[str],
    ) -> Dict[str, Any]:
        invalid_reasons: Dict[str, List[str]] = {}
        required_sections = [
            "## Purpose",
            "## Scope",
            "## Instructions for agent",
            "## Source of truth",
            "## When to update",
            "## Related nodes",
            "## Last reviewed",
        ]
        declared_nodes = [str(item.get("path") or "") for item in node_entries if str(item.get("path") or "").strip()]
        for rel in declared_nodes:
            abs_path = os.path.join(map_dir, rel)
            reasons: List[str] = []
            if not os.path.exists(abs_path):
                reasons.append("node_file_missing")
                invalid_reasons[rel] = reasons
                continue
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                self._log.exception("codebase map validate: failed reading node %s", abs_path)
                reasons.append("node_read_failed")
                invalid_reasons[rel] = reasons
                continue
            for section in required_sections:
                if section not in content:
                    reasons.append(f"missing_section:{section}")
            if "## Related nodes" in content:
                for ref in re.findall(r"`([^`]+)`", content):
                    p = str(ref).replace("\\", "/").strip("/")
                    if p.startswith("nodes/") and p != rel and not os.path.exists(os.path.join(map_dir, p)):
                        reasons.append(f"related_missing:{p}")
            if "## Source of truth" in content:
                if "`" not in content.split("## Source of truth", 1)[-1]:
                    reasons.append("source_of_truth_empty")
            if "## When to update" in content:
                tail = content.split("## When to update", 1)[-1]
                if "- " not in tail:
                    reasons.append("when_to_update_empty")
            if reasons:
                invalid_reasons[rel] = reasons
        nodes_dir = os.path.join(map_dir, _GRAPH_NODES_DIR)
        actual_nodes = []
        if os.path.isdir(nodes_dir):
            for name in sorted(os.listdir(nodes_dir)):
                if name.endswith(".md"):
                    actual_nodes.append(f"{_GRAPH_NODES_DIR}/{name}")
        declared_set = set(declared_nodes)
        for node in actual_nodes:
            if node not in declared_set:
                invalid_reasons.setdefault(node, []).append("orphan_node")
        index_path = os.path.join(map_dir, _GRAPH_INDEX)
        if os.path.exists(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    index_content = f.read()
                for rel in declared_nodes:
                    if rel not in index_content:
                        invalid_reasons.setdefault(rel, []).append("index_missing_node_link")
            except Exception:
                self._log.exception("codebase map validate: failed reading index")
        invalid_nodes = sorted(invalid_reasons.keys())
        return {
            "invalid_nodes": invalid_nodes,
            "invalid_reasons": invalid_reasons,
            "orphan_nodes": [n for n in invalid_nodes if "orphan_node" in list(invalid_reasons.get(n) or [])],
            "tree": list(tree_lines or []),
            "validated_at": self._utc_now_iso(),
        }

    def _merge_nodes_status(
        self,
        *,
        node_entries: Sequence[Dict[str, Any]],
        existing_status: Dict[str, Any],
        invalid_nodes: Sequence[str],
        invalid_reasons: Dict[str, List[str]],
    ) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        invalid_set = set(str(x) for x in list(invalid_nodes or []))
        for item in node_entries:
            rel = str(item.get("path") or "").strip()
            if not rel:
                continue
            prev = dict((existing_status or {}).get(rel) or {})
            attempts = int(prev.get("repair_attempts") or 0)
            prev_status = str(prev.get("status") or "").strip()
            if rel in invalid_set:
                status = "degraded" if prev_status == "degraded" else "needs_repair"
            else:
                status = "ok"
                attempts = 0
            out[rel] = {
                "status": status,
                "repair_attempts": attempts,
                "last_error": "; ".join(list(invalid_reasons.get(rel) or [])) if rel in invalid_set else "",
                "updated_at": self._utc_now_iso(),
            }
        for rel, payload in dict(existing_status or {}).items():
            if rel not in out:
                out[rel] = dict(payload or {})
        for rel in sorted(invalid_set):
            if rel in out:
                continue
            out[rel] = {
                "status": "invalid",
                "repair_attempts": 0,
                "last_error": "; ".join(list(invalid_reasons.get(rel) or [])),
                "updated_at": self._utc_now_iso(),
            }
        return out

    @staticmethod
    def _count_nodes_status(nodes_status: Dict[str, Any]) -> Dict[str, int]:
        counts = {"ok": 0, "needs_repair": 0, "degraded": 0, "invalid": 0}
        for payload in dict(nodes_status or {}).values():
            status = str((payload or {}).get("status") or "").strip()
            if status in counts:
                counts[status] = int(counts.get(status) or 0) + 1
        return counts

    async def _run_validate_operation(
        self,
        *,
        root: str,
        mode: str,
        cli_runner: Optional[DocRunner],
        trigger_repair: bool,
    ) -> Dict[str, Any]:
        if not root or not os.path.isdir(root):
            return CodebaseMapResult(status="failed", mode=mode, reason="workdir_not_found").as_dict()
        map_dir = self._map_dir(root)
        meta_path = os.path.join(map_dir, "meta.json")
        if not os.path.isdir(map_dir):
            return CodebaseMapResult(status="failed", mode=mode, reason="map_not_initialized").as_dict()

        # Проверка и восстановление отсутствующих статических документов
        missing_static_docs = self._restore_missing_static_docs(map_dir=map_dir, root=root)

        graph_path = os.path.join(map_dir, _GRAPH_FILE)
        state = self._read_graph_state(map_dir)
        graph = self._read_json_file(graph_path)
        node_entries = [dict(x or {}) for x in list(graph.get("nodes") or []) if isinstance(x, dict)]
        tree_lines = list(state.get("tree") or graph.get("tree") or [])
        validation = self._validate_instruction_graph(
            map_dir=map_dir,
            node_entries=node_entries,
            tree_lines=tree_lines,
        )
        existing_nodes_status = dict(state.get("nodes_status") or {})
        validate_queue = [str(x) for x in list(validation.get("invalid_nodes") or []) if str(x).strip()]
        nodes_status = self._merge_nodes_status(
            node_entries=node_entries,
            existing_status=existing_nodes_status,
            invalid_nodes=validate_queue,
            invalid_reasons=dict(validation.get("invalid_reasons") or {}),
        )
        repair_queue = [
            rel for rel, payload in nodes_status.items()
            if str((payload or {}).get("status") or "").strip() == "needs_repair"
        ]
        state["validation_report"] = dict(validation)
        state["validate_queue"] = validate_queue
        state["repair_queue"] = repair_queue
        state["nodes_status"] = nodes_status
        needs_review = [str(x) for x in list(state.get("needs_review") or []) if str(x).strip()]
        rules_needs_review = [str(x) for x in list(state.get("rules_needs_review") or []) if str(x).strip()]
        all_needs_review = needs_review + [x for x in rules_needs_review if x not in needs_review]
        all_needs_review.extend([x for x in repair_queue if x not in all_needs_review])
        state["state"] = (
            CODEBASE_MAPPER_GRAPH_STATE["READY"]
            if not all_needs_review
            else CODEBASE_MAPPER_GRAPH_STATE["NEEDS_REVIEW"]
        )
        state["updated_at"] = self._utc_now_iso()
        self._write_graph_state(map_dir, state)

        repaired = 0
        if trigger_repair and repair_queue and cli_runner is not None:
            repaired = await self._run_repair_queue(
                root=root,
                map_dir=map_dir,
                cli_runner=cli_runner,
                max_items=50,
            )
            state = self._read_graph_state(map_dir)

        return {
            "status": CODEBASE_MAPPER_RESULT_STATUS["VALIDATION_DONE"],
            "mode": mode,
            "reason": "validation_completed",
            "map_dir": map_dir,
            "meta_path": meta_path,
            "changed_files": [],
            "updated_docs": list(missing_static_docs),
            "graph_state": str((state or {}).get("state") or ""),
            "graph_nodes": int((state or {}).get("nodes_count") or len(node_entries)),
            "graph_tree": list((state or {}).get("tree") or tree_lines),
            "operation": "validate",
            "validate_queue": list((state or {}).get("validate_queue") or []),
            "repair_queue": list((state or {}).get("repair_queue") or []),
            "repair_processed": int(repaired),
        }

    async def _run_repair_operation(
        self,
        *,
        root: str,
        mode: str,
        cli_runner: Optional[DocRunner],
    ) -> Dict[str, Any]:
        if not root or not os.path.isdir(root):
            return CodebaseMapResult(status="failed", mode=mode, reason="workdir_not_found").as_dict()
        map_dir = self._map_dir(root)
        meta_path = os.path.join(map_dir, "meta.json")
        if not os.path.isdir(map_dir):
            return CodebaseMapResult(status="failed", mode=mode, reason="map_not_initialized").as_dict()
        if cli_runner is None:
            return CodebaseMapResult(status="failed", mode=mode, reason="repair_requires_cli").as_dict()
        processed = await self._run_repair_queue(root=root, map_dir=map_dir, cli_runner=cli_runner, max_items=50)
        state = self._read_graph_state(map_dir)
        return {
            "status": CODEBASE_MAPPER_RESULT_STATUS["REPAIR_DONE"],
            "mode": mode,
            "reason": "repair_completed",
            "map_dir": map_dir,
            "meta_path": meta_path,
            "changed_files": [],
            "updated_docs": [],
            "graph_state": str((state or {}).get("state") or ""),
            "graph_nodes": int((state or {}).get("nodes_count") or 0),
            "graph_tree": list((state or {}).get("tree") or []),
            "operation": "repair",
            "repair_processed": int(processed),
            "repair_queue": list((state or {}).get("repair_queue") or []),
        }

    async def _run_repair_queue(
        self,
        *,
        root: str,
        map_dir: str,
        cli_runner: DocRunner,
        max_items: int,
    ) -> int:
        state = self._read_graph_state(map_dir)
        queue = [str(x) for x in list(state.get("repair_queue") or []) if str(x).strip()]
        if not queue:
            return 0
        nodes_status = dict(state.get("nodes_status") or {})
        processed = 0
        remaining: List[str] = []
        for node_rel in queue[: max(1, int(max_items or 1))]:
            node_abs_rel = f"{_MAP_REL_ROOT}/{node_rel}"
            prompt = (
                "Codebase Mapper repair task.\n"
                f"Project root: `{root}`\n"
                f"Map root: `{map_dir}`\n"
                f"Target node: `{node_abs_rel}`\n"
                "Fix node in place, keep markdown concise.\n"
                "Required sections: Purpose, Scope, Instructions for agent, Source of truth, "
                "When to update, Related nodes, Last reviewed.\n"
                "Ensure links in Related nodes exist. Do not touch unrelated files."
            )
            task = {
                "focus": "repair",
                "target_docs": [node_rel],
                "prompt": prompt,
                "map_dir": map_dir,
                "full_scan": False,
                "changed_files": [node_rel],
            }
            try:
                result = await cli_runner(task)
            except Exception as e:
                result = {"success": False, "error": str(e)}
            processed += 1
            node_payload = dict(nodes_status.get(node_rel) or {})
            attempts = int(node_payload.get("repair_attempts") or 0) + 1
            success = bool((result or {}).get("success"))
            if not success:
                node_payload["repair_attempts"] = attempts
                if attempts >= 2:
                    node_payload["status"] = "degraded"
                    err = str((result or {}).get("error") or "repair_failed").strip()
                    node_payload["last_error"] = err
                else:
                    node_payload["status"] = "needs_repair"
                    remaining.append(node_rel)
                nodes_status[node_rel] = node_payload
                continue
            validation = self._validate_instruction_graph(
                map_dir=map_dir,
                node_entries=[{"path": node_rel}],
                tree_lines=list(state.get("tree") or []),
            )
            invalid = set(str(x) for x in list(validation.get("invalid_nodes") or []))
            if node_rel in invalid:
                node_payload["repair_attempts"] = attempts
                err = "; ".join(list(dict(validation.get("invalid_reasons") or {}).get(node_rel) or [])) or "validation_failed_after_repair"
                node_payload["last_error"] = err
                if attempts >= 2:
                    node_payload["status"] = "degraded"
                else:
                    node_payload["status"] = "needs_repair"
                    remaining.append(node_rel)
            else:
                node_payload["status"] = "ok"
                node_payload["repair_attempts"] = 0
                node_payload["last_error"] = ""
            node_payload["updated_at"] = self._utc_now_iso()
            nodes_status[node_rel] = node_payload
        for node_rel in queue[max_items:]:
            remaining.append(node_rel)
        state["nodes_status"] = nodes_status
        state["repair_queue"] = remaining
        state["validate_queue"] = [n for n, p in nodes_status.items() if str((p or {}).get("status") or "") in {"needs_repair", "invalid"}]
        needs_review = [str(x) for x in list(state.get("needs_review") or []) if str(x).strip()]
        rules_needs_review = [str(x) for x in list(state.get("rules_needs_review") or []) if str(x).strip()]
        all_needs_review = needs_review + [x for x in rules_needs_review if x not in needs_review]
        all_needs_review.extend([x for x in remaining if x not in all_needs_review])
        state["state"] = (
            CODEBASE_MAPPER_GRAPH_STATE["READY"]
            if not all_needs_review
            else CODEBASE_MAPPER_GRAPH_STATE["NEEDS_REVIEW"]
        )
        state["updated_at"] = self._utc_now_iso()
        self._write_graph_state(map_dir, state)
        return processed

    def _review_touched_items(
        self,
        *,
        review_items: Sequence[str],
        changed_files: Sequence[str],
        operation: str,
        is_first_init: bool = False,
    ) -> List[str]:
        if operation in {"init", "init_full"}:
            if is_first_init:
                return []
            return list(review_items)
        changed = [str(p).replace("\\", "/").strip("/") for p in list(changed_files or []) if str(p).strip()]
        if not changed:
            return []
        touched_domains = {p.split("/", 1)[0] for p in changed}
        touched: List[str] = []
        for item in review_items:
            name = str(item).replace("\\", "/").split("/")[-1]
            if not name.endswith(".md"):
                continue
            slug = name[:-3]
            if slug in {"workspace"}:
                if changed:
                    touched.append(item)
                continue
            domain = slug.replace("-", "_")
            if slug in touched_domains or domain in touched_domains:
                touched.append(item)
        return touched

    def _render_graph_index(
        self,
        *,
        node_entries: Sequence[Dict[str, Any]],
        map_dir: str,
        changed_files: Sequence[str],
    ) -> str:
        doc_descriptions = {
            "STACK.md": "Технологический стек, зависимости, рантаймы и инфраструктурные маркеры.",
            "INTEGRATIONS.md": "Внешние/внутренние интеграции, точки входа и контракты взаимодействий.",
            "ARCHITECTURE.md": "Архитектурная структура модулей, слои и их ответственность.",
            "STRUCTURE.md": "Физическая структура репозитория и индексация значимых путей.",
            "CONVENTIONS.md": "Кодовые конвенции, практики и стандарты реализации.",
            "TESTING.md": "Подход к тестированию, расположение тестов и проверочные правила.",
            "CONCERNS.md": "Риски, технический долг и зоны повышенного внимания.",
        }
        lines = [
            "# Codebase Mapper Instruction Graph",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            "This index is the entrypoint for agent instructions.",
            "",
            "## Mandatory Workflow",
            "1. Before any edits, read this `INDEX.md` completely.",
            "2. Determine relevant area(s) and open matching files under `.cli-proxy/.codebase_map/nodes/*.md`.",
            "3. Only then inspect source files and implement changes.",
            "4. After changes, update affected node metadata (`When to update`, `Last reviewed`).",
            "5. If node update fails, run targeted repair for that node.",
            "",
            "## Runtime Verification and Fallback Policy (Hardcoded)",
            (
                "- Перед любым утверждением о runtime-поведении ОБЯЗАТЕЛЬНО "
                "проверить конкретный метод/функцию в коде и сослаться на файл:строка."
            ),
            (
                "- Запрещено делать выводы по аналогии между этапами пайплайна без "
                "прямой проверки каждого этапа (decompose/dev/review/final audit)."
            ),
            "- Если вопрос про «кто/когда вызывается», отвечать в формате пошаговой цепочки: шаг -> метод -> исполнитель -> зачем.",
            "- При обнаружении своей неточности сначала коротко исправить факт, затем дать проверенные ссылки на код, без догадок.",
            "- Policy matrix по fallback:",
            (
                "- Legacy-потоки (уже существующее поведение в проде): fallback "
                "разрешён для обратной совместимости, но должен логироваться и быть "
                "явно отражён в отчёте."
            ),
            (
                "- Новый функционал и новые mode-сценарии: fallback запрещён по "
                "умолчанию; при ошибке — явный fail с причиной."
            ),
            (
                "- Opt-in fallback: разрешён только после явного согласования с "
                "пользователем в текущей задаче или если он явно приходит как "
                "требование от пользователя."
            ),
            "",
            "## Runtime Files",
            "- `graph.json`: topology and edges.",
            "- `rules.yaml`: update routing rules.",
            "- `state.json`: statuses/queues (`ok|needs_repair|degraded|invalid`).",
            "- `api/`: optional technical interface mirror.",
            "",
            "## Core Docs",
            "These files are mandatory context and must be considered before major edits.",
        ]
        for name in _DOC_NAMES:
            lines.append(f"- `{name}`: {doc_descriptions.get(name, 'Core project context.')}")  # deterministic catalog
        lines.extend([
            "",
            "## Nodes",
        ])
        for item in node_entries:
            title = str(item.get("title") or "")
            path = str(item.get("path") or "")
            count = int(item.get("file_count") or 0)
            source_glob = str(item.get("source_glob") or "**")
            lines.append(f"- [{title}]({path}) - files: {count}, source_glob: `{source_glob}`")
        lines.extend([
            "",
            "## Runtime Inputs",
            f"- map_dir: `{map_dir}`",
            f"- changed_files: {len(list(changed_files or []))}",
            "",
        ])
        return "\n".join(lines)

    async def _enrich_graph_nodes_with_cli(
        self,
        *,
        root: str,
        map_dir: str,
        node_paths: Sequence[str],
        changed_files: Sequence[str],
        cli_runner: DocRunner,
        max_items: int,
    ) -> None:
        unique_nodes: List[str] = []
        for rel in list(node_paths or []):
            node_rel = str(rel or "").replace("\\", "/").strip("/")
            if not node_rel.startswith("nodes/") or not node_rel.endswith(".md"):
                continue
            if node_rel in unique_nodes:
                continue
            if not os.path.exists(os.path.join(map_dir, node_rel)):
                continue
            unique_nodes.append(node_rel)
        if not unique_nodes:
            return
        for node_rel in unique_nodes[: max(1, int(max_items or 1))]:
            node_abs_rel = f"{_MAP_REL_ROOT}/{node_rel}"
            prompt = (
                "Codebase Mapper node enrichment task.\n"
                f"Project root: `{root}`\n"
                f"Map root: `{map_dir}`\n"
                f"Node file: `{node_abs_rel}`\n"
                "Enrich only this node in place.\n"
                "Required sections: Purpose, Scope, Instructions for agent, Source of truth, "
                "When to update, Related nodes, Last reviewed.\n"
                "Use concrete repository paths. No speculation. Keep concise.\n"
            )
            if changed_files:
                prompt += "Changed files context:\n" + "\n".join(f"- `{p}`" for p in list(changed_files)[:40])
            task = {
                "focus": "node_enrich",
                "target_docs": [node_rel],
                "prompt": prompt,
                "map_dir": map_dir,
                "full_scan": False,
                "changed_files": list(changed_files or []),
            }
            try:
                await cli_runner(task)
            except Exception:
                self._log.exception("mapper node enrich failed node=%s", node_rel)

    def _render_graph_node(
        self,
        *,
        domain: str,
        rel_node_path: str,
        source_glob: str,
        file_count: int,
        source_samples: Sequence[str],
        related_node_paths: Sequence[str],
        related_source_globs: Sequence[str],
        related_relation_notes: Sequence[str],
        changed_hits: Sequence[str],
        api_links: Sequence[tuple[str, str]] = (),
    ) -> str:
        hits = list(changed_hits or [])
        samples = [str(x) for x in list(source_samples or []) if str(x).strip()]
        related_nodes = [str(x) for x in list(related_node_paths or []) if str(x).strip() and str(x) != rel_node_path]
        related_globs = [str(x) for x in list(related_source_globs or []) if str(x).strip()]
        relation_notes = [str(x) for x in list(related_relation_notes or []) if str(x).strip()]
        lines = [
            f"# Node: {domain}",
            "",
            f"Generated: {self._utc_now_iso()}",
            "",
            "## Purpose",
            f"Instruction node for `{domain}` area.",
            "",
            "## Scope",
            f"- Source glob: `{source_glob}`",
            f"- Estimated files: {int(file_count)}",
            "",
            "## Instructions for agent",
            "- Read only files relevant to the active task.",
            "- Prefer deterministic checks before edits.",
            "- Keep changes minimal and validate with tests/linters where applicable.",
            "",
            "## Source of truth",
            f"- `{source_glob}`",
            *[f"- `{p}`" for p in samples[:10]],
            "",
        ]

        if api_links:
            lines.extend(["## Module API", "Детальные интерфейсы модулей этой области:", ""])
            for orig, link in api_links:
                lines.append(f"- [{orig}]({link})")
            lines.append("")

        lines.extend([
            "## When to update",
            f"- Any commit touching `{source_glob}`.",
            *[f"- Any commit touching `{g}` because this node has import/call dependency on it." for g in related_globs[:5]],
            "- Any architecture or behavior change affecting this area.",
            "",
            "## Related nodes",
            *([f"- `{p}`" for p in related_nodes[:8]] if related_nodes else ["- (none)"]),
            *([f"- {note}" for note in relation_notes[:8]] if relation_notes else []),
            "",
            "## Owner",
            "- project-maintainers",
            "",
            "## Last reviewed",
            f"- {self._utc_now_iso()}",
            "",
        ])
        if hits:
            lines.extend(["## Recent changed files", *[f"- `{p}`" for p in hits[:30]], ""])
        return "\n".join(lines)

    def _build_graph_edges(
        self,
        *,
        node_entries: Sequence[Dict[str, Any]],
        domain_relations: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> List[Dict[str, str]]:
        edges: List[Dict[str, str]] = []
        by_title = {str(item.get("title") or ""): str(item.get("id") or "") for item in node_entries}
        for item in node_entries:
            node_id = str(item.get("id") or "")
            if not node_id:
                continue
            edges.append({"from": "index", "to": node_id, "type": "references"})
        for domain, related in domain_relations.items():
            src = str(by_title.get(domain) or "")
            if not src:
                continue
            for rel in sorted(dict(related or {}).keys()):
                dst = str(by_title.get(rel) or "")
                if not dst or dst == src:
                    continue
                relation = dict((related or {}).get(rel) or {})
                edge = {
                    "from": src,
                    "to": dst,
                    "type": "related",
                    "confidence": f"{float(relation.get('score') or 0.0):.2f}",
                    "levels": ",".join(sorted([str(x) for x in list(relation.get("levels") or []) if str(x).strip()])),
                }
                edges.append(edge)
        return edges

    def _build_domain_file_index(
        self,
        *,
        files: Sequence[str],
        domains: Sequence[str],
    ) -> Dict[str, List[str]]:
        known = {str(x): [] for x in list(domains or [])}
        for raw in list(files or []):
            p = str(raw).replace("\\", "/").strip("/")
            if not p:
                continue
            area = self._path_to_area(p)
            if area in known:
                known[area].append(p)
            if "workspace" in known:
                known["workspace"].append(p)
        return {k: sorted(v) for k, v in known.items()}

    def _select_domain_source_samples(
        self,
        *,
        domain: str,
        domain_files: Sequence[str],
        changed_files: Sequence[str],
        operation: str,
    ) -> List[str]:
        files = [str(p).replace("\\", "/").strip("/") for p in list(domain_files or []) if str(p).strip()]
        if not files:
            return []
        # Preserve deterministic order and uniqueness.
        unique_files = list(dict.fromkeys(files))
        cap = self._domain_source_samples_cap(total=len(unique_files), operation=operation)
        if len(unique_files) <= cap:
            return unique_files

        out: List[str] = []
        selected: Set[str] = set()

        def _pick(path: str) -> None:
            if path in selected:
                return
            selected.add(path)
            out.append(path)

        def _pick_many(paths: Sequence[str]) -> None:
            for p in paths:
                if len(out) >= cap:
                    return
                _pick(str(p))

        normalized_changed = [
            str(p).replace("\\", "/").strip("/")
            for p in list(changed_files or [])
            if str(p).strip()
        ]
        changed_in_domain = [
            p for p in normalized_changed
            if p == domain or p.startswith(f"{domain}/")
        ]
        existing = set(unique_files)
        _pick_many([p for p in changed_in_domain if p in existing])

        first_by_subdir: Dict[str, str] = {}
        first_by_ext: Dict[str, str] = {}
        for path in unique_files:
            rel = path
            if path == domain:
                rel = ""
            elif path.startswith(f"{domain}/"):
                rel = path[len(domain) + 1:]
            parent = str(rel).split("/", 1)[0] if rel and "/" in rel else "(root)"
            if parent not in first_by_subdir:
                first_by_subdir[parent] = path
            ext = os.path.splitext(path)[1].lower() or "(none)"
            if ext not in first_by_ext:
                first_by_ext[ext] = path

        _pick_many(list(first_by_subdir.values()))
        _pick_many(list(first_by_ext.values()))
        _pick_many(unique_files)
        return out[:cap]

    @staticmethod
    def _domain_source_samples_cap(*, total: int, operation: str) -> int:
        n = max(0, int(total or 0))
        op = str(operation or "").strip().lower()
        if op in {"verify", "init_full"}:
            minimum = _SOURCE_SAMPLES_MIN_DEEP
            maximum = _SOURCE_SAMPLES_MAX_DEEP
        else:
            minimum = _SOURCE_SAMPLES_MIN
            maximum = _SOURCE_SAMPLES_MAX
        if n <= 0:
            return minimum
        adaptive = int(math.sqrt(n) * 3)
        return max(minimum, min(maximum, adaptive))

    def _infer_domain_relations(
        self,
        *,
        root: str,
        domain_files: Dict[str, List[str]],
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        domains = [str(d) for d in domain_files.keys() if str(d) and str(d) != "workspace"]
        out: Dict[str, Dict[str, Dict[str, Any]]] = {d: {} for d in domains}

        # L0: observed co-change from git history (works for any language).
        for a, b, conf, evidence in self._extract_observed_pairs_from_git(root=root):
            if a in out and b in out:
                self._merge_relation(
                    out=out,
                    source=a,
                    target=b,
                    level="L0",
                    score=float(conf),
                    evidence=list(evidence or []),
                )
                self._merge_relation(
                    out=out,
                    source=b,
                    target=a,
                    level="L0",
                    score=float(conf),
                    evidence=list(evidence or []),
                )

        # L1: regex-based import/include/use patterns for many languages.
        self._collect_regex_relations(root=root, domain_files=domain_files, out=out)

        # L2: precise Python AST layer.
        aliases: Dict[str, str] = {}
        for domain in domains:
            aliases[domain] = domain
            if domain.endswith(".py"):
                aliases[domain[:-3]] = domain
            aliases[domain.replace("-", "_")] = domain
            aliases[domain.replace("_", "-")] = domain
        for domain in domains:
            for rel in list(domain_files.get(domain) or [])[:400]:
                abs_path = os.path.join(root, rel)
                low = str(rel).lower()
                parsed: Set[str] = set()
                if low.endswith(".py"):
                    parsed = self._extract_python_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
                elif low.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
                    parsed = self._extract_ts_js_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
                elif low.endswith(".php"):
                    parsed = self._extract_php_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
                elif low.endswith(".go"):
                    parsed = self._extract_go_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
                elif low.endswith(".rs"):
                    parsed = self._extract_rust_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
                if not parsed:
                    continue
                for target in parsed:
                    if target == domain or target not in out:
                        continue
                    self._merge_relation(
                        out=out,
                        source=domain,
                        target=target,
                        level="L2",
                        score=0.9,
                        evidence=[f"lang_specific:{rel}"],
                    )
        return out

    def _collect_regex_relations(
        self,
        *,
        root: str,
        domain_files: Dict[str, List[str]],
        out: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> None:
        known = set(out.keys())
        patterns = [
            re.compile(r"(?:import|from|require|require_once|include|include_once|use|mod)\s+['\"]([^'\"]+)['\"]"),
            re.compile(r"(?:import|from|require|require_once|include|include_once|use|mod)\s+([A-Za-z0-9_./\\\\:-]+)"),
        ]
        for domain in sorted(known):
            for rel in list(domain_files.get(domain) or [])[:500]:
                low = str(rel).lower()
                if not low.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".php", ".go", ".rs", ".java", ".kt")):
                    continue
                abs_path = os.path.join(root, rel)
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    self._log.exception("codebase map: regex relation read failed %s", abs_path)
                    continue
                refs = self._extract_domain_refs_by_regex(content=content, known_domains=known, patterns=patterns)
                for target in refs:
                    if target == domain:
                        continue
                    self._merge_relation(
                        out=out,
                        source=domain,
                        target=target,
                        level="L1",
                        score=0.62,
                        evidence=[f"regex_ref:{rel}"],
                    )

    @staticmethod
    def _extract_domain_refs_by_regex(
        *,
        content: str,
        known_domains: Set[str],
        patterns: Sequence[re.Pattern[str]],
    ) -> Set[str]:
        out: Set[str] = set()
        text = str(content or "")
        for pat in list(patterns or []):
            for m in pat.findall(text):
                raw = str(m or "").replace("\\", "/").strip().strip("./")
                if not raw:
                    continue
                token = raw.split("/", 1)[0].split(".", 1)[0].strip().lower()
                if token in known_domains:
                    out.add(token)
        return out

    @staticmethod
    def _merge_relation(
        *,
        out: Dict[str, Dict[str, Dict[str, Any]]],
        source: str,
        target: str,
        level: str,
        score: float,
        evidence: Sequence[str],
    ) -> None:
        src = str(source or "")
        dst = str(target or "")
        if not src or not dst or src == dst:
            return
        rels = out.setdefault(src, {})
        current = dict(rels.get(dst) or {})
        prev_score = float(current.get("score") or 0.0)
        cur_levels = set(str(x) for x in list(current.get("levels") or []) if str(x).strip())
        cur_evidence = [str(x) for x in list(current.get("evidence") or []) if str(x).strip()]
        cur_levels.add(str(level))
        for ev in list(evidence or []):
            s = str(ev).strip()
            if s and s not in cur_evidence:
                cur_evidence.append(s)
        rels[dst] = {
            "score": round(max(prev_score, float(score or 0.0)), 2),
            "levels": sorted(cur_levels),
            "evidence": cur_evidence[:8],
        }

    @staticmethod
    def _format_relation_note(*, target: str, relation: Dict[str, Any]) -> str:
        score = float((relation or {}).get("score") or 0.0)
        levels = [str(x) for x in list((relation or {}).get("levels") or []) if str(x).strip()]
        lv = "/".join(levels) if levels else "unknown"
        return f"`{target}` confidence={score:.2f} via {lv}"

    def _relation_graph_payload(
        self,
        *,
        domain_relations: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for src, targets in domain_relations.items():
            entries: List[Dict[str, Any]] = []
            for dst in sorted(dict(targets or {}).keys()):
                rel = dict((targets or {}).get(dst) or {})
                entries.append(
                    {
                        "target": dst,
                        "score": float(rel.get("score") or 0.0),
                        "levels": list(rel.get("levels") or []),
                        "evidence": list(rel.get("evidence") or []),
                    }
                )
            out[str(src)] = entries[:20]
        return out

    def _extract_python_dependency_aliases(
        self,
        *,
        abs_path: str,
        known_aliases: Dict[str, str],
    ) -> Set[str]:
        out: Set[str] = set()
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=abs_path)
        except Exception:
            self._log.exception("codebase map: failed to parse python file %s", abs_path)
            return out
        imported_aliases: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = str(alias.name or "").split(".", 1)[0].strip()
                    if not top:
                        continue
                    target = str(known_aliases.get(top) or "")
                    if not target:
                        continue
                    out.add(target)
                    local_alias = str(alias.asname or top).strip()
                    if local_alias:
                        imported_aliases[local_alias] = target
            elif isinstance(node, ast.ImportFrom):
                module = str(getattr(node, "module", "") or "").strip()
                top = module.split(".", 1)[0] if module else ""
                target = str(known_aliases.get(top) or "")
                if target:
                    out.add(target)
                for alias in list(getattr(node, "names", []) or []):
                    local_alias = str(getattr(alias, "asname", None) or getattr(alias, "name", "") or "").strip()
                    if local_alias and target:
                        imported_aliases[local_alias] = target
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            head = self._resolve_call_head_name(node.func)
            if not head:
                continue
            target = str(imported_aliases.get(head) or known_aliases.get(head) or "")
            if target:
                out.add(target)
        return out

    def _extract_ts_js_dependency_aliases(
        self,
        *,
        abs_path: str,
        known_aliases: Dict[str, str],
    ) -> Set[str]:
        out: Set[str] = set()
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception:
            self._log.exception("codebase map: failed to read ts/js file %s", abs_path)
            return out
        patterns = [
            re.compile(r"import\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]"),
            re.compile(r"export\s+[^;]*?\s+from\s+['\"]([^'\"]+)['\"]"),
            re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"),
            re.compile(r"import\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        ]
        for pat in patterns:
            for raw in pat.findall(source):
                token = self._resolve_alias_token(str(raw), known_aliases=known_aliases)
                if token:
                    out.add(token)
        return out

    def _extract_php_dependency_aliases(
        self,
        *,
        abs_path: str,
        known_aliases: Dict[str, str],
    ) -> Set[str]:
        out: Set[str] = set()
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception:
            self._log.exception("codebase map: failed to read php file %s", abs_path)
            return out
        use_pat = re.compile(r"\buse\s+([A-Za-z_\\\\][A-Za-z0-9_\\\\]*)\s*;")
        for raw in use_pat.findall(source):
            token = str(raw).replace("\\", "/").strip().strip("/")
            head = token.split("/", 1)[0].lower()
            mapped = str(known_aliases.get(head) or "")
            if mapped:
                out.add(mapped)
        include_pats = [
            re.compile(r"\b(?:require|require_once|include|include_once)\s*\(?\s*['\"]([^'\"]+)['\"]\s*\)?"),
        ]
        for pat in include_pats:
            for raw in pat.findall(source):
                token = self._resolve_alias_token(str(raw), known_aliases=known_aliases)
                if token:
                    out.add(token)
        return out

    def _extract_go_dependency_aliases(
        self,
        *,
        abs_path: str,
        known_aliases: Dict[str, str],
    ) -> Set[str]:
        out: Set[str] = set()
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception:
            self._log.exception("codebase map: failed to read go file %s", abs_path)
            return out
        patterns = [
            re.compile(r'import\s+"([^"]+)"'),
            re.compile(r'import\s*\((.*?)\)', re.DOTALL),
        ]
        for raw in patterns[0].findall(source):
            token = self._resolve_alias_token(str(raw), known_aliases=known_aliases)
            if token:
                out.add(token)
        for block in patterns[1].findall(source):
            for raw in re.findall(r'"([^"]+)"', str(block or "")):
                token = self._resolve_alias_token(str(raw), known_aliases=known_aliases)
                if token:
                    out.add(token)
        return out

    def _extract_rust_dependency_aliases(
        self,
        *,
        abs_path: str,
        known_aliases: Dict[str, str],
    ) -> Set[str]:
        out: Set[str] = set()
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception:
            self._log.exception("codebase map: failed to read rust file %s", abs_path)
            return out
        use_pat = re.compile(r'\buse\s+([A-Za-z0-9_:\{\}]+)\s*;')
        mod_pat = re.compile(r'\bmod\s+([A-Za-z0-9_]+)\s*;')
        for raw in use_pat.findall(source):
            token = str(raw or "").replace("::", "/").replace("{", "/").replace("}", "").strip("/")
            head = token.split("/", 1)[0].strip().lower()
            mapped = str(known_aliases.get(head) or "")
            if mapped:
                out.add(mapped)
        for raw in mod_pat.findall(source):
            head = str(raw or "").strip().lower()
            mapped = str(known_aliases.get(head) or "")
            if mapped:
                out.add(mapped)
        return out

    @staticmethod
    def _resolve_alias_token(raw: str, *, known_aliases: Dict[str, str]) -> str:
        ref = str(raw or "").replace("\\", "/").strip().strip("./")
        if not ref:
            return ""
        head = ref.split("/", 1)[0].split(".", 1)[0].strip().lower()
        if not head:
            return ""
        return str(known_aliases.get(head) or "")

    @staticmethod
    def _resolve_call_head_name(func: ast.AST) -> str:
        cur = func
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        if isinstance(cur, ast.Name):
            return str(cur.id or "").strip()
        return ""

    def _infer_organizational_rules(
        self,
        *,
        root: str,
        snapshot: Dict[str, Any],
        node_entries: Sequence[Dict[str, Any]],
        reviewed_rules: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        areas = self._build_area_candidates(snapshot=snapshot, node_entries=node_entries)
        declared = self._extract_declared_pairs_from_markdown(root=root, areas=areas)
        observed = self._extract_observed_pairs_from_git(root=root)

        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for a, b, conf, evidence in observed:
            key = tuple(sorted((a, b)))
            merged[key] = {
                "areas": [key[0], key[1]],
                "source_class": "observed",
                "confidence": float(conf),
                "evidence": list(evidence),
            }

        for a, b, conf, evidence in declared:
            key = tuple(sorted((a, b)))
            current = merged.get(key)
            if current is None:
                current = {
                    "areas": [key[0], key[1]],
                    "source_class": "declared",
                    "confidence": float(conf),
                    "evidence": [],
                }
            else:
                current["source_class"] = "declared"
                current["confidence"] = max(float(current.get("confidence") or 0.0), float(conf))
            current["evidence"] = list(current.get("evidence") or []) + list(evidence)
            merged[key] = current

        out: List[Dict[str, Any]] = []
        for key in sorted(merged.keys()):
            item = dict(merged[key])
            a, b = key
            rule_id = f"rule-sync-{self._slugify_name(a)}-{self._slugify_name(b)}"
            confidence = float(item.get("confidence") or 0.0)
            evidence = [str(x) for x in list(item.get("evidence") or []) if str(x).strip()]
            source_class = str(item.get("source_class") or "observed")
            multi_source = len({ev.split(":", 1)[0] for ev in evidence}) >= 2
            if source_class == "declared":
                status = "active"
                needs_review = False
            else:
                status = "active" if confidence >= 0.8 and multi_source else "proposed"
                needs_review = status != "active"
            if bool(reviewed_rules.get(rule_id)):
                status = "active"
                needs_review = False
            out.append(
                {
                    "id": rule_id,
                    "kind": "sync_areas",
                    "title": f"Sync `{a}` with `{b}`",
                    "areas": [a, b],
                    "source_class": source_class,
                    "confidence": round(confidence, 2),
                    "status": status,
                    "needs_review": bool(needs_review),
                    "evidence": evidence[:10],
                }
            )
        return out

    def _build_area_candidates(
        self,
        *,
        snapshot: Dict[str, Any],
        node_entries: Sequence[Dict[str, Any]],
    ) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for item in node_entries:
            title = str((item or {}).get("title") or "").strip()
            glob = str((item or {}).get("source_glob") or "").strip()
            if not title or not glob:
                continue
            out[title.lower()] = glob
        files = list(snapshot.get("files") or [])
        for path in files:
            p = str(path).replace("\\", "/").strip("/")
            if not p or self._is_map_relative_path(p):
                continue
            if "/" not in p:
                if p.endswith(".md"):
                    continue
                if "." in p:
                    out.setdefault(p.lower(), p)
                    continue
            first = p.split("/", 1)[0]
            if first.endswith(".md"):
                continue
            if first not in {".git", ".venv", "__pycache__", "node_modules"}:
                out.setdefault(first.lower(), f"{first}/**")
        return out

    def _extract_declared_pairs_from_markdown(self, *, root: str, areas: Dict[str, str]) -> List[Tuple[str, str, float, List[str]]]:
        markdown_files = self._collect_markdown_files(root=root)
        if not markdown_files:
            return []
        keywords = ("sync", "synchron", "доработ", "синхрон", "both", "both sides")
        pairs: Dict[Tuple[str, str], List[str]] = {}
        for rel in markdown_files[:300]:
            abs_path = os.path.join(root, rel)
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                self._log.exception("codebase map: failed to read markdown file %s", abs_path)
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for line in lines[:200]:
                lower = line.lower()
                if not any(k in lower for k in keywords):
                    continue
                hits = [name for name in areas.keys() if self._area_mentioned(line=lower, area=name)]
                if len(hits) < 2:
                    continue
                for i in range(len(hits)):
                    for j in range(i + 1, len(hits)):
                        a, b = sorted((hits[i], hits[j]))
                        key = (a, b)
                        pairs.setdefault(key, []).append(f"declared:{rel}")
        out: List[Tuple[str, str, float, List[str]]] = []
        for (a, b), evidence in pairs.items():
            out.append((a, b, 0.95, evidence[:5]))
        return out

    @staticmethod
    def _area_mentioned(*, line: str, area: str) -> bool:
        text = str(line or "").lower()
        token = str(area or "").strip().lower()
        if not text or not token:
            return False
        if any(ch for ch in token if not ch.isalnum()):
            return token in text
        return re.search(rf"\b{re.escape(token)}\b", text) is not None

    def _collect_markdown_files(self, *, root: str) -> List[str]:
        files = self._list_all_files(root)
        out: List[str] = []
        for path in files:
            p = str(path).replace("\\", "/").strip("/")
            if not p or not p.lower().endswith(".md"):
                continue
            if self._is_map_relative_path(p):
                continue
            out.append(p)
        return out

    def _extract_observed_pairs_from_git(self, *, root: str) -> List[Tuple[str, str, float, List[str]]]:
        proc = self._run_cmd(["git", "-C", root, "log", "--name-only", "--pretty=format:__COMMIT__", "-n", "120"])
        if proc.returncode != 0:
            return []
        commits: List[Set[str]] = []
        current: Set[str] = set()
        for line in (proc.stdout or "").splitlines():
            s = str(line or "").strip()
            if not s:
                continue
            if s == "__COMMIT__":
                if current:
                    commits.append(set(current))
                    current = set()
                continue
            area = self._path_to_area(s)
            if area:
                current.add(area)
        if current:
            commits.append(set(current))
        if not commits:
            return []
        pair_count: Dict[Tuple[str, str], int] = {}
        for commit_areas in commits:
            sorted_areas = sorted(commit_areas)
            if len(sorted_areas) < 2:
                continue
            for i in range(len(sorted_areas)):
                for j in range(i + 1, len(sorted_areas)):
                    key = (sorted_areas[i], sorted_areas[j])
                    pair_count[key] = int(pair_count.get(key) or 0) + 1
        out: List[Tuple[str, str, float, List[str]]] = []
        total = max(1, len(commits))
        for key, count in pair_count.items():
            if count < 2:
                continue
            base = 0.45 + min(0.35, 0.08 * count)
            support = min(0.2, 0.6 * (count / total))
            conf = min(0.95, base + support)
            out.append((key[0], key[1], conf, [f"observed:git_cochange:{count}/{total}"]))
        return out

    @staticmethod
    def _path_to_area(path: str) -> str:
        p = str(path or "").replace("\\", "/").strip("/")
        if not p or CodebaseMapperRuntime._is_map_relative_path(p):
            return ""
        if "/" not in p:
            return p if not p.endswith(".md") else ""
        first = p.split("/", 1)[0]
        if first in {".git", ".venv", "__pycache__", "node_modules"}:
            return ""
        return first

    @staticmethod
    def _area_to_source_glob(area: str) -> str:
        token = str(area or "").replace("\\", "/").strip("/")
        if not token or token == "workspace":
            return "**"
        # Top-level files must match the file itself, not "<file>/**".
        if "/" not in token and "." in token:
            return token
        return f"{token}/**"

    def _render_graph_rules(
        self,
        *,
        node_entries: Sequence[Dict[str, Any]],
        inferred_rules: Sequence[Dict[str, Any]],
    ) -> str:
        lines = [
            "version: 1",
            "node_rules:",
        ]
        for item in node_entries:
            node_id = str(item.get("id") or "")
            source_glob = str(item.get("source_glob") or "**")
            path = str(item.get("path") or "")
            lines.extend(
                [
                    f"  - id: update-{self._slugify_name(node_id)}",
                    "    when:",
                    "      any_path_matches:",
                    f"        - \"{source_glob}\"",
                    "    update:",
                    "      docs:",
                    f"        - \"{path}\"",
                    "    policy:",
                    "      severity: medium",
                    "      self_heal: true",
                ]
            )
        lines.append("organizational_rules:")
        if not inferred_rules:
            lines.append("  []")
        else:
            for rule in inferred_rules:
                rule_id = str((rule or {}).get("id") or "")
                status = str((rule or {}).get("status") or "proposed")
                source_class = str((rule or {}).get("source_class") or "observed")
                confidence = float((rule or {}).get("confidence") or 0.0)
                needs_review = bool((rule or {}).get("needs_review"))
                areas = [str(x) for x in list((rule or {}).get("areas") or []) if str(x).strip()]
                evidence = [str(x) for x in list((rule or {}).get("evidence") or []) if str(x).strip()]
                lines.extend(
                    [
                        f"  - id: {rule_id}",
                        "    kind: sync_areas",
                        f"    source_class: {source_class}",
                        f"    confidence: {confidence:.2f}",
                        f"    status: {status}",
                        f"    needs_review: {'true' if needs_review else 'false'}",
                        "    when:",
                        "      any_path_matches:",
                    ]
                )
                for area in areas:
                    lines.append(f"        - \"{self._area_to_source_glob(area)}\"")
                lines.extend(
                    [
                        "    then:",
                        "      enforce_sync_with:",
                    ]
                )
                for area in areas:
                    lines.append(f"        - \"{area}\"")
                lines.append("    evidence:")
                if not evidence:
                    lines.append("      []")
                else:
                    for ev in evidence:
                        lines.append(f"      - \"{ev}\"")
        lines.append("")
        return "\n".join(lines)

    def _sync_agents_md(self, *, root: str, map_dir: str) -> None:
        if not root or not os.path.isdir(root):
            return
        agents_path = os.path.join(root, "AGENTS.md")
        if not os.path.exists(agents_path):
            return
        try:
            with open(agents_path, "r", encoding="utf-8") as f:
                original = f.read()
        except Exception:
            self._log.exception("codebase map: failed to read AGENTS.md")
            return
        block = "\n".join(
            [
                "## Codebase Mapper Graph",
                "- Use `/.cli-proxy/.codebase_map/INDEX.md` as the entrypoint for project instructions.",
                "- Load only relevant files under `/.cli-proxy/.codebase_map/nodes/*.md`.",
                "- If code changes affect an area, update `Last reviewed` in the relevant node.",
                "- If update fails, run targeted repair (`update-node`/`repair`).",
                f"- Graph root: `{map_dir}`",
                "",
            ]
        )
        updated = self._upsert_managed_block(
            text=original,
            start_marker="<!-- CODEBASE_MAPPER_GRAPH:START -->",
            end_marker="<!-- CODEBASE_MAPPER_GRAPH:END -->",
            body=block,
        )
        if updated == original:
            return
        self._write_text(agents_path, updated)

    def _sanitize_concerns_doc(self, *, map_dir: str) -> None:
        path = os.path.join(str(map_dir or "").strip(), "CONCERNS.md")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            self._log.exception("codebase map: failed to read concerns doc")
            return
        sanitized = self._strip_review_sections_from_concerns(content)
        if sanitized == content:
            return
        self._write_text(path, sanitized)

    @staticmethod
    def _strip_review_sections_from_concerns(content: str) -> str:
        lines = str(content or "").splitlines()
        out: List[str] = []
        skip_level = 0
        marker_re = re.compile(r"(needs\s*review|review\s*items|на\s*ревью|список\s*на\s*ревью)", re.IGNORECASE)

        for line in lines:
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if skip_level > 0:
                if heading and len(heading.group(1)) <= skip_level:
                    skip_level = 0
                else:
                    continue
            if heading:
                level = len(heading.group(1))
                title = str(heading.group(2) or "").strip()
                if marker_re.search(title):
                    skip_level = level
                    continue
            out.append(line)

        text = "\n".join(out).strip("\n")
        return (text + "\n") if text else ""

    @staticmethod
    def _upsert_managed_block(*, text: str, start_marker: str, end_marker: str, body: str) -> str:
        src = str(text or "")
        start = src.find(start_marker)
        end = src.find(end_marker)
        managed = f"{start_marker}\n{body}{end_marker}\n"
        if start >= 0 and end > start:
            end_pos = end + len(end_marker)
            prefix = src[:start]
            suffix = src[end_pos:]
            if suffix.startswith("\n"):
                suffix = suffix[1:]
            out = f"{prefix}{managed}{suffix}"
            return out
        if src and not src.endswith("\n"):
            src += "\n"
        return f"{src}\n{managed}"

    @staticmethod
    def _write_text(path: str, content: str) -> None:
        rendered = str(content or "")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if f.read() == rendered:
                        return
            except Exception:
                # Keep write path robust even if read-before-write fails.
                pass
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp, path)

    @staticmethod
    def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if f.read() == rendered:
                        return
            except Exception:
                # Keep write path robust even if read-before-write fails.
                pass
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp, path)
