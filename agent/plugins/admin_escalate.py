from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent.plugins.base import ToolPlugin
from modes.admin.plugin_tools import AdminToolError, resolve_workdir, write_escalation
from modes.admin.state_store import AdminStateStore
from modes.sdk.runtime.tooling.spec import ToolSpec

_log = logging.getLogger(__name__)


class AdminEscalateTool(ToolPlugin):
    plugin_id = "admin_escalate"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="admin_escalate",
            description=(
                "Escalate a situation to the human admin. Records an incident in the admin state store "
                "and attaches context. Use when you cannot resolve autonomously within a few steps."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Short description of the problem."},
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "How quickly a human should respond.",
                        "default": "medium",
                    },
                    "server_id": {"type": "string", "description": "Affected server ID, if applicable."},
                    "context": {
                        "type": "object",
                        "description": "Free-form context: last attempted actions, logs tails, etc.",
                    },
                },
                "required": ["reason"],
            },
            risk_level="low",
            parallelizable=False,
            category="admin",
            tags=["admin", "escalate"],
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            workdir = resolve_workdir(ctx)
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        state_store = _resolve_state_store(ctx)
        reason = str(args.get("reason") or "").strip()
        urgency = str(args.get("urgency") or "medium").strip().lower()
        server_id = args.get("server_id")
        context_payload = args.get("context") if isinstance(args.get("context"), dict) else None

        try:
            escalation = write_escalation(
                workdir=workdir,
                state_store=state_store,
                server_id=str(server_id) if server_id else None,
                reason=reason,
                urgency=urgency,
                context=context_payload,
            )
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "incident_id": escalation["incident_id"],
            "urgency": escalation["urgency"],
            "server_id": escalation["server_id"],
            "output": (
                f"Escalated incident={escalation['incident_id']} urgency={escalation['urgency']}"
            ),
        }


def _resolve_state_store(ctx: Dict[str, Any]) -> Optional[AdminStateStore]:
    bot = ctx.get("bot") if isinstance(ctx, dict) else None
    if bot is None:
        return None
    try:
        cfg = getattr(bot, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg is not None else None
        state_path = str(getattr(defaults, "state_path", "") or "") if defaults else ""
        if not state_path:
            return None
        return AdminStateStore(state_path)
    except Exception:
        _log.exception("admin_escalate: failed to resolve state store")
        return None
