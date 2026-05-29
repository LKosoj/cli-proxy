"""
Сборка runbook'а из набора шелл-скриптов.

Flow:
    1. Оператор задал набор (name, body) или список ScriptFile.
    2. Builder создаёт каталог `<admin_root>/scripts/<rb_id>/`, копирует туда скрипты,
       вычисляет SHA1, выставляет mode 0o700.
    3. Пишет .md runbook в global_runbooks_dir с frontmatter:
           id, title, servers: [dev_id], tags, steps: [{name, script, checksum, target_hint}],
           auto_action: {action_id: script.run, rb_id, step: <первый>, confidence: 0.0}.
    4. Возвращает Runbook (через повторный load), чтобы callsite мог валидировать.

Design invariants:
    - auto_action.confidence: 0.0 ⇒ RuleBased DM не возьмёт автоматически до promote.
    - admin.runbook_sources whitelist действует только на INPUT-каталог (откуда мы читаем).
      Сам admin_root считается trusted.
    - Один rb_id ↔ один каталог. Перезапись существующего — явный force=True.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from .memory import ServerMemory
from .runbooks import Runbook, global_runbooks_dir, load_runbooks
from .snapshot_store import admin_root, safe_server_id

_log = logging.getLogger(__name__)

_RB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(sh|bash)$")

ACTION_SCRIPT_RUN = "script.run"
DEFAULT_TARGET_HINT = "local"


class RunbookBuilderError(RuntimeError):
    """Raised when a runbook cannot be built (bad input or I/O failure)."""


@dataclass
class ScriptInput:
    """Представление одного скрипта в спецификации билдера."""
    name: str
    body: str
    target_hint: str = DEFAULT_TARGET_HINT

    def validated(self) -> "ScriptInput":
        n = str(self.name or "").strip()
        if not _SCRIPT_NAME_RE.fullmatch(n):
            raise RunbookBuilderError(
                f"script name invalid: {n!r} (allowed: [A-Za-z0-9_.-]+.{{sh,bash}})",
            )
        body = str(self.body or "")
        if not body.strip():
            raise RunbookBuilderError(f"script body is empty for {n}")
        target = str(self.target_hint or DEFAULT_TARGET_HINT).strip().lower()
        if target not in ("local", "ssh"):
            raise RunbookBuilderError(f"target_hint must be 'local' or 'ssh', got {target!r}")
        return ScriptInput(name=n, body=body, target_hint=target)


@dataclass
class BuildSpec:
    title: str
    dev_server_id: str
    scripts: List[ScriptInput]
    rb_id: Optional[str] = None  # если не задан — генерируется из title
    tags: List[str] = field(default_factory=list)
    triggers: List[str] = field(default_factory=list)
    description: str = ""


def _slugify(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "")).strip("-").lower()
    return s or "rb"


def _generate_rb_id(title: str, *, existing: Sequence[str]) -> str:
    base = f"rb-{_slugify(title)}"[:48]
    if base not in existing and _RB_ID_RE.fullmatch(base):
        return base
    ts = int(time.time())
    candidate = f"{base}-{ts}"
    if not _RB_ID_RE.fullmatch(candidate):
        candidate = f"rb-{ts}"
    return candidate


def scripts_dir(workdir: str, rb_id: str) -> Path:
    if not _RB_ID_RE.fullmatch(str(rb_id)):
        raise RunbookBuilderError(f"rb_id invalid: {rb_id!r}")
    return admin_root(workdir) / "scripts" / rb_id


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _dump_frontmatter(data: Mapping[str, Any]) -> str:
    # sort_keys=False чтобы порядок ключей был предсказуем и читаем.
    return yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False).rstrip() + "\n"


def _write_runbook_file(path: Path, *, frontmatter: Mapping[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "---\n" + _dump_frontmatter(frontmatter) + "---\n" + str(body or "").rstrip() + "\n"
    path.write_text(text, encoding="utf-8")


def build_runbook_from_scripts(
    workdir: str,
    spec: BuildSpec,
    *,
    force: bool = False,
) -> Runbook:
    """
    Материализует BuildSpec:
      - копирует скрипты в scripts_dir(workdir, rb_id)
      - пишет runbook .md в global_runbooks_dir
      - повторно загружает runbook через load_runbooks и возвращает Runbook.
    """
    if not isinstance(spec, BuildSpec):
        raise RunbookBuilderError("spec must be BuildSpec")
    title = str(spec.title or "").strip()
    if not title:
        raise RunbookBuilderError("title is required")
    if not spec.scripts:
        raise RunbookBuilderError("at least one script is required")

    dev_sid = safe_server_id(spec.dev_server_id)

    # rb_id: либо явно, либо сгенерирован из title + коллизия-проверка.
    existing_ids = {rb.id for rb in load_runbooks(workdir)}
    if spec.rb_id:
        rb_id = str(spec.rb_id).strip()
        if not _RB_ID_RE.fullmatch(rb_id):
            raise RunbookBuilderError(f"rb_id invalid: {rb_id!r}")
        if rb_id in existing_ids and not force:
            raise RunbookBuilderError(f"runbook {rb_id} already exists (use force=True)")
    else:
        rb_id = _generate_rb_id(title, existing=list(existing_ids))

    validated_scripts = [s.validated() for s in spec.scripts]
    # уникальность имён:
    names = [s.name for s in validated_scripts]
    if len(names) != len(set(names)):
        raise RunbookBuilderError("duplicate script names in spec")

    target_dir = scripts_dir(workdir, rb_id)
    if force and target_dir.is_dir():
        # rebuild с меньшим числом скриптов не должен оставлять orphan'ов
        # под тем же rb_id — они сбивают аудит и занимают disk.
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        target_dir.chmod(0o700)
    except OSError:
        _log.debug("could not chmod %s", target_dir)

    steps: List[Dict[str, Any]] = []
    for s in validated_scripts:
        dest = target_dir / s.name
        dest.write_text(s.body, encoding="utf-8")
        try:
            dest.chmod(0o600)
        except OSError:
            _log.debug("could not chmod %s", dest)
        steps.append({
            "name": s.name.rsplit(".", 1)[0],
            "script": s.name,
            "checksum": f"sha1:{_sha1_text(s.body)}",
            "target_hint": s.target_hint,
        })

    tags = [str(t).strip() for t in (spec.tags or []) if str(t or "").strip()]
    if "script" not in tags:
        tags.append("script")
    triggers = [str(t).strip() for t in (spec.triggers or []) if str(t or "").strip()]

    frontmatter = {
        "id": rb_id,
        "title": title,
        "servers": [dev_sid],
        "tags": tags,
        "triggers": triggers,
        "steps": steps,
        "auto_action": {
            "action_id": ACTION_SCRIPT_RUN,
            "rb_id": rb_id,
            "step": steps[0]["name"],
            "target_hint": steps[0]["target_hint"],
            "confidence": 0.0,  # manual-only до promote
        },
        "created_at": int(time.time()),
        "source": "script_builder",
    }
    if spec.description:
        frontmatter["description"] = str(spec.description).strip()

    rb_path = global_runbooks_dir(workdir) / f"{rb_id}.md"
    if rb_path.exists() and not force:
        raise RunbookBuilderError(f"runbook file already exists: {rb_path}")

    body_lines = [f"# {title}", ""]
    if spec.description:
        body_lines.extend([spec.description.strip(), ""])
    body_lines.append("## Steps")
    for s in validated_scripts:
        body_lines.append(f"- `{s.name}` — target: `{s.target_hint}`")
    body = "\n".join(body_lines) + "\n"

    _write_runbook_file(rb_path, frontmatter=frontmatter, body=body)

    try:
        ServerMemory(workdir, dev_sid).append_note(
            f"RUNBOOK-BUILT rb={rb_id} title={title!r} steps={len(steps)} "
            f"source=script_builder force={bool(force)}",
            source="runbook_builder",
            tags=["runbook", "build"],
        )
    except Exception:
        _log.exception("runbook_builder: audit note failed for %s", dev_sid)

    for rb in load_runbooks(workdir):
        if rb.id == rb_id:
            return rb
    raise RunbookBuilderError(f"runbook {rb_id} written but cannot be re-loaded from {rb_path}")


__all__ = [
    "ACTION_SCRIPT_RUN",
    "BuildSpec",
    "DEFAULT_TARGET_HINT",
    "RunbookBuilderError",
    "ScriptInput",
    "build_runbook_from_scripts",
    "scripts_dir",
]
