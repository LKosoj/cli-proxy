from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from glob import glob
from typing import Any, Dict, Optional

import yaml


logger = logging.getLogger(__name__)


def _clean_text(value: Any, *, max_len: int = 512) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _dedupe_strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        token = _clean_text(item, max_len=256)
        if not token or token in seen:
            continue
        result.append(token)
        seen.add(token)
    return tuple(result)


def _parse_front_matter(raw: str) -> tuple[Dict[str, Any], str]:
    text = str(raw or "")
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---", 4)
    if closing < 0:
        return {}, text
    header = text[4:closing]
    body = text[closing + 4:]
    if body.startswith("\n"):
        body = body[1:]
    try:
        payload = yaml.safe_load(header)
    except Exception:
        logger.exception("skill registry: failed to parse SKILL.md front matter")
        payload = {}
    return payload if isinstance(payload, dict) else {}, body


def _extract_description(body: str) -> str:
    lines = [line.strip() for line in str(body or "").splitlines()]
    for line in lines:
        if not line or line.startswith("#"):
            continue
        return _clean_text(line, max_len=512)
    for line in lines:
        if line.startswith("#"):
            return _clean_text(line.lstrip("#").strip(), max_len=512)
    return ""


@dataclass(frozen=True)
class SkillManifest:
    skill_id: str
    title: str
    description: str
    source: str
    scope: str
    root_path: str
    skill_path: str
    manifest_path: str
    tags: tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "scope": self.scope,
            "root_path": self.root_path,
            "skill_path": self.skill_path,
            "manifest_path": self.manifest_path,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SkillRegistrySnapshot:
    global_manifests: Dict[str, SkillManifest]
    project_manifests: Dict[str, SkillManifest]
    effective_manifests: Dict[str, SkillManifest]
    collisions: Dict[str, list[str]]

    def available_skill_set_hash(self) -> str:
        material = [
            (
                f"{skill_id}|{manifest.source}|{manifest.manifest_path}|"
                f"{manifest.metadata.get('skill_md_sha256') or ''}"
            )
            for skill_id, manifest in sorted(self.effective_manifests.items())
        ]
        digest = hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


class SkillRegistryService:
    def __init__(self, config: Any, logger_: Optional[logging.Logger] = None) -> None:
        self.config = config
        self._logger = logger_ or logger

    def scan_global_registry(self) -> Dict[str, SkillManifest]:
        defaults = getattr(self.config, "defaults", None)
        base_root = os.path.abspath(str(getattr(defaults, "workdir", "") or os.getcwd()))
        registry_paths = list(getattr(defaults, "skill_registry_paths", None) or [".cli-proxy/skills"])
        manifests, _collisions = self._scan_registry_paths(
            base_root=base_root,
            registry_paths=registry_paths,
            scope="global",
        )
        return manifests

    def scan_project_registry(self, session: Any) -> Dict[str, SkillManifest]:
        defaults = getattr(self.config, "defaults", None)
        base_root = os.path.abspath(
            str(
                getattr(session, "project_root", None)
                or getattr(session, "workdir", None)
                or getattr(defaults, "workdir", None)
                or os.getcwd()
            )
        )
        registry_paths = list(getattr(defaults, "skill_registry_paths", None) or [".cli-proxy/skills"])
        manifests, _collisions = self._scan_registry_paths(
            base_root=base_root,
            registry_paths=registry_paths,
            scope="project",
        )
        return manifests

    def load_registry(self, *, session: Any | None = None) -> SkillRegistrySnapshot:
        global_manifests, global_collisions = self._scan_registry_paths(
            base_root=os.path.abspath(
                str(getattr(getattr(self.config, "defaults", None), "workdir", "") or os.getcwd())
            ),
            registry_paths=list(getattr(getattr(self.config, "defaults", None), "skill_registry_paths", None) or [".cli-proxy/skills"]),
            scope="global",
        )
        if session is None:
            project_manifests: Dict[str, SkillManifest] = {}
            project_collisions: Dict[str, list[str]] = {}
        else:
            project_manifests, project_collisions = self._scan_registry_paths(
                base_root=os.path.abspath(
                    str(
                        getattr(session, "project_root", None)
                        or getattr(session, "workdir", None)
                        or getattr(getattr(self.config, "defaults", None), "workdir", None)
                        or os.getcwd()
                    )
                ),
                registry_paths=list(getattr(getattr(self.config, "defaults", None), "skill_registry_paths", None) or [".cli-proxy/skills"]),
                scope="project",
            )
        effective = dict(global_manifests)
        effective.update(project_manifests)
        collisions = self._merge_collisions(global_collisions, project_collisions)
        for skill_id, manifest in project_manifests.items():
            global_manifest = global_manifests.get(skill_id)
            if global_manifest is None:
                continue
            collisions.setdefault(skill_id, [])
            for path in (global_manifest.manifest_path, manifest.manifest_path):
                if path not in collisions[skill_id]:
                    collisions[skill_id].append(path)
        return SkillRegistrySnapshot(
            global_manifests=global_manifests,
            project_manifests=project_manifests,
            effective_manifests=effective,
            collisions=collisions,
        )

    def _scan_registry_paths(
        self,
        *,
        base_root: str,
        registry_paths: list[str],
        scope: str,
    ) -> tuple[Dict[str, SkillManifest], Dict[str, list[str]]]:
        manifests: Dict[str, SkillManifest] = {}
        collisions: Dict[str, list[str]] = {}
        for raw_path in registry_paths:
            resolved_root = self._resolve_registry_path(base_root=base_root, raw_path=raw_path)
            source = self._detect_source(scope=scope, raw_path=raw_path, resolved_root=resolved_root)
            for manifest_path in self._discover_manifest_paths(resolved_root):
                manifest = self._load_manifest(
                    manifest_path=manifest_path,
                    root_path=resolved_root,
                    scope=scope,
                    source=source,
                )
                if manifest is None:
                    continue
                existing = manifests.get(manifest.skill_id)
                manifests[manifest.skill_id] = manifest
                if existing is None:
                    continue
                collisions.setdefault(manifest.skill_id, [])
                for path in (existing.manifest_path, manifest.manifest_path):
                    if path not in collisions[manifest.skill_id]:
                        collisions[manifest.skill_id].append(path)
        return manifests, collisions

    @staticmethod
    def _resolve_registry_path(*, base_root: str, raw_path: Any) -> str:
        token = str(raw_path or "").strip()
        if not token:
            return os.path.abspath(base_root)
        if os.path.isabs(token):
            return os.path.abspath(token)
        return os.path.abspath(os.path.join(base_root, token))

    @staticmethod
    def _detect_source(*, scope: str, raw_path: Any, resolved_root: str) -> str:
        token = str(raw_path or "").strip()
        if os.path.isabs(token):
            return "path:absolute"
        if scope == "project":
            return "local:project-registry"
        if os.path.isabs(resolved_root) and not token:
            return "path:absolute"
        return "local:global-registry"

    @staticmethod
    def _discover_manifest_paths(root_path: str) -> list[str]:
        if not os.path.isdir(root_path):
            return []
        manifest_paths = []
        direct_manifest = os.path.join(root_path, "SKILL.md")
        if os.path.isfile(direct_manifest):
            manifest_paths.append(os.path.abspath(direct_manifest))
        manifest_paths.extend(sorted(glob(os.path.join(root_path, "*", "SKILL.md"))))
        deduped: list[str] = []
        seen: set[str] = set()
        for path in manifest_paths:
            normalized = os.path.abspath(path)
            if normalized in seen:
                continue
            deduped.append(normalized)
            seen.add(normalized)
        return deduped

    def _load_manifest(
        self,
        *,
        manifest_path: str,
        root_path: str,
        scope: str,
        source: str,
    ) -> SkillManifest | None:
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except Exception:
            self._logger.exception("skill registry: failed to read manifest path=%s", manifest_path)
            return None
        header, body = _parse_front_matter(raw)
        skill_dir = os.path.dirname(manifest_path)
        if os.path.basename(manifest_path) == "SKILL.md":
            skill_id = os.path.basename(skill_dir)
        else:
            skill_id = os.path.splitext(os.path.basename(manifest_path))[0]
        title = _clean_text(header.get("name") or skill_id, max_len=256) or skill_id
        description = _clean_text(header.get("description") or _extract_description(body), max_len=512)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        metadata = {
            "front_matter": dict(header),
            "content_length": len(raw),
            "skill_md_sha256": f"sha256:{digest}",
            "selector_sidecar_path": os.path.join(skill_dir, "selector.json"),
        }
        return SkillManifest(
            skill_id=_clean_text(skill_id, max_len=128) or "unknown-skill",
            title=title,
            description=description,
            source=source,
            scope="project" if scope == "project" else "global",
            root_path=os.path.abspath(root_path),
            skill_path=os.path.abspath(skill_dir),
            manifest_path=os.path.abspath(manifest_path),
            tags=_dedupe_strings(header.get("tags")),
            metadata=metadata,
        )

    @staticmethod
    def _merge_collisions(*parts: Dict[str, list[str]]) -> Dict[str, list[str]]:
        merged: Dict[str, list[str]] = {}
        for chunk in parts:
            for skill_id, paths in chunk.items():
                bucket = merged.setdefault(skill_id, [])
                for path in paths:
                    token = str(path or "").strip()
                    if token and token not in bucket:
                        bucket.append(token)
        return merged
