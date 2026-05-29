from __future__ import annotations

import json
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.admin.plugin_tools import AdminToolError, build_server_dossier, resolve_workdir
from modes.sdk.runtime.tooling.spec import ToolSpec


class AdminGetDossierTool(ToolPlugin):
    plugin_id = "admin_get_dossier"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="admin_get_dossier",
            description=(
                "Return a structured dossier about a server: baseline profile, persistent facts, "
                "recent notes, open drifts summary, recent drifts, and relevant runbooks. "
                "Call this FIRST when working on a server so you don't rediscover known facts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "Target server ID."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for runbook matching (e.g. ['disk','nginx']).",
                    },
                    "recent_drifts_limit": {
                        "type": "integer",
                        "description": "Max recent drifts to include (default 10).",
                        "default": 10,
                    },
                    "runbook_limit": {
                        "type": "integer",
                        "description": "Max runbooks to include (default 5).",
                        "default": 5,
                    },
                },
                "required": ["server_id"],
            },
            risk_level="low",
            parallelizable=True,
            category="admin",
            tags=["admin", "dossier"],
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            workdir = resolve_workdir(ctx)
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        server_id = args.get("server_id")
        if not server_id:
            return {"success": False, "error": "server_id is required"}

        tags = args.get("tags") or []
        if not isinstance(tags, list):
            return {"success": False, "error": "tags must be a list of strings"}

        try:
            dossier = build_server_dossier(
                workdir=workdir,
                server_id=str(server_id),
                alert_tags=[str(t) for t in tags],
                recent_drifts_limit=int(args.get("recent_drifts_limit") or 10),
                runbook_limit=int(args.get("runbook_limit") or 5),
            )
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "server_id": dossier["server_id"],
            "dossier": dossier,
            "output": _format_output(dossier),
        }


def _format_output(dossier: Dict[str, Any]) -> str:
    lines = [f"=== Server dossier: {dossier.get('server_id')} ==="]
    facts = dossier.get("facts") or {}
    if facts:
        lines.append("Facts:")
        for k, v in facts.items():
            if k.startswith("_"):
                continue
            lines.append(f"  {k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    open_drifts = dossier.get("open_drifts_summary") or {}
    active = {k: v for k, v in open_drifts.items() if v}
    if active:
        lines.append("Open drifts: " + ", ".join(f"{k}={v}" for k, v in active.items()))
    else:
        lines.append("Open drifts: none")
    runbooks = dossier.get("runbooks") or []
    if runbooks:
        lines.append("Matched runbooks:")
        for rb in runbooks:
            lines.append(f"  - {rb.get('id')}: {rb.get('title')}")
    if dossier.get("has_proposed_baseline"):
        lines.append("⚠ baseline.proposed.yaml present (human accept required)")
    return "\n".join(lines)
