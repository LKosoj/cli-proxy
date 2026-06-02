from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterable, List

import yaml

from .schema import PackDefinition
from .schema import ensure_safe_token

_PACKS_DIR = Path(__file__).resolve().parent
_BUILTIN_DIR = _PACKS_DIR / "builtin"


class PackRegistry:
    def __init__(self, packs: Iterable[PackDefinition]) -> None:
        self._packs: Dict[str, PackDefinition] = {}
        for pack in packs:
            self.add(pack)

    def all(self) -> List[PackDefinition]:
        return list(self._packs.values())

    def get(self, pack_id: str) -> PackDefinition | None:
        return self._packs.get(str(pack_id or "").strip())

    def add(self, pack: PackDefinition) -> None:
        existing = self._packs.get(pack.pack_id)
        if existing is not None and str(pack.source or "") == "proposed":
            return
        self._packs[pack.pack_id] = pack


def load_pack_registry(*, workdir: str = "") -> PackRegistry:
    packs: List[PackDefinition] = []
    packs.extend(_load_pack_dir(_BUILTIN_DIR, source="builtin"))
    root = Path(str(workdir or "").strip()) if workdir else None
    if root:
        project_dir = _safe_existing_child_dir(root, ".cli-proxy", "sdd", "packs", "project")
        proposed_dir = _safe_existing_child_dir(root, ".cli-proxy", "sdd", "packs", "proposed")
        if project_dir is not None:
            packs.extend(_load_pack_dir(project_dir, source="project", allowed_root=root.resolve(strict=True)))
        if proposed_dir is not None:
            packs.extend(_load_pack_dir(proposed_dir, source="proposed", allowed_root=root.resolve(strict=True)))
    return PackRegistry(packs)


def save_project_pack_index(*, workdir: str, packs: Iterable[PackDefinition]) -> str:
    root = Path(str(workdir or "").strip())
    out_dir = _safe_child_dir(root, ".cli-proxy", "sdd", "packs", create=True)
    path = out_dir / "index.json"
    payload = {
        "packs": [
            {
                "pack_id": pack.pack_id,
                "lifecycle": pack.lifecycle,
                "source": pack.source,
                "version": pack.version,
                "hash": pack.stable_hash(),
            }
            for pack in sorted(packs, key=lambda item: item.pack_id)
        ]
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def write_pack_definition(*, workdir: str, pack: PackDefinition, lifecycle: str) -> str:
    lifecycle_norm = ensure_safe_token(
        str(lifecycle or pack.lifecycle or "proposed").strip() or "proposed",
        field_name="pack lifecycle",
    )
    pack_id = ensure_safe_token(pack.pack_id, field_name="pack id")
    root = Path(str(workdir or "").strip())
    out_dir = _safe_child_dir(root, ".cli-proxy", "sdd", "packs", lifecycle_norm, create=True)
    path = out_dir / f"{pack_id}.yaml"
    payload = pack.to_dict()
    payload["lifecycle"] = lifecycle_norm
    payload["source"] = lifecycle_norm
    _atomic_write_text(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
    return str(path)


def _load_pack_dir(path: Path, *, source: str, allowed_root: Path | None = None) -> List[PackDefinition]:
    if not path.exists() or not path.is_dir():
        return []
    out: List[PackDefinition] = []
    base = path.resolve()
    if allowed_root is not None:
        try:
            base.relative_to(allowed_root)
        except Exception:
            return []
    for child in sorted(path.iterdir(), key=lambda p: p.name):
        if child.suffix.lower() not in {".yaml", ".yml"}:
            continue
        try:
            resolved = child.resolve(strict=True)
            resolved.relative_to(base)
        except Exception:
            continue
        if not resolved.is_file():
            continue
        try:
            data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
            pack = PackDefinition.from_dict(data, source=source)
        except Exception as exc:
            raise ValueError(f"failed to load SDD pack {os.fspath(child)}: {exc}") from exc
        out.append(pack)
    return out


def _safe_existing_child_dir(root: Path, *parts: str) -> Path | None:
    if not str(root):
        return None
    path = root.joinpath(*parts)
    if not path.exists():
        return None
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except Exception:
        return None
    if not resolved.is_dir():
        return None
    return path


def _safe_child_dir(root: Path, *parts: str, create: bool) -> Path:
    if not str(root):
        raise ValueError("workdir_not_found")
    root_resolved = root.resolve(strict=True)
    path = root.joinpath(*parts)
    _assert_under_root(root_resolved, path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
        _assert_under_root(root_resolved, path)
    return path


def _assert_under_root(root_resolved: Path, path: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root_resolved)
    except Exception as exc:
        raise RuntimeError(f"unsafe_sdd_pack_path:{path}") from exc


def _atomic_write_text(path: Path, text: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    if tmp_path.exists() or tmp_path.is_symlink():
        tmp_path.unlink()
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)
