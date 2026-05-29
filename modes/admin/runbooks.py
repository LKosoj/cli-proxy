from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml

from .snapshot_store import admin_root, server_dir

_log = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)


class RunbookError(RuntimeError):
    """Raised when a runbook file is malformed."""


@dataclass
class Runbook:
    id: str
    title: str
    path: Path
    servers: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    triggers: List[str] = field(default_factory=list)
    body: str = ""
    scope: str = "global"  # "global" or "server"
    owner_server_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "servers": list(self.servers),
            "tags": list(self.tags),
            "triggers": list(self.triggers),
            "scope": self.scope,
            "owner_server_id": self.owner_server_id,
            "path": str(self.path),
            "body": self.body,
            "metadata": dict(self.metadata),
        }

    def auto_action(self) -> Optional[Dict[str, Any]]:
        raw = self.metadata.get("auto_action")
        if isinstance(raw, Mapping):
            return dict(raw)
        return None


def global_runbooks_dir(workdir: str) -> Path:
    return admin_root(workdir) / "runbooks"


def server_runbooks_dir(workdir: str, server_id: str) -> Path:
    return server_dir(workdir, server_id) / "runbooks"


def load_runbooks(workdir: str, *, server_ids: Optional[Iterable[str]] = None) -> List[Runbook]:
    books: List[Runbook] = []
    books.extend(_load_dir(global_runbooks_dir(workdir), scope="global"))
    for sid in list(server_ids or []):
        if not sid:
            continue
        books.extend(_load_dir(server_runbooks_dir(workdir, sid), scope="server", owner=sid))
    seen_ids = set()
    unique: List[Runbook] = []
    for rb in books:
        if rb.id in seen_ids:
            _log.warning("duplicate runbook id %s at %s", rb.id, rb.path)
            continue
        seen_ids.add(rb.id)
        unique.append(rb)
    return unique


def _load_dir(path: Path, *, scope: str, owner: Optional[str] = None) -> List[Runbook]:
    if not path.is_dir():
        return []
    out: List[Runbook] = []
    for md in sorted(path.glob("*.md")):
        try:
            out.append(_parse_runbook(md, scope=scope, owner=owner))
        except RunbookError as exc:
            _log.warning("skipping runbook %s: %s", md, exc)
    return out


def _parse_runbook(path: Path, *, scope: str, owner: Optional[str]) -> Runbook:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise RunbookError(f"no YAML frontmatter in {path}")
    try:
        front = yaml.safe_load(match.group("front")) or {}
    except yaml.YAMLError as exc:
        raise RunbookError(f"invalid frontmatter in {path}: {exc}") from exc
    if not isinstance(front, Mapping):
        raise RunbookError(f"frontmatter in {path} is not a mapping")

    rb_id = str(front.get("id") or path.stem).strip()
    if not rb_id:
        raise RunbookError(f"runbook {path} has empty id")
    title = str(front.get("title") or rb_id).strip()
    servers = _coerce_str_list(front.get("servers"))
    tags = _coerce_str_list(front.get("tags"))
    triggers = _coerce_str_list(front.get("triggers"))
    known_keys = {"id", "title", "servers", "tags", "triggers"}
    metadata = {str(k): v for k, v in front.items() if str(k) not in known_keys}

    return Runbook(
        id=rb_id,
        title=title,
        path=path,
        servers=servers,
        tags=tags,
        triggers=triggers,
        body=match.group("body").strip(),
        scope=scope,
        owner_server_id=owner,
        metadata=metadata,
    )


def match_runbooks(
    runbooks: List[Runbook],
    *,
    server_id: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
) -> List[Runbook]:
    """
    Возвращает отсортированный по релевантности список runbook'ов.

    Scoring:
      - совпадение по server_id (glob в frontmatter `servers`) → +2
      - пересечение tags → +1 за каждое
      - per-server scope с точным owner match → +3
      - runbooks без server/tag фильтров в frontmatter принимаются как "общие" (+0.1 чтобы не совпадали)
    """
    tag_set = {str(t).strip() for t in (tags or []) if str(t or "").strip()}
    sid = str(server_id or "").strip() or None
    scored: List[tuple[float, Runbook]] = []
    for rb in runbooks:
        score = _score_runbook(rb, server_id=sid, tags=tag_set)
        if score > 0:
            scored.append((score, rb))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [rb for _, rb in scored]


def _score_runbook(
    rb: Runbook,
    *,
    server_id: Optional[str],
    tags: set,
) -> float:
    score = 0.0
    server_filter_present = bool(rb.servers)
    tag_filter_present = bool(rb.tags)

    if rb.scope == "server" and server_id and rb.owner_server_id == server_id:
        score += 3

    if server_filter_present and server_id:
        if any(fnmatch.fnmatch(server_id, pattern) for pattern in rb.servers):
            score += 2

    if tag_filter_present and tags:
        overlap = len(tags & set(rb.tags))
        score += float(overlap)

    if not server_filter_present and not tag_filter_present:
        # runbook "общего назначения" — матчится всем, но слабо
        score += 0.1

    return score


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [str(value).strip()]


def summarize_runbooks(runbooks: List[Runbook], *, limit: int = 5) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rb in runbooks[: max(1, int(limit))]:
        out.append({
            "id": rb.id,
            "title": rb.title,
            "scope": rb.scope,
            "tags": list(rb.tags),
            "servers": list(rb.servers),
            "path": str(rb.path),
        })
    return out


__all__ = [
    "Runbook",
    "RunbookError",
    "global_runbooks_dir",
    "load_runbooks",
    "match_runbooks",
    "server_runbooks_dir",
    "summarize_runbooks",
]
