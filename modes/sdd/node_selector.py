"""Routing-pointer selector over the codebase map for SDD plan/tasks phases.

Read-only consumer of `.cli-proxy/.codebase_map` (the map itself is maintained by
the separate codebase_mapper process — this module never writes under it).

Given feature terms and affected modules, it ranks the COARSE directory-level nodes
of the map lexically, expands 1-hop over `related` edges, and returns a bounded list
of node ids. `read_node_sources` then gathers the `## Scope` / `## Source of truth`
sections of those nodes as routing context for the CLI prompt (which is expected to
open the actual source files).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Sequence, Set

from modes.sdk.runtime.json_normalizer import loads_safe

_log = logging.getLogger(__name__)

# Scoring weights: a structural path hit is a stronger signal than a name overlap.
W1: int = 3  # path hit (term/module matches node path or source_glob)
W2: int = 2  # lexical hit (term token matches node title/id token)

MAX_NODE_SOURCE_CHARS: int = 16000

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_SECTION_RE = re.compile(r"(?m)^(## .+)$")
_TARGET_SECTIONS = {"Scope", "Source of truth"}


def _tokenize(s: str) -> Set[str]:
    """Lowercase, split on non-alphanumeric runs, drop empties."""
    return {t for t in _TOKEN_SPLIT_RE.split(str(s or "").lower()) if t}


def _path_prefixes(path: str) -> List[str]:
    """All directory prefixes: 'modes/sdd/x.py' -> ['modes','modes/sdd','modes/sdd/x.py']."""
    parts = [p for p in str(path or "").replace("\\", "/").strip("/").split("/") if p]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _path_hit(node: dict, feature_terms: Sequence[str], affected_modules: Sequence[str]) -> bool:
    source_glob = str(node.get("source_glob") or "")
    haystack = (source_glob + "\n" + str(node.get("path") or "")).lower()
    for term in feature_terms:
        t = str(term or "").strip().lower()
        if t and t in haystack:
            return True
    for am in affected_modules:
        for prefix in _path_prefixes(am):
            if prefix and prefix in source_glob:
                return True
    return False


def _lex_hit(node: dict, feature_terms: Sequence[str]) -> bool:
    node_tokens = _tokenize(node.get("title")) | _tokenize(node.get("id"))
    query_tokens: Set[str] = set()
    for t in feature_terms:
        query_tokens |= _tokenize(t)
    return bool(query_tokens & node_tokens)


def load_graph(map_dir: str) -> dict:
    """Read+parse graph.json; return {} on any failure. Read-only."""
    path = os.path.join(str(map_dir or ""), "graph.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = loads_safe(fh.read())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def select_relevant_nodes(
    *,
    feature_terms: Sequence[str],
    affected_modules: Sequence[str] = (),
    graph: dict,
    map_dir: str,
    max_nodes: int = 6,
) -> List[str]:
    """Return up to *max_nodes* relevant node ids. Deterministic, pure in-memory."""
    nodes = list((graph or {}).get("nodes") or [])
    if max_nodes <= 0 or not nodes:
        return []
    edges = list((graph or {}).get("edges") or [])
    terms = [str(t) for t in (feature_terms or []) if str(t or "").strip()]
    mods = tuple(str(m) for m in (affected_modules or ()) if str(m or "").strip())

    # Adjacency over `related` edges (treated bidirectionally).
    adj: Dict[str, Set[str]] = {}
    for e in edges:
        if str(e.get("type") or "") != "related":
            continue
        frm, to = str(e.get("from") or ""), str(e.get("to") or "")
        if not frm or not to:
            continue
        adj.setdefault(frm, set()).add(to)
        adj.setdefault(to, set()).add(frm)

    scored: Dict[str, int] = {}
    for node in nodes:
        nid = str(node.get("id") or "")
        if not nid:
            continue
        s = (W1 if _path_hit(node, terms, mods) else 0) + (W2 if _lex_hit(node, terms) else 0)
        if s > 0:
            scored[nid] = s
    if not scored:
        return []

    ranked = sorted(scored, key=lambda nid: (-scored[nid], nid))
    top_k = ranked[:max_nodes]

    expanded: Set[str] = set(top_k)
    for nid in top_k:
        # §7.3: pull only related neighbours that ALSO scored > 0 — avoids transitive
        # noise (e.g. node:tests is related to everything). Neighbours rank by their score.
        expanded.update(n for n in adj.get(nid, set()) if n in scored)

    return sorted(expanded, key=lambda nid: (-scored.get(nid, 0), nid))[:max_nodes]


def _extract_sections(text: str, section_names: Set[str]) -> str:
    parts = _SECTION_RE.split(str(text or ""))
    result: List[str] = []
    current = None
    for part in parts:
        if part.startswith("## "):
            current = part[3:].strip()
        elif current in section_names:
            body = part.strip()
            if body:
                result.append(f"## {current}\n{body}")
    return "\n\n".join(result)


def read_node_sources(map_dir: str, node_ids: List[str]) -> str:
    """Concatenate Scope / Source-of-truth sections of the given nodes, capped. Read-only."""
    parts: List[str] = []
    total = 0
    nodes_dir = Path(str(map_dir or "")) / "nodes"
    try:
        nodes_root = nodes_dir.resolve()
    except Exception:
        return ""
    for nid in node_ids or []:
        slug = str(nid or "")
        if slug.startswith("node:"):
            slug = slug[len("node:"):]
        if not slug:
            continue
        # graph.json may be attacker-controlled (it lives in the consumed project repo);
        # a node id is used to build a file path, so confine the resolved path under nodes/
        # to block traversal / symlink escapes before reading into the LLM prompt.
        try:
            md_path = (nodes_dir / f"{slug}.md").resolve()
            md_path.relative_to(nodes_root)
        except Exception:
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        extracted = _extract_sections(text, _TARGET_SECTIONS)
        if not extracted:
            continue
        chunk = f"### {nid}\n{extracted}"
        sep = 2 if parts else 0  # the "\n\n" join separator must count against the cap
        if total + sep + len(chunk) > MAX_NODE_SOURCE_CHARS:
            remaining = MAX_NODE_SOURCE_CHARS - total - sep
            if remaining > 0:
                parts.append(chunk[:remaining])
            break
        parts.append(chunk)
        total += sep + len(chunk)
    return "\n\n".join(parts)


__all__ = ["select_relevant_nodes", "load_graph", "read_node_sources", "W1", "W2"]
