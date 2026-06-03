from __future__ import annotations

import asyncio
import types
from pathlib import Path

import yaml
import pytest

from session import SddState
from modes.sdd.handoff import seed_plan_from_tasks_md
from modes.sdd.packs.schema import PACK_SCHEMA_VERSION, PackSelection
from modes.sdd.project_init import (
    _select_and_persist_packs,
    classify_project,
    run_project_initialization,
)
from modes.sdd.schemas import PACKS_OUTPUT_SCHEMA


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not available on this filesystem")


class _FakeMapper:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.ready = False

    def get_status(self, *, workdir: str):
        if not self.ready:
            return {
                "status": "empty",
                "graph_initialized": False,
                "docs": [],
                "graph_state": "empty",
                "repair_queue": [],
                "degraded_nodes": [],
            }
        return {
            "status": "ready",
            "graph_initialized": True,
            "docs": ["ARCHITECTURE.md", "STACK.md"],
            "graph_state": "ready",
            "repair_queue": [],
            "degraded_nodes": [],
            "map_dir": str(Path(workdir) / ".cli-proxy" / ".codebase_map"),
        }

    async def maybe_run(self, **kwargs):
        self.calls.append(str(kwargs.get("operation") or ""))
        if self.fail:
            return {"status": "failed", "reason": "boom"}
        self.ready = True
        return {"status": "full_updated", "reason": "bootstrap"}

    def build_runtime_context(self, *, workdir: str, max_chars: int = 2000):
        return "ARCHITECTURE: demo\nSTACK: Rust"


def _session(tmp_path: Path):
    return types.SimpleNamespace(
        workdir=str(tmp_path),
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(codebase_mapper_usage="auto")),
        sdd=SddState(),
    )


def test_classify_project_empty_repo(tmp_path: Path) -> None:
    assert classify_project(str(tmp_path)).kind == "empty_repo"


def test_classify_project_existing_codebase(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    assert classify_project(str(tmp_path)).kind == "existing_codebase"


def test_project_init_empty_repo_writes_templates(tmp_path: Path) -> None:
    session = _session(tmp_path)

    result = asyncio.run(
        run_project_initialization(
            session=session,
            runtime_getter=lambda _cap: None,
            persist=lambda: None,
        )
    )

    assert result.kind == "empty_repo"
    assert session.sdd.project_init_status == "done"
    assert (tmp_path / "specs" / "_templates" / "spec.md").is_file()
    assert (tmp_path / "specs" / "_templates" / "plan.md").is_file()
    tasks_path = tmp_path / "specs" / "_templates" / "tasks.md"
    assert tasks_path.is_file()
    plan = seed_plan_from_tasks_md(str(tmp_path), str(tasks_path), "template-smoke")
    assert plan.project_goal
    assert plan.tasks


def test_project_init_empty_repo_seeds_constitution_scaffold(tmp_path: Path) -> None:
    session = _session(tmp_path)

    asyncio.run(
        run_project_initialization(
            session=session,
            runtime_getter=lambda _cap: None,
            persist=lambda: None,
        )
    )

    constitution = tmp_path / ".cli-proxy" / "constitution.md"
    assert constitution.is_file()
    body = constitution.read_text(encoding="utf-8")
    assert "Project Constitution" in body
    # Idempotent: a user-edited constitution must survive a re-init unchanged.
    constitution.write_text("# Custom\n", encoding="utf-8")
    asyncio.run(
        run_project_initialization(
            session=_session(tmp_path),
            runtime_getter=lambda _cap: None,
            persist=lambda: None,
        )
    )
    assert constitution.read_text(encoding="utf-8") == "# Custom\n"


def test_project_init_rejects_specs_symlink_outside_workdir(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-specs"
    outside.mkdir()
    _symlink_or_skip(tmp_path / "specs", outside)
    session = _session(tmp_path)

    with pytest.raises(RuntimeError, match="unsafe_sdd_output_path"):
        asyncio.run(
            run_project_initialization(
                session=session,
                runtime_getter=lambda _cap: None,
                persist=lambda: None,
            )
        )

    assert not (outside / "_project" / "project-init-snapshot.json").exists()
    assert not (outside / "_templates" / "spec.md").exists()


def test_project_init_existing_codebase_runs_mapper_before_artifacts(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"demo\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    mapper = _FakeMapper()
    session = _session(tmp_path)

    result = asyncio.run(
        run_project_initialization(
            session=session,
            runtime_getter=lambda _cap: mapper,
            persist=lambda: None,
        )
    )

    assert result.kind == "existing_codebase"
    assert mapper.calls == ["init"]
    assert session.sdd.project_init_status == "done"
    assert (tmp_path / "specs" / "_project" / "project-profile.generated.md").is_file()
    manifest = (tmp_path / "specs" / "_project" / "pack_manifest.json").read_text(encoding="utf-8")
    assert "rust-cargo" in manifest
    validation = (tmp_path / "specs" / "_project" / "validation.md").read_text(encoding="utf-8")
    assert "cargo check" in validation


def test_project_init_existing_codebase_mapper_failure_does_not_write_templates(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"demo\"\nversion = \"0.1.0\"\n", encoding="utf-8")
    mapper = _FakeMapper(fail=True)
    session = _session(tmp_path)

    with pytest.raises(RuntimeError):
        asyncio.run(
            run_project_initialization(
                session=session,
                runtime_getter=lambda _cap: mapper,
                persist=lambda: None,
            )
        )

    assert session.sdd.project_init_status == "failed"
    assert not (tmp_path / "specs" / "_templates" / "spec.md").exists()
    assert not (tmp_path / "specs" / "_project" / "project-init-snapshot.json").exists()


# ---------------------------------------------------------------------------
# A4b: CLI synthesis of missing packs (with best-effort fallback to heuristic stub)
# ---------------------------------------------------------------------------


class _RecordingCli:
    """Fake CliCall closure: records invocations, returns a canned payload or raises."""

    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, work_type: str, system: str, user: str, schema):
        self.calls.append({"work_type": work_type, "system": system, "schema": schema})
        if self.error is not None:
            raise self.error
        return self.response


def _valid_cli_pack(pack_id: str = "data-pipeline") -> dict:
    return {
        "pack_id": pack_id,
        "title": "Data Pipeline",
        "detectors": {
            "rules": [
                {"id": "r", "kind": "file_exists", "path": "notes.txt", "group": "g", "weight": 1.0}
            ]
        },
        "applies_to": {"primary_ecosystem": "data", "can_combine_with": ["core-baseline"]},
        "sdd": {"spec_guidance": "x"},
    }


def _proposed_workdir(tmp_path: Path) -> str:
    # A meaningful file no built-in pack matches → selection status "proposed".
    (tmp_path / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    return str(tmp_path)


# Neutral sentinel: must NOT contain detector keywords (e.g. "ARCHITECTURE"), otherwise a
# built-in pack would score and the selection status would be "selected", not "proposed".
_MAP_CTX = "ZZZSENTINELMAPCTX"


def _run_select(tmp_path: Path, cli) -> tuple[dict, list[str]]:
    workdir = _proposed_workdir(tmp_path)
    created: list[str] = []
    manifest = asyncio.run(
        _select_and_persist_packs(
            workdir=workdir,
            classification=classify_project(workdir),
            codebase_context=_MAP_CTX,
            created_files=created,
            cli_call=cli,
        )
    )
    return manifest, created


def test_packs_cli_synthesis_replaces_stub(tmp_path: Path) -> None:
    cli = _RecordingCli(response={"packs": [_valid_cli_pack()]})
    manifest, created = _run_select(tmp_path, cli)

    # Routed as analytics, with the packs schema and a fully-substituted prompt.
    assert cli.calls and cli.calls[0]["work_type"] == "analytics"
    assert cli.calls[0]["schema"] is PACKS_OUTPUT_SCHEMA
    system = cli.calls[0]["system"]
    assert _MAP_CTX in system
    assert "notes.txt" in system
    assert "core-baseline" in system               # already-selected pack passed in
    assert "{pack_schema_version}" not in system   # placeholder filled
    assert PACK_SCHEMA_VERSION in system

    proposed_ids = [p["pack_id"] for p in manifest["proposed"]]
    assert "data-pipeline" in proposed_ids
    assert not any(pid.startswith("proposed-") for pid in proposed_ids)  # hash-stub replaced
    assert any("data-pipeline" in path for path in created)
    assert (tmp_path / ".cli-proxy" / "sdd" / "packs" / "index.json").exists()


def test_packs_cli_failure_falls_back_to_stub(tmp_path: Path) -> None:
    cli = _RecordingCli(error=RuntimeError("cli down"))
    manifest, created = _run_select(tmp_path, cli)

    assert cli.calls  # CLI was attempted
    proposed_ids = [p["pack_id"] for p in manifest["proposed"]]
    assert any(pid.startswith("proposed-") for pid in proposed_ids)  # heuristic stub retained
    assert (tmp_path / ".cli-proxy" / "sdd" / "packs" / "index.json").exists()


def test_packs_cli_empty_list_falls_back_to_stub(tmp_path: Path) -> None:
    cli = _RecordingCli(response={"packs": []})
    manifest, _created = _run_select(tmp_path, cli)

    proposed_ids = [p["pack_id"] for p in manifest["proposed"]]
    assert any(pid.startswith("proposed-") for pid in proposed_ids)


def test_packs_cli_invalid_pack_falls_back_to_stub(tmp_path: Path) -> None:
    # Unsafe pack_id (whitespace) → PackDefinition.from_dict raises → caught → fallback.
    cli = _RecordingCli(response={"packs": [{"pack_id": "has space", "title": "X"}]})
    manifest, _created = _run_select(tmp_path, cli)

    proposed_ids = [p["pack_id"] for p in manifest["proposed"]]
    assert "has space" not in proposed_ids
    assert any(pid.startswith("proposed-") for pid in proposed_ids)


def test_packs_ambiguous_skips_cli(tmp_path: Path, monkeypatch) -> None:
    import modes.sdd.project_init as pi

    monkeypatch.setattr(
        pi, "select_packs",
        lambda **_kw: PackSelection(
            selected=[], proposed=[], all_scores=[], status="ambiguous", reason="too_close"
        ),
    )
    cli = _RecordingCli(response={"packs": [_valid_cli_pack()]})

    with pytest.raises(RuntimeError, match="pack_selection_ambiguous"):
        asyncio.run(
            _select_and_persist_packs(
                workdir=str(tmp_path),
                classification=classify_project(str(tmp_path)),
                codebase_context="",
                created_files=[],
                cli_call=cli,
            )
        )
    assert cli.calls == []  # ambiguity short-circuits before consulting the CLI


def test_packs_cli_none_uses_heuristic_stub(tmp_path: Path) -> None:
    manifest, _created = _run_select(tmp_path, None)

    proposed_ids = [p["pack_id"] for p in manifest["proposed"]]
    assert any(pid.startswith("proposed-") for pid in proposed_ids)
    assert (tmp_path / ".cli-proxy" / "sdd" / "packs" / "index.json").exists()


def test_project_init_ambiguous_pack_selection_does_not_write_pack_index(tmp_path: Path) -> None:
    (tmp_path / "foo.marker").write_text("x\n", encoding="utf-8")
    project_pack_dir = tmp_path / ".cli-proxy" / "sdd" / "packs" / "project"
    project_pack_dir.mkdir(parents=True)
    for pack_id in ("conflict-a", "conflict-b"):
        (project_pack_dir / f"{pack_id}.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1",
                    "pack_id": pack_id,
                    "title": pack_id,
                    "lifecycle": "project",
                    "version": "1.0",
                    "applies_to": {
                        "primary_ecosystem": pack_id,
                        "can_combine_with": [],
                    },
                    "detectors": {
                        "min_confidence": 0.55,
                        "evidence_groups_required": ["marker"],
                        "rules": [
                            {
                                "id": "marker",
                                "kind": "file_exists",
                                "path": "foo.marker",
                                "group": "marker",
                                "weight": 1.0,
                            }
                        ],
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    mapper = _FakeMapper()
    session = _session(tmp_path)

    with pytest.raises(RuntimeError, match="pack_selection_ambiguous"):
        asyncio.run(
            run_project_initialization(
                session=session,
                runtime_getter=lambda _cap: mapper,
                persist=lambda: None,
            )
        )

    assert not (tmp_path / ".cli-proxy" / "sdd" / "packs" / "index.json").exists()
    assert not (tmp_path / "specs" / "_project" / "pack_manifest.json").exists()
    assert not (tmp_path / "specs" / "_project" / "project-profile.generated.md").exists()
