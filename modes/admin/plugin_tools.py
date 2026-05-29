from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from .baseline import load_baseline, load_proposed_baseline
from .config_store import AdminConfigStore
from .memory import ServerMemory
from .runbooks import load_runbooks, match_runbooks, summarize_runbooks
from .snapshot_store import AdminSnapshotStore, safe_server_id
from .transports import (
    LocalCommandSpec,
    LocalSubprocessTransport,
    SSHCommandSpec,
    SSHSubprocessTransport,
)

_log = logging.getLogger(__name__)

ALLOWED_TARGETS = ("local", "ssh")
ALLOWED_URGENCIES = ("low", "medium", "high")


class AdminToolError(RuntimeError):
    """Raised when admin plugin tool cannot execute a request."""


@dataclass
class ActionRunResult:
    success: bool
    action_id: str
    server_id: Optional[str]
    target: str
    dry_run: bool
    applied: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    command_preview: str
    timed_out: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "action_id": self.action_id,
            "server_id": self.server_id,
            "target": self.target,
            "dry_run": self.dry_run,
            "applied": self.applied,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "command_preview": self.command_preview,
            "timed_out": self.timed_out,
            "error": self.error,
        }


def resolve_workdir(ctx: Mapping[str, Any]) -> str:
    session = ctx.get("session") if isinstance(ctx, Mapping) else None
    workdir = ""
    if session is not None:
        workdir = str(getattr(session, "workdir", "") or "")
    if not workdir:
        workdir = str(ctx.get("cwd") or "")
    if not workdir:
        raise AdminToolError("admin: session workdir is not available")
    return workdir


def find_allowlisted_action(
    admin_cfg: Mapping[str, Any],
    *,
    action_id: str,
    target_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Находит action в `admin.allowlist.<target>`. Возвращает dict с ключами:
    target ("local"|"ssh"), action_id, argv, timeout_sec, risk_level, + остальные поля.
    """
    clean_id = str(action_id or "").strip()
    if not clean_id:
        raise AdminToolError("action_id is empty")

    allowlist = (admin_cfg.get("allowlist") or {}) if isinstance(admin_cfg, Mapping) else {}
    candidates: List[str] = []
    if target_hint:
        th = str(target_hint).strip().lower()
        if th in ALLOWED_TARGETS:
            candidates = [th]
        else:
            raise AdminToolError(f"invalid target_hint: {target_hint!r}")
    else:
        candidates = list(ALLOWED_TARGETS)

    for target in candidates:
        section = allowlist.get(target) if isinstance(allowlist, Mapping) else None
        if not isinstance(section, Mapping):
            continue
        spec = section.get(clean_id)
        if isinstance(spec, Mapping):
            merged: Dict[str, Any] = {"target": target, "action_id": clean_id}
            merged.update(dict(spec))
            return merged
    raise AdminToolError(f"action {clean_id!r} is not in allowlist (target_hint={target_hint!r})")


async def run_allowlisted_action(
    *,
    workdir: str,
    action_id: str,
    server_id: Optional[str] = None,
    target_hint: Optional[str] = None,
    dry_run: bool = True,
    local_transport: Optional[LocalSubprocessTransport] = None,
    ssh_transport: Optional[SSHSubprocessTransport] = None,
) -> ActionRunResult:
    """
    MVP-executor для plugin-tool `admin.execute_action`.

    Не заменяет полного `AdminExecutor` — используется только из agent-плагина.
    Поддерживает dry_run (preview без запуска). Записывает аудит в notes.md сервера.
    """
    config_store = AdminConfigStore(workdir)
    cfg_payload = config_store.load_effective_config()
    admin_cfg = cfg_payload.get("admin") if isinstance(cfg_payload, Mapping) else {}
    spec = find_allowlisted_action(admin_cfg or {}, action_id=action_id, target_hint=target_hint)

    target = str(spec.get("target") or "").strip().lower()
    if target not in ALLOWED_TARGETS:
        raise AdminToolError(f"resolved target invalid: {target!r}")

    argv = spec.get("argv") or []
    if not isinstance(argv, (list, tuple)) or not argv:
        raise AdminToolError(f"action {action_id}: empty or invalid argv")
    argv_tuple = tuple(str(item) for item in argv)
    timeout_sec = float(spec.get("timeout_sec") or 30.0)
    preview = " ".join(argv_tuple)

    sid = safe_server_id(server_id) if server_id else None

    if dry_run:
        result = ActionRunResult(
            success=True,
            action_id=str(action_id),
            server_id=sid,
            target=target,
            dry_run=True,
            applied=False,
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=0,
            command_preview=preview,
        )
        _audit_note(workdir, sid, f"DRY-RUN action={action_id} target={target} argv={preview}")
        return result

    started_at = time.perf_counter()
    if target == "local":
        transport = local_transport or LocalSubprocessTransport()
        local_spec = LocalCommandSpec(
            action_id=f"plugin:{action_id}",
            argv=argv_tuple,
            timeout_sec=timeout_sec,
        )
        try:
            res = await transport.run(local_spec)
        except Exception as exc:
            err = str(exc)
            _audit_note(workdir, sid, f"ERROR action={action_id} target=local error={err}")
            return ActionRunResult(
                success=False, action_id=str(action_id), server_id=sid,
                target=target, dry_run=False, applied=False,
                exit_code=None, stdout="", stderr="", duration_ms=0,
                command_preview=preview, error=err,
            )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        success = (res.returncode == 0) and not res.timed_out
        _audit_note(
            workdir, sid,
            f"EXECUTED action={action_id} target=local rc={res.returncode} "
            f"timed_out={res.timed_out} duration_ms={duration_ms}",
        )
        return ActionRunResult(
            success=success, action_id=str(action_id), server_id=sid,
            target=target, dry_run=False, applied=success,
            exit_code=int(res.returncode), stdout=res.stdout or "",
            stderr=res.stderr or "", duration_ms=duration_ms,
            command_preview=preview, timed_out=bool(res.timed_out),
        )

    # target == "ssh"
    transport = ssh_transport or SSHSubprocessTransport()
    host = str(spec.get("host") or "").strip()
    key_path = str(spec.get("key_path") or "").strip()
    if not host or not key_path:
        raise AdminToolError(f"ssh action {action_id} requires host and key_path")
    ssh_spec = SSHCommandSpec(
        action_id=f"plugin:{action_id}",
        host=host,
        argv=argv_tuple,
        key_path=key_path,
        user=str(spec.get("user") or "").strip() or None,
        port=int(spec.get("port") or 22),
        timeout_sec=timeout_sec,
        options=tuple(str(opt) for opt in (spec.get("options") or ())),
    )
    try:
        res = await transport.run(ssh_spec)
    except Exception as exc:
        err = str(exc)
        _audit_note(workdir, sid, f"ERROR action={action_id} target=ssh host={host} error={err}")
        return ActionRunResult(
            success=False, action_id=str(action_id), server_id=sid,
            target=target, dry_run=False, applied=False,
            exit_code=None, stdout="", stderr="", duration_ms=0,
            command_preview=preview, error=err,
        )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    success = (res.returncode == 0) and not res.timed_out
    _audit_note(
        workdir, sid,
        f"EXECUTED action={action_id} target=ssh host={host} rc={res.returncode} "
        f"timed_out={res.timed_out} duration_ms={duration_ms}",
    )
    return ActionRunResult(
        success=success, action_id=str(action_id), server_id=sid,
        target=target, dry_run=False, applied=success,
        exit_code=int(res.returncode), stdout=res.stdout or "",
        stderr=res.stderr or "", duration_ms=duration_ms,
        command_preview=preview, timed_out=bool(res.timed_out),
    )


def _audit_note(workdir: str, server_id: Optional[str], text: str) -> None:
    if not server_id:
        return
    try:
        ServerMemory(workdir, server_id).append_note(text, source="executor")
    except Exception:
        _log.exception("admin plugin: failed to write audit note server=%s", server_id)


# --- escalate ---

def write_escalation(
    *,
    workdir: str,
    state_store: Any,
    server_id: Optional[str],
    reason: str,
    urgency: str,
    context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    reason_clean = str(reason or "").strip()
    if not reason_clean:
        raise AdminToolError("escalate: reason is empty")
    urg = str(urgency or "").strip().lower() or "medium"
    if urg not in ALLOWED_URGENCIES:
        raise AdminToolError(f"escalate: invalid urgency {urgency!r}")

    sid = safe_server_id(server_id) if server_id else None
    incident_id = f"esc-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    payload = {
        "source": "agent_plugin",
        "reason": reason_clean,
        "urgency": urg,
        "server_id": sid,
        "context": dict(context or {}),
    }
    if state_store is not None:
        try:
            state_store.record_incident(incident_id=incident_id, payload=payload)
        except Exception:
            _log.exception("admin plugin: failed to record incident %s", incident_id)

    if sid:
        try:
            ServerMemory(workdir, sid).append_note(
                f"ESCALATION urgency={urg} reason={reason_clean}",
                source="agent",
                tags=["escalation", urg],
            )
        except Exception:
            _log.exception("admin plugin: failed to note escalation server=%s", sid)

    return {
        "incident_id": incident_id,
        "server_id": sid,
        "reason": reason_clean,
        "urgency": urg,
    }


# --- dossier ---

def build_server_dossier(
    *,
    workdir: str,
    server_id: str,
    alert_tags: Optional[List[str]] = None,
    recent_drifts_limit: int = 10,
    runbook_limit: int = 5,
) -> Dict[str, Any]:
    sid = safe_server_id(server_id)
    baseline = load_baseline(workdir, sid) or {}
    proposed = load_proposed_baseline(workdir, sid)
    memory = ServerMemory(workdir, sid)
    facts = memory.get_facts()
    notes_tail = _tail_entries(memory, n=10)

    store = AdminSnapshotStore.for_server(workdir, sid)
    drifts = store.list_drifts(limit=int(recent_drifts_limit or 10), include_acknowledged=False)
    drift_summary = store.drift_stats()

    runbooks = load_runbooks(workdir, server_ids=[sid])
    matched = match_runbooks(runbooks, server_id=sid, tags=alert_tags or [])
    runbook_summary = summarize_runbooks(matched, limit=int(runbook_limit or 5))

    return {
        "server_id": sid,
        "baseline": baseline,
        "has_proposed_baseline": proposed is not None,
        "facts": facts,
        "recent_notes": notes_tail,
        "open_drifts_summary": drift_summary,
        "recent_drifts": drifts,
        "runbooks": runbook_summary,
    }


def _tail_entries(memory: ServerMemory, *, n: int) -> List[Dict[str, Any]]:
    entries = list(memory.iter_note_entries())
    tail = entries[-int(n):] if len(entries) > int(n) else entries
    return [{"ts": e.ts, "source": e.source, "text": e.text} for e in tail]


__all__ = [
    "ActionRunResult",
    "AdminToolError",
    "ALLOWED_TARGETS",
    "ALLOWED_URGENCIES",
    "build_server_dossier",
    "find_allowlisted_action",
    "resolve_workdir",
    "run_allowlisted_action",
    "write_escalation",
]
