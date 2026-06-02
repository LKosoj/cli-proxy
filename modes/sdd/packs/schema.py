from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List


PACK_SCHEMA_VERSION = "1"
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class EvidenceItem:
    pack_id: str
    rule_id: str
    group: str
    kind: str
    path: str
    reason: str
    weight: float = 1.0
    confidence: float = 1.0
    value: str = ""
    source: str = "workdir"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "rule_id": self.rule_id,
            "group": self.group,
            "kind": self.kind,
            "path": self.path,
            "reason": self.reason,
            "weight": float(self.weight),
            "confidence": float(self.confidence),
            "value": self.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class PackDefinition:
    pack_id: str
    title: str
    lifecycle: str
    version: str
    source: str
    detectors: Dict[str, Any] = field(default_factory=dict)
    sdd: Dict[str, Any] = field(default_factory=dict)
    merge: Dict[str, Any] = field(default_factory=dict)
    applies_to: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], *, source: str) -> "PackDefinition":
        if not isinstance(data, dict):
            raise ValueError("pack definition must be an object")
        schema_version = str(data.get("schema_version") or "").strip()
        if schema_version != PACK_SCHEMA_VERSION:
            raise ValueError(f"unsupported pack schema_version: {schema_version!r}")
        pack_id = _require_token(data, "pack_id")
        title = _require_text(data, "title")
        lifecycle = ensure_safe_token(
            str(data.get("lifecycle") or "builtin").strip() or "builtin",
            field_name="pack lifecycle",
        )
        version = str(data.get("version") or "1.0").strip() or "1.0"
        detectors = data.get("detectors") if isinstance(data.get("detectors"), dict) else {}
        sdd = data.get("sdd") if isinstance(data.get("sdd"), dict) else {}
        merge = data.get("merge") if isinstance(data.get("merge"), dict) else {}
        applies_to = data.get("applies_to") if isinstance(data.get("applies_to"), dict) else {}
        provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
        return cls(
            pack_id=pack_id,
            title=title,
            lifecycle=lifecycle,
            version=version,
            source=str(source or data.get("source") or "builtin"),
            detectors=dict(detectors),
            sdd=dict(sdd),
            merge=dict(merge),
            applies_to=dict(applies_to),
            provenance=dict(provenance),
            raw=dict(data),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = dict(self.raw)
        payload.setdefault("schema_version", PACK_SCHEMA_VERSION)
        payload["pack_id"] = self.pack_id
        payload["title"] = self.title
        payload["lifecycle"] = self.lifecycle
        payload["version"] = self.version
        payload["source"] = self.source
        payload["detectors"] = dict(self.detectors)
        payload["sdd"] = dict(self.sdd)
        payload["merge"] = dict(self.merge)
        payload["applies_to"] = dict(self.applies_to)
        payload["provenance"] = dict(self.provenance)
        return payload

    def stable_hash(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class PackScore:
    pack: PackDefinition
    score: float
    evidence: List[EvidenceItem]
    missing_groups: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack.pack_id,
            "title": self.pack.title,
            "lifecycle": self.pack.lifecycle,
            "pack": self.pack.to_dict(),
            "score": round(float(self.score), 4),
            "missing_groups": list(self.missing_groups),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class PackSelection:
    selected: List[PackScore]
    proposed: List[PackDefinition]
    all_scores: List[PackScore]
    status: str
    reason: str = ""

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "selected": [score.to_dict() for score in self.selected],
            "proposed": [pack.to_dict() for pack in self.proposed],
            "all_scores": [score.to_dict() for score in self.all_scores],
        }


def _require_token(data: Dict[str, Any], key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise ValueError(f"pack definition missing required field: {key}")
    if not _SAFE_TOKEN_RE.match(value):
        raise ValueError(f"pack definition field {key} has unsafe value: {value!r}")
    return value


def _require_text(data: Dict[str, Any], key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise ValueError(f"pack definition missing required field: {key}")
    return value


def ensure_safe_token(value: str, *, field_name: str) -> str:
    token = str(value or "").strip()
    if not token or not _SAFE_TOKEN_RE.match(token):
        raise ValueError(f"unsafe {field_name}: {value!r}")
    return token
