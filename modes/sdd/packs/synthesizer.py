from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from .schema import PACK_SCHEMA_VERSION, PackDefinition


def synthesize_proposed_pack(*, meaningful_paths: Iterable[str], reason: str = "uncovered_project") -> PackDefinition:
    paths = sorted({str(path or "").strip() for path in meaningful_paths if str(path or "").strip()})[:80]
    digest = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()[:10]
    pack_id = f"proposed-{digest}"
    title = "Generated project pack"
    raw: Dict[str, Any] = {
        "schema_version": PACK_SCHEMA_VERSION,
        "pack_id": pack_id,
        "title": title,
        "lifecycle": "proposed",
        "version": "0.1",
        "source": "proposed",
        "applies_to": {
            "primary_ecosystem": "unknown",
            "languages": [],
            "frameworks": [],
            "targets": [],
            "can_combine_with": ["core-baseline", "architecture", "adr"],
        },
        "detectors": {
            "min_confidence": 0.55,
            "evidence_groups_required": ["project_files"],
            "rules": [
                {
                    "id": "project_files_present",
                    "kind": "any_file_matches",
                    "pattern": "*",
                    "group": "project_files",
                    "weight": 1.0,
                    "reason": "Project has meaningful files but no built-in pack covered it.",
                }
            ],
        },
        "sdd": {
            "spec_guidance": "Treat this as an uncovered project. Keep all technology claims evidence-based.",
            "plan_guidance": "Create a technology-specific plan only after confirming the generated pack.",
            "task_guidance": "Include a task to refine and promote the generated SDD pack before implementation.",
            "artifact_templates": [
                {
                    "id": "generated-pack-notes",
                    "output_path": "generated-pack-notes.md",
                    "title": "Generated Pack Notes",
                }
            ],
            "validation_commands": [],
            "risk_prompts": [
                "The project type was not recognized by built-in packs.",
                "Do not infer build commands without explicit files or user confirmation.",
            ],
        },
        "merge": {
            "priority": 10,
            "override_policy": "additive",
            "conflict_keys": [],
        },
        "provenance": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "evidence_paths": paths,
        },
    }
    return PackDefinition.from_dict(raw, source="proposed")
