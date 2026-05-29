from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import asyncio
import types

from modes.codebase_mapper import runtime as mapper_runtime_module
from modes.codebase_mapper.runtime import CodebaseMapperRuntime


def _git_ok() -> bool:
    return bool(shutil.which("git"))


def _git(cmd: list[str], *, cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _init_repo(tmp_path) -> str:
    wd = str(tmp_path)
    _git(["git", "init"], cwd=wd)
    _git(["git", "config", "user.name", "Test"], cwd=wd)
    _git(["git", "config", "user.email", "test@example.com"], cwd=wd)
    with open(os.path.join(wd, "README.md"), "w", encoding="utf-8") as f:
        f.write("# test\n")
    with open(os.path.join(wd, "app.py"), "w", encoding="utf-8") as f:
        f.write("print('ok')\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "init"], cwd=wd)
    return wd


def test_codebase_map_disabled(tmp_path):
    service = CodebaseMapperRuntime()
    result = service.run(workdir=str(tmp_path), usage="disabled", force=False)
    assert result.status == "disabled"


def test_codebase_map_manifest_fallback_without_git(tmp_path):
    wd = str(tmp_path)
    with open(os.path.join(wd, "app.py"), "w", encoding="utf-8") as f:
        f.write("print('ok')\n")
    service = CodebaseMapperRuntime()

    first = service.run(workdir=wd, usage="enabled", force=False)
    assert first.status in {"full_updated", "partial_updated"}
    assert first.reason == "bootstrap"

    second = service.run(workdir=wd, usage="enabled", force=False)
    assert second.status == "skipped"
    assert second.reason == "manifest_diff_empty"

    with open(os.path.join(wd, "app.py"), "a", encoding="utf-8") as f:
        f.write("print('changed')\n")
    third = service.run(workdir=wd, usage="enabled", force=False)
    assert third.status in {"partial_updated", "full_updated"}
    assert third.reason == "no_git_incremental"
    assert "app.py" in list(third.changed_files or [])


def test_codebase_map_bootstrap_and_skip(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    first = service.run(workdir=wd, usage="enabled", force=False)
    assert first.status in {"full_updated", "partial_updated"}
    assert first.map_dir
    assert os.path.exists(os.path.join(first.map_dir, "meta.json"))
    assert os.path.exists(os.path.join(first.map_dir, "STACK.md"))

    second = service.run(workdir=wd, usage="enabled", force=False)
    assert second.status == "skipped"


def test_codebase_map_skip_does_not_rewrite_graph_files(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    first = service.run(workdir=wd, usage="enabled", force=False)
    assert first.status in {"full_updated", "partial_updated"}
    _git(["git", "add", ".cli-proxy/.codebase_map"], cwd=wd)
    _git(["git", "commit", "-m", "commit map"], cwd=wd)

    index_path = os.path.join(wd, ".cli-proxy/.codebase_map", "INDEX.md")
    state_path = service.graph_state_path(workdir=wd)
    before_index_mtime = os.path.getmtime(index_path)
    before_state_mtime = os.path.getmtime(state_path)

    second = service.run(workdir=wd, usage="enabled", force=False)
    assert second.status == "skipped"
    assert os.path.getmtime(index_path) == before_index_mtime
    assert os.path.getmtime(state_path) == before_state_mtime


def test_codebase_map_skip_restores_graph_when_key_files_missing(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    first = service.run(workdir=wd, usage="enabled", force=False)
    assert first.status in {"full_updated", "partial_updated"}
    _git(["git", "add", ".cli-proxy/.codebase_map"], cwd=wd)
    _git(["git", "commit", "-m", "commit map"], cwd=wd)

    index_path = os.path.join(wd, ".cli-proxy/.codebase_map", "INDEX.md")
    os.remove(index_path)
    assert not os.path.exists(index_path)

    second = service.run(workdir=wd, usage="enabled", force=False)
    assert second.status == "skipped"
    assert os.path.exists(index_path)


def test_codebase_map_git_diff_ignores_codebase_map_artifacts(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    first = service.run(workdir=wd, usage="enabled", force=False)
    assert first.status in {"full_updated", "partial_updated"}
    _git(["git", "add", ".cli-proxy/.codebase_map"], cwd=wd)
    _git(["git", "commit", "-m", "commit map"], cwd=wd)

    stack_path = os.path.join(wd, ".cli-proxy/.codebase_map", "STACK.md")
    with open(stack_path, "a", encoding="utf-8") as f:
        f.write("\nmap-only-change\n")
    _git(["git", "add", ".cli-proxy/.codebase_map/STACK.md"], cwd=wd)
    _git(["git", "commit", "-m", "map only change"], cwd=wd)

    second = service.run(workdir=wd, usage="enabled", force=False)
    assert second.status == "skipped"
    assert second.reason == "git_diff_empty"
    assert list(second.updated_docs or []) == []


def test_codebase_map_updates_on_git_diff(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    _ = service.run(workdir=wd, usage="enabled", force=False)
    with open(os.path.join(wd, "app.py"), "a", encoding="utf-8") as f:
        f.write("# TODO: update\n")
    _git(["git", "add", "app.py"], cwd=wd)
    _git(["git", "commit", "-m", "change"], cwd=wd)

    updated = service.run(workdir=wd, usage="enabled", force=False)
    assert updated.status in {"partial_updated", "full_updated"}
    assert len(updated.changed_files or []) >= 1
    assert len(updated.updated_docs or []) >= 1


def test_codebase_map_incremental_run_skips_heavy_graph_sync(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    first = service.run(workdir=wd, usage="enabled", force=False)
    assert first.status in {"full_updated", "partial_updated"}
    _git(["git", "add", ".cli-proxy/.codebase_map"], cwd=wd)
    _git(["git", "commit", "-m", "commit map"], cwd=wd)

    graph_path = os.path.join(wd, ".cli-proxy/.codebase_map", "graph.json")
    state_path = service.graph_state_path(workdir=wd)
    graph_before = os.path.getmtime(graph_path)
    state_before = os.path.getmtime(state_path)

    with open(os.path.join(wd, "app.py"), "a", encoding="utf-8") as f:
        f.write("print('change')\n")
    _git(["git", "add", "app.py"], cwd=wd)
    _git(["git", "commit", "-m", "change app"], cwd=wd)

    updated = service.run(workdir=wd, usage="enabled", force=False)
    assert updated.status in {"partial_updated", "full_updated"}
    assert updated.reason == "incremental"
    assert os.path.getmtime(graph_path) == graph_before
    assert os.path.getmtime(state_path) == state_before


def test_codebase_map_selective_snapshot_skips_todo_scan_for_testing_docs(tmp_path, monkeypatch):
    wd = str(tmp_path)
    os.makedirs(os.path.join(wd, "tests"), exist_ok=True)
    with open(os.path.join(wd, "tests", "t.py"), "w", encoding="utf-8") as f:
        f.write("def test_ok():\n    assert True\n")
    service = CodebaseMapperRuntime()

    calls = {"todo": 0}

    def _fake_rg_count(_workdir: str, _pattern: str) -> int:
        calls["todo"] += 1
        return 0

    monkeypatch.setattr(service, "_rg_count", _fake_rg_count)
    snap = service._scan_workspace_for_docs(wd, {"TESTING.md"})
    assert calls["todo"] == 0
    assert isinstance(snap.get("files"), list)


def test_codebase_map_runtime_context(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=False)

    context = service.build_runtime_context(workdir=wd, max_chars=2500)
    assert context
    assert "Codebase map" in context
    assert "[ARCHITECTURE.md]" in context or "[STACK.md]" in context


def test_codebase_map_excludes_markdown_and_license_from_generated_map(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "docs"), exist_ok=True)
    with open(os.path.join(wd, "docs", "guide.md"), "w", encoding="utf-8") as f:
        f.write("# guide\n")
    with open(os.path.join(wd, "LICENSE"), "w", encoding="utf-8") as f:
        f.write("MIT\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add docs and license"], cwd=wd)

    service = CodebaseMapperRuntime()
    result = service.run(workdir=wd, usage="enabled", force=False)
    assert result.status in {"full_updated", "partial_updated"}

    structure_path = os.path.join(wd, ".cli-proxy/.codebase_map", "STRUCTURE.md")
    with open(structure_path, "r", encoding="utf-8") as f:
        structure = f.read()
    assert "`docs/guide.md`" not in structure
    assert "`LICENSE`" not in structure


def test_codebase_map_excludes_pycache_and_pyc_from_generated_map(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "__pycache__"), exist_ok=True)
    os.makedirs(os.path.join(wd, "pkg", "__pycache__"), exist_ok=True)
    with open(os.path.join(wd, "__pycache__", "root.cpython-312.pyc"), "w", encoding="utf-8") as f:
        f.write("binary-placeholder\n")
    with open(os.path.join(wd, "pkg", "__pycache__", "mod.cpython-312.pyc"), "w", encoding="utf-8") as f:
        f.write("binary-placeholder\n")
    with open(os.path.join(wd, "module.pyc"), "w", encoding="utf-8") as f:
        f.write("binary-placeholder\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add pycache"], cwd=wd)

    service = CodebaseMapperRuntime()
    result = service.run(workdir=wd, usage="enabled", force=False)
    assert result.status in {"full_updated", "partial_updated"}

    structure_path = os.path.join(wd, ".cli-proxy/.codebase_map", "STRUCTURE.md")
    with open(structure_path, "r", encoding="utf-8") as f:
        structure = f.read()
    assert "__pycache__" not in structure
    assert "`module.pyc`" not in structure

    architecture_path = os.path.join(wd, ".cli-proxy/.codebase_map", "ARCHITECTURE.md")
    with open(architecture_path, "r", encoding="utf-8") as f:
        architecture = f.read()
    assert "`__pycache__`" not in architecture


def test_codebase_map_parallel_cli_runner(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    async def _runner(task: dict) -> dict:
        map_dir = str(task.get("map_dir") or "")
        docs = list(task.get("target_docs") or [])
        os.makedirs(map_dir, exist_ok=True)
        for name in docs:
            with open(os.path.join(map_dir, name), "w", encoding="utf-8") as f:
                f.write(f"# {name}\n\nGenerated by test runner\n")
        return {"success": True, "focus": task.get("focus"), "docs": docs}

    result = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            force=False,
            cli_runner=_runner,
        )
    )
    assert result.get("status") == "full_updated"
    assert os.path.exists(os.path.join(wd, ".cli-proxy/.codebase_map", "STACK.md"))
    assert os.path.exists(os.path.join(wd, ".cli-proxy/.codebase_map", "meta.json"))


def test_codebase_map_parallel_cli_runner_limited_to_two(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    seen_running = {"current": 0, "max": 0}
    lock = asyncio.Lock()

    async def _runner(task: dict) -> dict:
        map_dir = str(task.get("map_dir") or "")
        docs = list(task.get("target_docs") or [])
        os.makedirs(map_dir, exist_ok=True)
        async with lock:
            seen_running["current"] += 1
            if seen_running["current"] > seen_running["max"]:
                seen_running["max"] = seen_running["current"]
        await asyncio.sleep(0.03)
        for name in docs:
            with open(os.path.join(map_dir, name), "w", encoding="utf-8") as f:
                f.write(f"# {name}\n\nGenerated by test runner\n")
        async with lock:
            seen_running["current"] -= 1
        return {"success": True, "focus": task.get("focus"), "docs": docs}

    result = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            force=False,
            cli_runner=_runner,
        )
    )
    assert result.get("status") == "full_updated"
    assert seen_running["max"] <= 2


def test_codebase_map_run_profile_disables_node_enrich(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    seen_focuses: list[str] = []

    async def _runner(task: dict) -> dict:
        seen_focuses.append(str(task.get("focus") or ""))
        map_dir = str(task.get("map_dir") or "")
        docs = list(task.get("target_docs") or [])
        os.makedirs(map_dir, exist_ok=True)
        for name in docs:
            if str(name).startswith("nodes/"):
                continue
            with open(os.path.join(map_dir, name), "w", encoding="utf-8") as f:
                f.write(f"# {name}\n\nGenerated by test runner\n")
        return {"success": True}

    payload = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            force=False,
            operation="run",
            cli_runner=_runner,
        )
    )
    assert str(payload.get("status") or "").strip() in {"full_updated", "partial_updated"}
    assert "node_enrich" not in seen_focuses
    assert "repair" not in seen_focuses


def test_codebase_map_verify_profile_runs_node_enrich(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=False)

    seen_focuses: list[str] = []

    async def _runner(task: dict) -> dict:
        seen_focuses.append(str(task.get("focus") or ""))
        map_dir = str(task.get("map_dir") or "")
        docs = list(task.get("target_docs") or [])
        os.makedirs(map_dir, exist_ok=True)
        for name in docs:
            with open(os.path.join(map_dir, name), "w", encoding="utf-8") as f:
                f.write(f"# {name}\n\nGenerated by test runner\n")
        return {"success": True}

    payload = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            force=False,
            operation="verify",
            cli_runner=_runner,
        )
    )
    assert str(payload.get("status") or "").strip() in {"graph_verified", "full_updated", "partial_updated"}
    assert "node_enrich" in seen_focuses


def test_codebase_map_list_files_fallback_uses_parallelism_limit(tmp_path, monkeypatch):
    wd = str(tmp_path)
    os.makedirs(os.path.join(wd, "a"), exist_ok=True)
    os.makedirs(os.path.join(wd, "b"), exist_ok=True)
    with open(os.path.join(wd, "a", "file_a.py"), "w", encoding="utf-8") as f:
        f.write("print('a')\n")
    with open(os.path.join(wd, "b", "file_b.ts"), "w", encoding="utf-8") as f:
        f.write("export const b = 1\n")

    service = CodebaseMapperRuntime()

    def _fake_run_cmd(_args, *, cwd):
        _ = cwd
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    seen = {"workers": None}

    class _RecordingExecutor:
        def __init__(self, max_workers=None):
            seen["workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, *args, **kwargs):
            fut = concurrent.futures.Future()
            try:
                fut.set_result(fn(*args, **kwargs))
            except Exception as e:
                fut.set_exception(e)
            return fut

    monkeypatch.setattr(service, "_run_cmd", _fake_run_cmd)
    monkeypatch.setattr(mapper_runtime_module.concurrent.futures, "ThreadPoolExecutor", _RecordingExecutor)

    files = service._list_files(wd)
    assert seen["workers"] == mapper_runtime_module._CLI_PARALLELISM_LIMIT
    assert "a/file_a.py" in files
    assert "b/file_b.ts" in files


def test_codebase_map_api_spec_includes_line_numbers(tmp_path):
    wd = str(tmp_path)
    source_path = os.path.join(wd, "sample.py")
    with open(source_path, "w", encoding="utf-8") as f:
        f.write(
            "class A:\n"
            "    def m(self):\n"
            "        return 1\n"
            "\n"
            "def foo(x):\n"
            "    return x\n"
        )

    service = CodebaseMapperRuntime()
    map_dir = os.path.join(wd, ".cli-proxy/.codebase_map")
    rel = service._generate_api_spec(wd, map_dir, "sample.py")
    assert rel is not None

    api_path = os.path.join(map_dir, rel)
    content = open(api_path, "r", encoding="utf-8").read()
    assert "class A" in content
    assert "line 1" in content
    assert "def m()" in content
    assert "line 2" in content
    assert "def foo(x)" in content
    assert "line 5" in content


def test_codebase_map_cleans_up_stale_api_specs(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=True)

    stale_path = os.path.join(wd, ".cli-proxy/.codebase_map", "api", "legacy", "old-py.md")
    os.makedirs(os.path.dirname(stale_path), exist_ok=True)
    with open(stale_path, "w", encoding="utf-8") as f:
        f.write("# stale\n")
    assert os.path.exists(stale_path)

    _ = service.run(workdir=wd, usage="enabled", force=True)
    assert not os.path.exists(stale_path)


def test_codebase_map_strips_review_section_from_concerns(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    async def _runner(task: dict) -> dict:
        map_dir = str(task.get("map_dir") or "")
        docs = list(task.get("target_docs") or [])
        os.makedirs(map_dir, exist_ok=True)
        for name in docs:
            path = os.path.join(map_dir, name)
            if name == "CONCERNS.md":
                content = (
                    "# CONCERNS\n\n"
                    "## Potential concerns\n"
                    "- stable concern\n\n"
                    "## Needs review\n"
                    "- nodes/a.md\n"
                    "- nodes/b.md\n\n"
                    "## Other\n"
                    "- keep me\n"
                )
            else:
                content = f"# {name}\n\nGenerated by test runner\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return {"success": True, "focus": task.get("focus"), "docs": docs}

    _ = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            force=False,
            cli_runner=_runner,
        )
    )
    concerns_path = os.path.join(wd, ".cli-proxy/.codebase_map", "CONCERNS.md")
    assert os.path.exists(concerns_path)
    concerns = open(concerns_path, "r", encoding="utf-8").read()
    assert "Needs review" not in concerns
    assert "nodes/a.md" not in concerns
    assert "## Other" in concerns


def test_codebase_map_builds_instruction_graph_and_status_tree(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    result = service.run(workdir=wd, usage="enabled", force=False)
    assert result.status in {"full_updated", "partial_updated"}
    assert os.path.exists(os.path.join(wd, ".cli-proxy/.codebase_map", "INDEX.md"))
    assert os.path.exists(os.path.join(wd, ".cli-proxy/.codebase_map", "nodes"))
    assert os.path.exists(os.path.join(wd, ".cli-proxy/.codebase_map", "graph.json"))
    assert os.path.exists(os.path.join(wd, ".cli-proxy/.codebase_map", "rules.yaml"))
    assert os.path.exists(os.path.join(wd, ".cli-proxy/.codebase_map", "state.json"))

    status = service.get_status(workdir=wd)
    assert bool(status.get("graph_initialized")) is True
    assert str(status.get("graph_state") or "").strip() in {"ready", "needs_review"}
    assert int(status.get("graph_nodes") or 0) >= 1
    tree = list(status.get("graph_tree") or [])
    assert tree
    assert any("INDEX.md" in line for line in tree)
    index_path = os.path.join(wd, ".cli-proxy/.codebase_map", "INDEX.md")
    index_content = open(index_path, "r", encoding="utf-8").read()
    assert "## Mandatory Workflow" in index_content
    assert "Before any edits, read this `INDEX.md` completely." in index_content
    assert "## Runtime Verification and Fallback Policy (Hardcoded)" in index_content
    assert "Policy matrix по fallback" in index_content
    assert "Legacy-потоки" in index_content
    assert "Новый функционал и новые mode-сценарии" in index_content
    assert "Opt-in fallback" in index_content
    assert "`state.json`" in index_content
    assert "## Core Docs" in index_content
    assert "`STACK.md`" in index_content
    assert "`ARCHITECTURE.md`" in index_content
    assert "`CONCERNS.md`" in index_content


def test_codebase_map_generates_file_globs_without_trailing_wildcard(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()

    result = service.run(workdir=wd, usage="enabled", force=False)
    assert result.status in {"full_updated", "partial_updated"}

    index_path = os.path.join(wd, ".cli-proxy/.codebase_map", "INDEX.md")
    index_content = open(index_path, "r", encoding="utf-8").read()
    assert "source_glob: `app.py`" in index_content
    assert "source_glob: `app.py/**`" not in index_content

    rules_path = os.path.join(wd, ".cli-proxy/.codebase_map", "rules.yaml")
    rules_content = open(rules_path, "r", encoding="utf-8").read()
    assert '        - "app.py"' in rules_content
    assert '        - "app.py/**"' not in rules_content


def test_codebase_map_init_syncs_agents_md(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    with open(os.path.join(wd, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("# AGENTS\n\nexisting rules\n")
    service = CodebaseMapperRuntime()

    async def _run_once() -> dict:
        return await service.maybe_run(workdir=wd, usage="enabled", force=False, operation="init", sync_agents=True)

    result = asyncio.run(_run_once())
    assert str(result.get("status") or "").strip() in {"full_updated", "partial_updated"}
    assert str(result.get("graph_state") or "").strip() == "ready"
    with open(os.path.join(wd, "AGENTS.md"), "r", encoding="utf-8") as f:
        content = f.read()
    assert "CODEBASE_MAPPER_GRAPH:START" in content
    assert ".cli-proxy/.codebase_map/INDEX.md" in content


def test_codebase_map_infers_declared_rule_from_any_markdown(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    with open(os.path.join(wd, "bot.py"), "w", encoding="utf-8") as f:
        f.write("print('bot')\n")
    os.makedirs(os.path.join(wd, "desktop"), exist_ok=True)
    with open(os.path.join(wd, "desktop", "main.py"), "w", encoding="utf-8") as f:
        f.write("print('desktop')\n")
    os.makedirs(os.path.join(wd, "docs"), exist_ok=True)
    with open(os.path.join(wd, "docs", "policy.md"), "w", encoding="utf-8") as f:
        f.write("Если меняешь bot.py, синхронизируй изменения в desktop.\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add policy"], cwd=wd)

    service = CodebaseMapperRuntime()
    result = service.run(workdir=wd, usage="enabled", force=False)
    assert result.status in {"full_updated", "partial_updated"}

    state = service.read_graph_state(workdir=wd)
    inferred = list(state.get("inferred_rules") or [])
    assert inferred
    declared = [r for r in inferred if str((r or {}).get("source_class") or "") == "declared"]
    assert declared
    assert any(str((r or {}).get("status") or "") == "active" for r in declared)

    status = service.get_status(workdir=wd)
    assert int(status.get("inferred_rules_total") or 0) >= 1
    assert int(status.get("inferred_rules_active") or 0) >= 1


def test_codebase_map_observed_rule_can_be_confirmed(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "alpha"), exist_ok=True)
    os.makedirs(os.path.join(wd, "beta"), exist_ok=True)
    with open(os.path.join(wd, "alpha", "a.py"), "w", encoding="utf-8") as f:
        f.write("print('a1')\n")
    with open(os.path.join(wd, "beta", "b.py"), "w", encoding="utf-8") as f:
        f.write("print('b1')\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add areas"], cwd=wd)
    with open(os.path.join(wd, "alpha", "a.py"), "a", encoding="utf-8") as f:
        f.write("print('a2')\n")
    with open(os.path.join(wd, "beta", "b.py"), "a", encoding="utf-8") as f:
        f.write("print('b2')\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "cochange 1"], cwd=wd)
    with open(os.path.join(wd, "alpha", "a.py"), "a", encoding="utf-8") as f:
        f.write("print('a3')\n")
    with open(os.path.join(wd, "beta", "b.py"), "a", encoding="utf-8") as f:
        f.write("print('b3')\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "cochange 2"], cwd=wd)

    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=False)

    state = service.read_graph_state(workdir=wd)
    observed_rules = [
        r for r in list(state.get("inferred_rules") or [])
        if str((r or {}).get("source_class") or "") == "observed"
    ]
    assert observed_rules
    target = observed_rules[0]
    assert str(target.get("status") or "") == "proposed"
    rule_id = str(target.get("id") or "")
    assert rule_id

    confirmed = service.confirm_review_item(workdir=wd, item=rule_id)
    assert bool(confirmed.get("ok")) is True

    state_after = service.read_graph_state(workdir=wd)
    active = [
        r for r in list(state_after.get("inferred_rules") or [])
        if str((r or {}).get("id") or "") == rule_id
    ]
    assert active
    assert str(active[0].get("status") or "") == "active"


def test_codebase_map_validate_marks_invalid_nodes(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=False)

    state = service.read_graph_state(workdir=wd)
    review_items = list(state.get("review_items") or [])
    assert review_items
    target = str(review_items[0])
    target_abs = os.path.join(wd, ".cli-proxy/.codebase_map", target)
    with open(target_abs, "w", encoding="utf-8") as f:
        f.write("# broken node\n\nno required sections\n")

    payload = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            operation="validate",
        )
    )
    assert str(payload.get("status") or "") == "validation_done"
    validate_queue = list(payload.get("validate_queue") or [])
    assert target in validate_queue
    status = service.get_status(workdir=wd)
    assert int((status.get("nodes_status_counts") or {}).get("needs_repair") or 0) >= 1


def test_codebase_map_nodes_include_related_by_imports(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "alpha"), exist_ok=True)
    os.makedirs(os.path.join(wd, "beta"), exist_ok=True)
    with open(os.path.join(wd, "beta", "service.py"), "w", encoding="utf-8") as f:
        f.write("def ping():\n    return 'ok'\n")
    with open(os.path.join(wd, "alpha", "worker.py"), "w", encoding="utf-8") as f:
        f.write("import beta.service\n\n\ndef run():\n    return beta.service.ping()\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add alpha beta deps"], cwd=wd)

    service = CodebaseMapperRuntime()
    result = service.run(workdir=wd, usage="enabled", force=True)
    assert result.status in {"full_updated", "partial_updated"}

    alpha_node = os.path.join(wd, ".cli-proxy/.codebase_map", "nodes", "alpha.md")
    assert os.path.exists(alpha_node)
    content = open(alpha_node, "r", encoding="utf-8").read()
    assert "## Related nodes" in content
    assert "`nodes/beta.md`" in content
    assert "beta/**" in content
    assert "`alpha/worker.py`" in content


def test_codebase_map_repair_does_not_write_concerns(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=False)

    concerns_path = os.path.join(wd, ".cli-proxy/.codebase_map", "CONCERNS.md")
    baseline = open(concerns_path, "r", encoding="utf-8").read()

    state = service.read_graph_state(workdir=wd)
    target = str((state.get("review_items") or [])[0])
    with open(os.path.join(wd, ".cli-proxy/.codebase_map", target), "w", encoding="utf-8") as f:
        f.write("# broken\n")

    _ = asyncio.run(service.maybe_run(workdir=wd, usage="enabled", operation="validate"))

    async def _fail_runner(_task: dict) -> dict:
        return {"success": False, "error": "forced_failure"}

    _ = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            operation="repair",
            cli_runner=_fail_runner,
        )
    )
    _ = asyncio.run(
        service.maybe_run(
            workdir=wd,
            usage="enabled",
            operation="repair",
            cli_runner=_fail_runner,
        )
    )

    after = open(concerns_path, "r", encoding="utf-8").read()
    assert after == baseline


def test_codebase_map_regex_relations_for_non_python(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "api"), exist_ok=True)
    os.makedirs(os.path.join(wd, "shared"), exist_ok=True)
    with open(os.path.join(wd, "shared", "util.ts"), "w", encoding="utf-8") as f:
        f.write("export const x = 1;\n")
    with open(os.path.join(wd, "api", "index.ts"), "w", encoding="utf-8") as f:
        f.write("import { x } from 'shared/util';\nconsole.log(x)\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add ts relation"], cwd=wd)

    service = CodebaseMapperRuntime()
    result = service.run(workdir=wd, usage="enabled", force=True)
    assert result.status in {"full_updated", "partial_updated"}

    api_node = os.path.join(wd, ".cli-proxy/.codebase_map", "nodes", "api.md")
    content = open(api_node, "r", encoding="utf-8").read()
    assert "`nodes/shared.md`" in content
    assert "via L1" in content or "via L0/L1" in content

    state = service.read_graph_state(workdir=wd)
    rel_graph = dict(state.get("relation_graph") or {})
    api_rel = list(rel_graph.get("api") or [])
    assert any(str((x or {}).get("target") or "") == "shared" for x in api_rel)
    shared_rel = [x for x in api_rel if str((x or {}).get("target") or "") == "shared"]
    assert shared_rel
    levels = list(shared_rel[0].get("levels") or [])
    assert "L2" in levels or "L1" in levels or "L0" in levels


def test_codebase_map_php_relations_detected(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "billing"), exist_ok=True)
    os.makedirs(os.path.join(wd, "shared"), exist_ok=True)
    with open(os.path.join(wd, "shared", "bootstrap.php"), "w", encoding="utf-8") as f:
        f.write("<?php\nfunction boot() {}\n")
    with open(os.path.join(wd, "billing", "service.php"), "w", encoding="utf-8") as f:
        f.write("<?php\nrequire_once 'shared/bootstrap.php';\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add php relation"], cwd=wd)

    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=True)

    billing_node = os.path.join(wd, ".cli-proxy/.codebase_map", "nodes", "billing.md")
    content = open(billing_node, "r", encoding="utf-8").read()
    assert "`nodes/shared.md`" in content

    state = service.read_graph_state(workdir=wd)
    rel_graph = dict(state.get("relation_graph") or {})
    billing_rel = [x for x in list(rel_graph.get("billing") or []) if str((x or {}).get("target") or "") == "shared"]
    assert billing_rel
    levels = list(billing_rel[0].get("levels") or [])
    assert "L2" in levels or "L1" in levels


def test_codebase_map_go_relations_detected(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "api"), exist_ok=True)
    os.makedirs(os.path.join(wd, "shared"), exist_ok=True)
    with open(os.path.join(wd, "shared", "util.go"), "w", encoding="utf-8") as f:
        f.write("package shared\nfunc X() {}\n")
    with open(os.path.join(wd, "api", "main.go"), "w", encoding="utf-8") as f:
        f.write("package api\nimport \"shared/util\"\nfunc Run() {}\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add go relation"], cwd=wd)

    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=True)

    state = service.read_graph_state(workdir=wd)
    rel_graph = dict(state.get("relation_graph") or {})
    api_rel = [x for x in list(rel_graph.get("api") or []) if str((x or {}).get("target") or "") == "shared"]
    assert api_rel
    levels = list(api_rel[0].get("levels") or [])
    assert "L2" in levels or "L1" in levels


def test_codebase_map_rust_relations_detected(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    os.makedirs(os.path.join(wd, "core"), exist_ok=True)
    os.makedirs(os.path.join(wd, "shared"), exist_ok=True)
    with open(os.path.join(wd, "shared", "lib.rs"), "w", encoding="utf-8") as f:
        f.write("pub fn x() {}\n")
    with open(os.path.join(wd, "core", "lib.rs"), "w", encoding="utf-8") as f:
        f.write("use shared::lib;\npub fn run() { let _ = lib::x; }\n")
    _git(["git", "add", "."], cwd=wd)
    _git(["git", "commit", "-m", "add rust relation"], cwd=wd)

    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=True)

    state = service.read_graph_state(workdir=wd)
    rel_graph = dict(state.get("relation_graph") or {})
    core_rel = [x for x in list(rel_graph.get("core") or []) if str((x or {}).get("target") or "") == "shared"]
    assert core_rel
    levels = list(core_rel[0].get("levels") or [])
    assert "L2" in levels or "L1" in levels


def test_codebase_map_source_samples_are_adaptive_and_prioritize_changed():
    service = CodebaseMapperRuntime()
    domain = "app"
    domain_files = [f"app/core/m{idx}.py" for idx in range(30)]
    domain_files.extend(
        [
            "app/http/router.ts",
            "app/http/client.ts",
            "app/jobs/worker.py",
            "app/cli/main.py",
            "app/README",
        ]
    )
    changed = ["app/http/router.ts", "app/jobs/worker.py", "README.md"]

    run_samples = service._select_domain_source_samples(
        domain=domain,
        domain_files=domain_files,
        changed_files=changed,
        operation="run",
    )
    verify_samples = service._select_domain_source_samples(
        domain=domain,
        domain_files=domain_files,
        changed_files=changed,
        operation="verify",
    )

    assert "app/http/router.ts" in run_samples
    assert "app/jobs/worker.py" in run_samples
    assert 12 <= len(run_samples) <= 24
    assert len(verify_samples) >= len(run_samples)
    assert len(verify_samples) <= 40


def test_codebase_map_repair_prompt_targets_dot_codebase_map_nodes(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=False)

    state = service.read_graph_state(workdir=wd)
    review_items = [str(x) for x in list(state.get("review_items") or []) if str(x).strip()]
    assert review_items
    target = review_items[0]

    state["repair_queue"] = [target]
    state["nodes_status"] = {target: {"status": "needs_repair", "repair_attempts": 0}}
    service.write_graph_state(workdir=wd, state=state)

    captured: dict[str, str] = {}

    async def _fake_cli_runner(task: dict) -> dict:
        captured["prompt"] = str(task.get("prompt") or "")
        return {"success": False, "error": "skip_real_repair"}

    _ = asyncio.run(
        service._run_repair_queue(
            root=wd,
            map_dir=os.path.join(wd, ".cli-proxy/.codebase_map"),
            cli_runner=_fake_cli_runner,
            max_items=1,
        )
    )
    prompt = captured.get("prompt", "")
    assert "Map root:" in prompt
    assert f"`.cli-proxy/.codebase_map/{target}`" in prompt


def test_codebase_map_enrich_prompt_targets_dot_codebase_map_nodes(tmp_path):
    if not _git_ok():
        return
    wd = _init_repo(tmp_path)
    service = CodebaseMapperRuntime()
    _ = service.run(workdir=wd, usage="enabled", force=False)

    state = service.read_graph_state(workdir=wd)
    review_items = [str(x) for x in list(state.get("review_items") or []) if str(x).strip()]
    assert review_items
    target = review_items[0]

    captured: dict[str, str] = {}

    async def _fake_cli_runner(task: dict) -> dict:
        captured["prompt"] = str(task.get("prompt") or "")
        return {"success": True}

    asyncio.run(
        service._enrich_graph_nodes_with_cli(
            root=wd,
            map_dir=os.path.join(wd, ".cli-proxy/.codebase_map"),
            node_paths=[target],
            changed_files=[],
            cli_runner=_fake_cli_runner,
            max_items=1,
        )
    )
    prompt = captured.get("prompt", "")
    assert "Map root:" in prompt
    assert f"`.cli-proxy/.codebase_map/{target}`" in prompt
