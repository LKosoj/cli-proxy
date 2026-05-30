"""Pure graph-analysis helpers extracted from CodebaseMapperRuntime.

All functions in this module are free of instance state — they take only
explicit arguments and are safe to call without a CodebaseMapperRuntime
instance.
"""
from __future__ import annotations

import ast
import logging
import math
import os
import re
import subprocess
from typing import Any, Dict, List, Sequence, Set, Tuple

_log = logging.getLogger(__name__)

# ── constants (must stay in sync with runtime.py) ──────────────────────────
_CMD_TIMEOUT_SEC: int = 30
_MAP_REL_ROOT_SLASH: str = ".cli-proxy/.codebase_map/"
_LEGACY_MAP_REL_ROOT_SLASH: str = ".codebase_map/"

_SOURCE_SAMPLES_MIN: int = 12
_SOURCE_SAMPLES_MAX: int = 24
_SOURCE_SAMPLES_MIN_DEEP: int = 16
_SOURCE_SAMPLES_MAX_DEEP: int = 40


# ── low-level helpers ───────────────────────────────────────────────────────

def _run_cmd(args: List[str], cwd: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    effective_timeout = timeout if timeout is not None else _CMD_TIMEOUT_SEC
    try:
        return subprocess.run(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args, returncode=124, stdout="",
            stderr=f"timeout: command did not complete within {effective_timeout}s",
        )


def _is_map_relative_path(path: str) -> bool:
    p = str(path or "").replace("\\", "/").strip("/")
    return p.startswith(_MAP_REL_ROOT_SLASH) or p.startswith(_LEGACY_MAP_REL_ROOT_SLASH)


def _path_to_area(path: str) -> str:
    p = str(path or "").replace("\\", "/").strip("/")
    if not p or _is_map_relative_path(p):
        return ""
    if "/" not in p:
        return p if not p.endswith(".md") else ""
    first = p.split("/", 1)[0]
    if first in {".git", ".venv", "__pycache__", "node_modules"}:
        return ""
    return first


def _resolve_alias_token(raw: str, *, known_aliases: Dict[str, str]) -> str:
    ref = str(raw or "").replace("\\", "/").strip().strip("./")
    if not ref:
        return ""
    head = ref.split("/", 1)[0].split(".", 1)[0].strip().lower()
    if not head:
        return ""
    return str(known_aliases.get(head) or "")


def _resolve_call_head_name(func: ast.AST) -> str:
    cur = func
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return str(cur.id or "").strip()
    return ""


# ── C1 graph-analysis candidates ───────────────────────────────────────────

def _build_graph_edges(
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
    *,
    files: Sequence[str],
    domains: Sequence[str],
) -> Dict[str, List[str]]:
    known: Dict[str, List[str]] = {str(x): [] for x in list(domains or [])}
    for raw in list(files or []):
        p = str(raw).replace("\\", "/").strip("/")
        if not p:
            continue
        area = _path_to_area(p)
        if area in known:
            known[area].append(p)
        if "workspace" in known:
            known["workspace"].append(p)
    return {k: sorted(v) for k, v in known.items()}


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


def _select_domain_source_samples(
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
    cap = _domain_source_samples_cap(total=len(unique_files), operation=operation)
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


def _extract_domain_refs_by_regex(
    *,
    content: str,
    known_domains: Set[str],
    patterns: Sequence[re.Pattern],
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


def _relation_graph_payload(
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
        _log.exception("codebase map: failed to parse python file %s", abs_path)
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
        head = _resolve_call_head_name(node.func)
        if not head:
            continue
        target = str(imported_aliases.get(head) or known_aliases.get(head) or "")
        if target:
            out.add(target)
    return out


def _extract_ts_js_dependency_aliases(
    *,
    abs_path: str,
    known_aliases: Dict[str, str],
) -> Set[str]:
    out: Set[str] = set()
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception:
        _log.exception("codebase map: failed to read ts/js file %s", abs_path)
        return out
    patterns = [
        re.compile(r"import\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]"),
        re.compile(r"export\s+[^;]*?\s+from\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"import\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    ]
    for pat in patterns:
        for raw in pat.findall(source):
            token = _resolve_alias_token(str(raw), known_aliases=known_aliases)
            if token:
                out.add(token)
    return out


def _extract_php_dependency_aliases(
    *,
    abs_path: str,
    known_aliases: Dict[str, str],
) -> Set[str]:
    out: Set[str] = set()
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception:
        _log.exception("codebase map: failed to read php file %s", abs_path)
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
            token = _resolve_alias_token(str(raw), known_aliases=known_aliases)
            if token:
                out.add(token)
    return out


def _extract_go_dependency_aliases(
    *,
    abs_path: str,
    known_aliases: Dict[str, str],
) -> Set[str]:
    out: Set[str] = set()
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception:
        _log.exception("codebase map: failed to read go file %s", abs_path)
        return out
    patterns = [
        re.compile(r'import\s+"([^"]+)"'),
        re.compile(r'import\s*\((.*?)\)', re.DOTALL),
    ]
    for raw in patterns[0].findall(source):
        token = _resolve_alias_token(str(raw), known_aliases=known_aliases)
        if token:
            out.add(token)
    for block in patterns[1].findall(source):
        for raw in re.findall(r'"([^"]+)"', str(block or "")):
            token = _resolve_alias_token(str(raw), known_aliases=known_aliases)
            if token:
                out.add(token)
    return out


def _extract_rust_dependency_aliases(
    *,
    abs_path: str,
    known_aliases: Dict[str, str],
) -> Set[str]:
    out: Set[str] = set()
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception:
        _log.exception("codebase map: failed to read rust file %s", abs_path)
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


def _collect_regex_relations(
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
                _log.exception("codebase map: regex relation read failed %s", abs_path)
                continue
            refs = _extract_domain_refs_by_regex(content=content, known_domains=known, patterns=patterns)
            for target in refs:
                if target == domain:
                    continue
                _merge_relation(
                    out=out,
                    source=domain,
                    target=target,
                    level="L1",
                    score=0.62,
                    evidence=[f"regex_ref:{rel}"],
                )


def _extract_observed_pairs_from_git(*, root: str) -> List[Tuple[str, str, float, List[str]]]:
    proc = _run_cmd(["git", "-C", root, "log", "--name-only", "--pretty=format:__COMMIT__", "-n", "120"])
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
        area = _path_to_area(s)
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


def _infer_domain_relations(
    *,
    root: str,
    domain_files: Dict[str, List[str]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    domains = [str(d) for d in domain_files.keys() if str(d) and str(d) != "workspace"]
    out: Dict[str, Dict[str, Dict[str, Any]]] = {d: {} for d in domains}

    # L0: observed co-change from git history (works for any language).
    for a, b, conf, evidence in _extract_observed_pairs_from_git(root=root):
        if a in out and b in out:
            _merge_relation(
                out=out,
                source=a,
                target=b,
                level="L0",
                score=float(conf),
                evidence=list(evidence or []),
            )
            _merge_relation(
                out=out,
                source=b,
                target=a,
                level="L0",
                score=float(conf),
                evidence=list(evidence or []),
            )

    # L1: regex-based import/include/use patterns for many languages.
    _collect_regex_relations(root=root, domain_files=domain_files, out=out)

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
                parsed = _extract_python_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
            elif low.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
                parsed = _extract_ts_js_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
            elif low.endswith(".php"):
                parsed = _extract_php_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
            elif low.endswith(".go"):
                parsed = _extract_go_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
            elif low.endswith(".rs"):
                parsed = _extract_rust_dependency_aliases(abs_path=abs_path, known_aliases=aliases)
            if not parsed:
                continue
            for target in parsed:
                if target == domain or target not in out:
                    continue
                _merge_relation(
                    out=out,
                    source=domain,
                    target=target,
                    level="L2",
                    score=0.9,
                    evidence=[f"lang_specific:{rel}"],
                )
    return out
