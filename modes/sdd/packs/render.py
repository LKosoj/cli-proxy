from __future__ import annotations

from typing import Any, Dict


def render_pack_manifest_md(manifest: Dict[str, Any]) -> str:
    lines = ["# SDD Artifact Pack Manifest", ""]
    lines.append(f"Status: `{manifest.get('status') or 'unknown'}`")
    reason = str(manifest.get("reason") or "").strip()
    if reason:
        lines.append(f"Reason: `{reason}`")
    lines.append("")
    lines.append("## Selected Packs")
    selected = list(manifest.get("selected") or [])
    if not selected:
        lines.append("")
        lines.append("_No selected packs._")
    for pack in selected:
        lines.append("")
        lines.append(f"### {pack.get('pack_id')}")
        lines.append(f"- Title: {pack.get('title')}")
        lines.append(f"- Lifecycle: `{pack.get('lifecycle')}`")
        lines.append(f"- Score: `{pack.get('score')}`")
        evidence = list(pack.get("evidence") or [])
        if evidence:
            lines.append("- Evidence:")
            for item in evidence:
                lines.append(
                    f"  - `{item.get('path')}` via `{item.get('rule_id')}` "
                    f"({item.get('reason')})"
                )
    proposed = list(manifest.get("proposed") or [])
    if proposed:
        lines.append("")
        lines.append("## Proposed Packs")
        for pack in proposed:
            lines.append("")
            lines.append(f"### {pack.get('pack_id')}")
            lines.append(f"- Title: {pack.get('title')}")
            lines.append("- Status: proposed; requires explicit confirmation before promotion.")
    return "\n".join(lines).rstrip() + "\n"


def render_validation_md(manifest: Dict[str, Any]) -> str:
    lines = ["# Validation Plan", ""]
    lines.append("## Pack-Specific Checks")
    checks: list[tuple[str, str]] = []
    for pack_score in list(manifest.get("selected") or []):
        pack = pack_score.get("pack") if isinstance(pack_score.get("pack"), dict) else {}
        sdd = pack.get("sdd") if isinstance(pack, dict) else {}
        for command in list((sdd or {}).get("validation_commands") or []):
            checks.append((str(pack_score.get("pack_id") or ""), str(command or "")))
    if not checks:
        lines.append("")
        lines.append("_No executable validation command inferred. Add project-specific checks before implementation._")
    else:
        for pack_id, command in checks:
            lines.append(f"- `{pack_id}`: `{command}`")
    lines.append("")
    lines.append("## Manual Checks")
    lines.append("- Confirm generated packs before using proposed pack guidance.")
    lines.append("- Confirm artifact claims against code map evidence.")
    return "\n".join(lines).rstrip() + "\n"
