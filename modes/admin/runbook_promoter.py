"""
Promote runbook на новые серверы (обычно dev → prod).

Промоушен = редактирование frontmatter:
    - добавляем `add_servers` в `servers` (уникально, сохраняя порядок)
    - опционально повышаем `auto_action.confidence`
    - перезаписываем .md файл атомарно (через tmp+rename)

Промоутер НЕ копирует скрипты: они живут в admin_root/scripts/<rb_id>/
и общие между dev и prod (scripts shared by rb_id).

Перед промоутом может быть запущена validate_runbook (require_validation=True по умолчанию).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from .memory import ServerMemory
from .runbook_validator import ValidationReport, validate_runbook
from .runbooks import global_runbooks_dir, load_runbooks
from .snapshot_store import safe_server_id

_log = logging.getLogger(__name__)

_RB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)


class RunbookPromoteError(RuntimeError):
    """Raised when runbook promotion cannot proceed."""


@dataclass
class PromoteResult:
    rb_id: str
    added_servers: List[str] = field(default_factory=list)
    already_present: List[str] = field(default_factory=list)
    confidence_before: Optional[float] = None
    confidence_after: Optional[float] = None
    validation: Optional[ValidationReport] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rb_id": self.rb_id,
            "added_servers": list(self.added_servers),
            "already_present": list(self.already_present),
            "confidence_before": self.confidence_before,
            "confidence_after": self.confidence_after,
            "validation": self.validation.to_dict() if self.validation else None,
        }


def _find_global_runbook_path(workdir: str, rb_id: str) -> Path:
    if not _RB_ID_RE.fullmatch(str(rb_id)):
        raise RunbookPromoteError(f"rb_id invalid: {rb_id!r}")
    path = global_runbooks_dir(workdir) / f"{rb_id}.md"
    if not path.is_file():
        raise RunbookPromoteError(f"runbook {rb_id} not found under global runbooks dir")
    return path


def _parse_file(path: Path) -> tuple[Dict[str, Any], str]:
    # utf-8-sig снимает опциональный BOM, если .md отредактировали внешним редактором.
    text = path.read_text(encoding="utf-8-sig")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise RunbookPromoteError(f"no YAML frontmatter in {path}")
    try:
        front = yaml.safe_load(m.group("front")) or {}
    except yaml.YAMLError as exc:
        raise RunbookPromoteError(f"invalid frontmatter: {exc}") from exc
    if not isinstance(front, Mapping):
        raise RunbookPromoteError("frontmatter is not a mapping")
    return dict(front), m.group("body")


def _dump_frontmatter(data: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False).rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _extract_confidence(front: Mapping[str, Any]) -> Optional[float]:
    aa = front.get("auto_action")
    if not isinstance(aa, Mapping):
        return None
    raw = aa.get("confidence")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _validate_confidence(value: float) -> float:
    if not (0.0 <= float(value) <= 1.0):
        raise RunbookPromoteError(f"confidence must be in [0.0, 1.0], got {value!r}")
    return float(value)


async def promote_runbook(
    workdir: str,
    rb_id: str,
    *,
    add_servers: Sequence[str],
    confidence: Optional[float] = None,
    run_validation: bool = True,
) -> PromoteResult:
    """
    Добавляет серверы в `servers` списка runbook'а и (опционально) поднимает `auto_action.confidence`.

    - `add_servers` обязателен и не пустой; каждый server_id проходит safe_server_id.
    - `confidence`: если задан, значение должно быть в [0.0, 1.0]. Понижение допускается —
      оператор может откатить runbook снова в manual-only режим, передав 0.0.
    - `run_validation`: если True (по умолчанию), перед записью запускается validate_runbook;
      при ok=False промоушен прерывается RunbookPromoteError.
    """
    if not add_servers:
        raise RunbookPromoteError("add_servers is empty")

    path = _find_global_runbook_path(workdir, rb_id)
    front, body = _parse_file(path)

    existing_servers = list(front.get("servers") or [])
    existing_set = {str(s).strip() for s in existing_servers if str(s or "").strip()}

    normalized: List[str] = []
    for raw in add_servers:
        sid = safe_server_id(raw)
        if sid not in normalized:
            normalized.append(sid)

    added: List[str] = []
    already: List[str] = []
    for sid in normalized:
        if sid in existing_set:
            already.append(sid)
        else:
            existing_servers.append(sid)
            existing_set.add(sid)
            added.append(sid)

    confidence_before = _extract_confidence(front)
    confidence_after = confidence_before

    front["servers"] = existing_servers
    if confidence is not None:
        new_conf = _validate_confidence(confidence)
        aa = front.get("auto_action")
        if not isinstance(aa, Mapping):
            raise RunbookPromoteError("runbook has no auto_action block; cannot set confidence")
        aa_dict = dict(aa)
        aa_dict["confidence"] = new_conf
        front["auto_action"] = aa_dict
        confidence_after = new_conf

    validation: Optional[ValidationReport] = None
    if run_validation:
        validation = await validate_runbook(workdir, rb_id)
        if not validation.ok:
            raise RunbookPromoteError(
                f"runbook {rb_id} failed validation: {'; '.join(validation.errors)[:300]}",
            )

    new_text = "---\n" + _dump_frontmatter(front) + "---\n" + str(body or "").rstrip() + "\n"
    _atomic_write(path, new_text)

    try:
        _ = [rb for rb in load_runbooks(workdir) if rb.id == rb_id][0]
    except IndexError as exc:
        raise RunbookPromoteError(f"runbook {rb_id} disappeared after write") from exc

    for target_sid in added:
        try:
            ServerMemory(workdir, target_sid).append_note(
                f"RUNBOOK-PROMOTED rb={rb_id} confidence={confidence_before}→{confidence_after}",
                source="runbook_promoter",
                tags=["runbook", "promote"],
            )
        except Exception:
            _log.exception("promoter: audit note failed for %s", target_sid)

    return PromoteResult(
        rb_id=rb_id,
        added_servers=added,
        already_present=already,
        confidence_before=confidence_before,
        confidence_after=confidence_after,
        validation=validation,
    )


__all__ = [
    "PromoteResult",
    "RunbookPromoteError",
    "promote_runbook",
]
