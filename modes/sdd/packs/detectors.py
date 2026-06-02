from __future__ import annotations

import fnmatch
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List

import tomllib

from .schema import EvidenceItem, PackDefinition, PackScore

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "target",
    "build",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    ".cli-proxy/runs",
    ".cli-proxy/cli-json-stream",
}


def score_pack(pack: PackDefinition, *, workdir: str, codebase_context: str = "") -> PackScore:
    root = Path(str(workdir or "").strip())
    rules = list((pack.detectors or {}).get("rules") or [])
    evidence: List[EvidenceItem] = []
    matched_groups: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        item = _evaluate_rule(pack, rule, root=root, codebase_context=codebase_context)
        if item is not None:
            evidence.append(item)
            if item.group:
                matched_groups.add(item.group)
    required_groups = [
        str(group or "").strip()
        for group in list((pack.detectors or {}).get("evidence_groups_required") or [])
        if str(group or "").strip()
    ]
    missing = [group for group in required_groups if group not in matched_groups]
    score = _weighted_score(evidence)
    if missing:
        score = min(score, 0.49)
    return PackScore(pack=pack, score=score, evidence=evidence, missing_groups=missing)


def meaningful_files(workdir: str) -> List[str]:
    root = Path(str(workdir or "").strip())
    if not root.exists() or not root.is_dir():
        return []
    out: List[str] = []
    for path in _iter_files(root):
        rel = _rel(path, root)
        if rel and not _is_generated_sdd_path(rel):
            out.append(rel)
    return sorted(out)


def _weighted_score(evidence: Iterable[EvidenceItem]) -> float:
    items = list(evidence)
    if not items:
        return 0.0
    total_weight = sum(max(0.0, float(item.weight)) for item in items) or 1.0
    score = sum(max(0.0, float(item.weight)) * max(0.0, min(1.0, float(item.confidence))) for item in items)
    return max(0.0, min(1.0, score / total_weight))


def _evaluate_rule(
    pack: PackDefinition,
    rule: Dict[str, Any],
    *,
    root: Path,
    codebase_context: str,
) -> EvidenceItem | None:
    kind = str(rule.get("kind") or "").strip()
    rule_id = str(rule.get("id") or kind or "rule").strip()
    group = str(rule.get("group") or "default").strip()
    weight = _float(rule.get("weight"), default=1.0)
    confidence = _float(rule.get("confidence"), default=1.0)
    reason = str(rule.get("reason") or "").strip() or f"matched {kind}"
    path_val = str(rule.get("path") or "").strip()
    if kind == "file_exists":
        path = _safe_child_path(root, path_val)
        if path is None:
            return None
        if path.is_file():
            return _e(pack, rule_id, group, kind, _rel(path, root), reason, weight, confidence)
        return None
    if kind == "dir_exists":
        path = _safe_child_path(root, path_val)
        if path is None:
            return None
        if path.is_dir():
            return _e(pack, rule_id, group, kind, _rel(path, root), reason, weight, confidence)
        return None
    if kind == "any_file_matches":
        pattern = str(rule.get("pattern") or path_val or "").strip()
        found = _find_pattern(root, pattern)
        if found:
            return _e(pack, rule_id, group, kind, found, reason, weight, confidence)
        return None
    if kind == "ext_count":
        ext = str(rule.get("ext") or "").strip().lower()
        min_count = int(rule.get("min") or 1)
        count = sum(1 for p in _iter_files(root) if p.suffix.lower() == ext)
        if ext and count >= min_count:
            return _e(pack, rule_id, group, kind, f"*{ext}", reason, weight, confidence, value=str(count))
        return None
    if kind == "json_field":
        return _json_field(pack, rule, root=root, rule_id=rule_id, group=group, weight=weight, confidence=confidence)
    if kind == "toml_field":
        return _toml_field(pack, rule, root=root, rule_id=rule_id, group=group, weight=weight, confidence=confidence)
    if kind == "xml_hint":
        return _xml_hint(pack, rule, root=root, rule_id=rule_id, group=group, weight=weight, confidence=confidence)
    if kind == "content_regex":
        return _content_regex(pack, rule, root=root, rule_id=rule_id, group=group, weight=weight, confidence=confidence)
    if kind == "codebase_map_contains":
        needle = str(rule.get("text") or "").strip().lower()
        if needle and needle in str(codebase_context or "").lower():
            return _e(pack, rule_id, group, kind, ".cli-proxy/.codebase_map", reason, weight, confidence, source="codebase_map")
        return None
    return None


def _json_field(
    pack: PackDefinition,
    rule: Dict[str, Any],
    *,
    root: Path,
    rule_id: str,
    group: str,
    weight: float,
    confidence: float,
) -> EvidenceItem | None:
    rel_path = str(rule.get("path") or "").strip()
    path = root / rel_path
    path = _safe_child_path(root, rel_path)
    if path is None:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = _lookup_path(data, str(rule.get("field") or ""))
    if _matches_expected(value, rule.get("equals"), rule.get("contains")):
        return _e(
            pack,
            rule_id,
            group,
            "json_field",
            rel_path,
            str(rule.get("reason") or "matched JSON field"),
            weight,
            confidence,
            value=str(value),
        )
    return None


def _toml_field(
    pack: PackDefinition,
    rule: Dict[str, Any],
    *,
    root: Path,
    rule_id: str,
    group: str,
    weight: float,
    confidence: float,
) -> EvidenceItem | None:
    rel_path = str(rule.get("path") or "").strip()
    path = _safe_child_path(root, rel_path)
    if path is None:
        return None
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = _lookup_path(data, str(rule.get("field") or ""))
    if _matches_expected(value, rule.get("equals"), rule.get("contains")):
        return _e(
            pack,
            rule_id,
            group,
            "toml_field",
            rel_path,
            str(rule.get("reason") or "matched TOML field"),
            weight,
            confidence,
            value=str(value),
        )
    return None


def _xml_hint(
    pack: PackDefinition,
    rule: Dict[str, Any],
    *,
    root: Path,
    rule_id: str,
    group: str,
    weight: float,
    confidence: float,
) -> EvidenceItem | None:
    pattern = str(rule.get("pattern") or "*.csproj").strip()
    sdk_contains = str(rule.get("sdk_contains") or "").strip().lower()
    for rel in _matching_files(root, pattern):
        path = _safe_child_path(root, rel)
        if path is None:
            continue
        try:
            tree = ET.parse(path)
            sdk = str(tree.getroot().attrib.get("Sdk") or "")
        except Exception:
            content = path.read_text(encoding="utf-8", errors="ignore")
            sdk = content
        if not sdk_contains or sdk_contains in sdk.lower():
            return _e(
                pack,
                rule_id,
                group,
                "xml_hint",
                rel,
                str(rule.get("reason") or "matched XML hint"),
                weight,
                confidence,
                value=sdk[:120],
            )
    return None


def _content_regex(
    pack: PackDefinition,
    rule: Dict[str, Any],
    *,
    root: Path,
    rule_id: str,
    group: str,
    weight: float,
    confidence: float,
) -> EvidenceItem | None:
    rel_path = str(rule.get("path") or "").strip()
    pattern = str(rule.get("regex") or "").strip()
    if not rel_path or not pattern:
        return None
    path = _safe_child_path(root, rel_path)
    if path is None:
        return None
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    if re.search(pattern, content, flags=re.MULTILINE):
        return _e(pack, rule_id, group, "content_regex", rel_path, str(rule.get("reason") or "matched content"), weight, confidence)
    return None


def _lookup_path(data: Any, dotted: str) -> Any:
    cur = data
    for part in [p for p in str(dotted or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _matches_expected(value: Any, equals: Any, contains: Any) -> bool:
    if equals is not None:
        return value == equals
    if contains is not None:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        return str(contains).lower() in text.lower()
    return value is not None


def _find_pattern(root: Path, pattern: str) -> str:
    for rel in _matching_files(root, pattern):
        return rel
    return ""


def _matching_files(root: Path, pattern: str) -> List[str]:
    pat = str(pattern or "").replace("\\", "/")
    if _unsafe_rel_token(pat):
        return []
    out: List[str] = []
    for path in _iter_files(root):
        rel = _rel(path, root)
        if _pattern_matches(rel, pat):
            out.append(rel)
    return sorted(out)


def _pattern_matches(rel: str, pattern: str) -> bool:
    if fnmatch.fnmatch(rel, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(rel, pattern[3:])
    return False


def _iter_files(root: Path) -> Iterable[Path]:
    for base, dirs, files in os.walk(root):
        rel_base = _rel(Path(base), root)
        dirs[:] = [
            d for d in dirs
            if not _is_ignored_dir((f"{rel_base}/{d}" if rel_base else d).replace("\\", "/"))
        ]
        for name in files:
            path = Path(base) / name
            rel = _rel(path, root)
            if rel and not _is_ignored_dir(rel):
                safe = _safe_child_path(root, rel)
                if safe is None or not safe.is_file():
                    continue
                yield path


def _is_ignored_dir(rel: str) -> bool:
    token = str(rel or "").replace("\\", "/").strip("/")
    parts = token.split("/")
    if any(part in _IGNORED_DIRS for part in parts):
        return True
    return any(token.startswith(f"{ignored}/") for ignored in _IGNORED_DIRS)


def _is_generated_sdd_path(rel: str) -> bool:
    token = str(rel or "").replace("\\", "/").strip("/")
    return (
        token.startswith(".cli-proxy/sdd/")
        or token.startswith("specs/_project/")
        or token.startswith("specs/_templates/")
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except Exception:
        return ""


def _safe_child_path(root: Path, rel_path: str) -> Path | None:
    token = str(rel_path or "").replace("\\", "/").strip()
    if _unsafe_rel_token(token):
        return None
    try:
        resolved = (root / token).resolve()
        resolved.relative_to(root.resolve())
    except Exception:
        return None
    return resolved


def _unsafe_rel_token(token: str) -> bool:
    value = str(token or "").replace("\\", "/").strip()
    if not value or value.startswith("/"):
        return True
    return any(part == ".." for part in value.split("/"))


def _e(
    pack: PackDefinition,
    rule_id: str,
    group: str,
    kind: str,
    path: str,
    reason: str,
    weight: float,
    confidence: float,
    *,
    value: str = "",
    source: str = "workdir",
) -> EvidenceItem:
    return EvidenceItem(
        pack_id=pack.pack_id,
        rule_id=rule_id,
        group=group,
        kind=kind,
        path=path,
        reason=reason,
        weight=weight,
        confidence=confidence,
        value=value,
        source=source,
    )


def _float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
