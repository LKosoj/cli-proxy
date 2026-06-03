from __future__ import annotations

from pathlib import Path

from modes.sdd.node_selector import (
    load_graph,
    read_node_sources,
    select_relevant_nodes,
)

GRAPH = {
    "nodes": [
        {"id": "node:modes", "title": "modes", "path": "nodes/modes.md", "source_glob": "modes/**"},
        {"id": "node:sessions", "title": "sessions", "path": "nodes/sessions.md", "source_glob": "sessions/**"},
        {"id": "node:session-py", "title": "session.py", "path": "nodes/session-py.md", "source_glob": "session.py"},
        {"id": "node:agent", "title": "agent", "path": "nodes/agent.md", "source_glob": "agent/**"},
        {"id": "node:tg", "title": "tg", "path": "nodes/tg.md", "source_glob": "tg/**"},
    ],
    "edges": [
        {"from": "node:modes", "to": "node:agent", "type": "related"},
        {"from": "node:modes", "to": "node:sessions", "type": "related"},
        {"from": "index", "to": "node:modes", "type": "references"},
    ],
}


def _select(**kw):
    kw.setdefault("graph", GRAPH)
    kw.setdefault("map_dir", "/nonexistent")
    return select_relevant_nodes(**kw)


def test_lexical_and_path_hit_ranks_strongest_first() -> None:
    result = _select(feature_terms=["session"])
    # session.py: path hit (source_glob) + lexical hit (token) = strongest.
    assert "node:session-py" in result
    assert result[0] == "node:session-py"
    # sessions/**: path hit only — present but ranked after.
    assert "node:sessions" in result
    assert result.index("node:session-py") < result.index("node:sessions")


def test_affected_modules_prefix_matches_coarse_node() -> None:
    result = _select(feature_terms=[], affected_modules=("modes/sdd/node_selector.py",))
    assert "node:modes" in result


def test_max_nodes_strictly_capped() -> None:
    result = _select(feature_terms=["session", "agent", "modes", "tg"], max_nodes=2)
    assert len(result) == 2


def test_no_match_returns_empty() -> None:
    assert _select(feature_terms=["xyzzy-nothing"]) == []


def test_empty_graph_returns_empty() -> None:
    assert select_relevant_nodes(feature_terms=["modes"], graph={}, map_dir="/x") == []


def test_max_nodes_zero_returns_empty() -> None:
    assert _select(feature_terms=["modes"], max_nodes=0) == []


def test_zero_scored_related_neighbour_not_pulled() -> None:
    # node:modes is related to node:agent but scores 0 for "agent" — §7.3 forbids pulling it.
    result = _select(feature_terms=["agent"], max_nodes=6)
    assert "node:agent" in result
    assert "node:modes" not in result


def test_deterministic() -> None:
    a = _select(feature_terms=["session", "agent"])
    b = _select(feature_terms=["session", "agent"])
    assert a == b


def test_load_graph_missing_returns_empty_dict() -> None:
    assert load_graph("/nonexistent/path") == {}


def test_read_node_sources_extracts_target_sections(tmp_path: Path) -> None:
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "demo.md").write_text(
        "# Node: demo\n\n"
        "## Scope\n- modes/demo/**\n\n"
        "## Purpose\nShould NOT appear.\n\n"
        "## Source of truth\n- modes/demo/mode.py\n",
        encoding="utf-8",
    )
    out = read_node_sources(str(tmp_path), ["node:demo"])
    assert "modes/demo/**" in out
    assert "modes/demo/mode.py" in out
    assert "Should NOT appear" not in out


def test_read_node_sources_skips_missing(tmp_path: Path) -> None:
    assert read_node_sources(str(tmp_path), ["node:does-not-exist"]) == ""


def test_read_node_sources_rejects_path_traversal(tmp_path: Path) -> None:
    # graph.json may be attacker-controlled; a crafted node id must not escape nodes/.
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (tmp_path / "secret.md").write_text("## Source of truth\nTOP SECRET\n", encoding="utf-8")
    out = read_node_sources(str(tmp_path), ["node:../secret"])
    assert "TOP SECRET" not in out
    assert out == ""


def test_read_node_sources_respects_char_cap(tmp_path: Path, monkeypatch) -> None:
    import modes.sdd.node_selector as ns

    monkeypatch.setattr(ns, "MAX_NODE_SOURCE_CHARS", 200)
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    ids = []
    for i in range(8):
        (nodes_dir / f"n{i}.md").write_text("## Source of truth\n" + ("x" * 100) + "\n", encoding="utf-8")
        ids.append(f"node:n{i}")
    out = ns.read_node_sources(str(tmp_path), ids)
    assert len(out) <= 200
