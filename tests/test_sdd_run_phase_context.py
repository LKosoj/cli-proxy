from __future__ import annotations

import asyncio
import types
from pathlib import Path

from modes.sdd import mode as sdd_mode
from modes.sdd.mode import SddMode


# ---------------------------------------------------------------------------
# Module-level context helpers
# ---------------------------------------------------------------------------

def test_build_feature_terms_slug_and_requirements() -> None:
    spec = "## Requirements\n\n- **REQ-1**: implement authentication caching layer\n"
    terms = sdd_mode._build_feature_terms("user-auth", spec)
    assert "user" in terms          # slug token (kept even though stopword for req text)
    assert "auth" in terms          # slug token
    assert "authentication" in terms
    assert "caching" in terms
    assert "the" not in terms       # stopword filtered from requirement text
    assert "to" not in terms


def test_build_feature_terms_empty_inputs() -> None:
    assert sdd_mode._build_feature_terms("", "") == []


def test_read_project_profile_absent_returns_empty(tmp_path: Path) -> None:
    assert sdd_mode._read_project_profile(str(tmp_path)) == ""


def test_read_project_profile_reads_file(tmp_path: Path) -> None:
    d = tmp_path / "specs" / "_project"
    d.mkdir(parents=True)
    (d / "project-profile.generated.md").write_text("PROFILE BODY", encoding="utf-8")
    assert "PROFILE BODY" in sdd_mode._read_project_profile(str(tmp_path))


def test_load_relevant_nodes_passes_affected_modules(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_select(*, feature_terms, affected_modules=(), graph, map_dir, max_nodes=6):
        captured["affected_modules"] = list(affected_modules)
        captured["feature_terms"] = list(feature_terms)
        return ["node:x"]

    monkeypatch.setattr(sdd_mode, "load_graph", lambda md: {"nodes": [{"id": "node:x"}]})
    monkeypatch.setattr(sdd_mode, "select_relevant_nodes", fake_select)
    monkeypatch.setattr(sdd_mode, "read_node_sources", lambda md, ids: "SRC:" + ",".join(ids))

    out = sdd_mode._load_relevant_nodes(str(tmp_path), ["term"], affected_modules=["modes/x.py"])
    assert captured["affected_modules"] == ["modes/x.py"]
    assert out == "SRC:node:x"


def test_load_relevant_nodes_empty_graph_returns_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sdd_mode, "load_graph", lambda md: {})
    assert sdd_mode._load_relevant_nodes(str(tmp_path), ["t"]) == ""


def test_load_relevant_nodes_never_raises(monkeypatch, tmp_path: Path) -> None:
    def boom(md):
        raise RuntimeError("graph corrupt")

    monkeypatch.setattr(sdd_mode, "load_graph", boom)
    assert sdd_mode._load_relevant_nodes(str(tmp_path), ["t"]) == ""


# ---------------------------------------------------------------------------
# _ensure_map_freshness
# ---------------------------------------------------------------------------

class _FakeMapperRun:
    def __init__(self) -> None:
        self.calls = []

    async def maybe_run(self, *, session, workdir, usage, operation, sync_agents):
        self.calls.append({"operation": operation, "sync_agents": sync_agents, "usage": usage})
        return {"status": "ready"}


class _FakeMapperStatus:
    def __init__(self, status) -> None:
        self._status = status

    def get_status(self, *, workdir):
        return self._status


def _getter(status_obj, run_obj):
    def _g(cap):
        if cap == "codebase_mapper_status":
            return status_obj
        if cap == "codebase_mapper_run":
            return run_obj
        return None
    return _g


def _bot_app():
    return types.SimpleNamespace(config=types.SimpleNamespace(defaults=types.SimpleNamespace()))


def _run_freshness(mode, status_dict, run):
    mode._optional_runtime_getter = lambda: _getter(_FakeMapperStatus(status_dict), run)
    asyncio.run(mode._ensure_map_freshness(types.SimpleNamespace(), _bot_app(), "/repo"))


def test_ensure_freshness_no_runtime_is_noop() -> None:
    mode = SddMode()
    mode._optional_runtime_getter = lambda: None
    # Must not raise even though there is no mapper runtime at all.
    asyncio.run(mode._ensure_map_freshness(types.SimpleNamespace(), _bot_app(), "/repo"))


def test_ensure_freshness_init_when_no_map_and_code(monkeypatch) -> None:
    mode = SddMode()
    run = _FakeMapperRun()
    monkeypatch.setattr(
        sdd_mode, "classify_project",
        lambda wd: types.SimpleNamespace(is_existing_codebase=True),
    )
    _run_freshness(mode, {"graph_initialized": False, "head_commit": ""}, run)
    assert run.calls and run.calls[0]["operation"] == "init"


def test_ensure_freshness_greenfield_skips(monkeypatch) -> None:
    mode = SddMode()
    run = _FakeMapperRun()
    monkeypatch.setattr(
        sdd_mode, "classify_project",
        lambda wd: types.SimpleNamespace(is_existing_codebase=False),
    )
    _run_freshness(mode, {"graph_initialized": False, "head_commit": ""}, run)
    assert run.calls == []


def test_ensure_freshness_verify_on_head_drift(monkeypatch) -> None:
    mode = SddMode()
    run = _FakeMapperRun()
    monkeypatch.setattr(
        sdd_mode, "_git",
        lambda args, wd: "newhead" if args[0] == "rev-parse" else "",
    )
    _run_freshness(mode, {"graph_initialized": True, "head_commit": "oldhead"}, run)
    assert run.calls and run.calls[0]["operation"] == "verify"


def test_ensure_freshness_skip_when_fresh(monkeypatch) -> None:
    mode = SddMode()
    run = _FakeMapperRun()
    # HEAD matches map and working tree is clean (status --short empty) → no work.
    monkeypatch.setattr(
        sdd_mode, "_git",
        lambda args, wd: "samehead" if args[0] == "rev-parse" else "",
    )
    _run_freshness(mode, {"graph_initialized": True, "head_commit": "samehead"}, run)
    assert run.calls == []


def test_ensure_freshness_skip_when_not_git(monkeypatch) -> None:
    mode = SddMode()
    run = _FakeMapperRun()
    monkeypatch.setattr(sdd_mode, "_git", lambda args, wd: "")  # no git → can't compare
    _run_freshness(mode, {"graph_initialized": True, "head_commit": "x"}, run)
    assert run.calls == []
