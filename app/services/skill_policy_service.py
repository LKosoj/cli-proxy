from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

import yaml

from modes.sdk.json_store import read_json_locked_if_exists, update_json_locked

from app.services.skill_registry_service import SkillManifest
from session import session_runtime_uid


logger = logging.getLogger(__name__)


def _clean_text(value: Any, *, max_len: int = 256) -> str:
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


def _normalize_rules(values: Any) -> tuple[Dict[str, Any], ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    result: list[Dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        result.append(dict(item))
    return tuple(result)


@dataclass(frozen=True)
class SkillPreferences:
    always_use_skills: tuple[str, ...] = ()
    prefer_skills: tuple[str, ...] = ()
    avoid_skills: tuple[str, ...] = ()
    skill_rules: tuple[Dict[str, Any], ...] = ()
    skill_discovery_mode: Optional[str] = None
    skill_install_policy: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "always_use_skills": list(self.always_use_skills),
            "prefer_skills": list(self.prefer_skills),
            "avoid_skills": list(self.avoid_skills),
            "skill_rules": [dict(item) for item in self.skill_rules],
            "skill_discovery_mode": self.skill_discovery_mode,
            "skill_install_policy": self.skill_install_policy,
        }


@dataclass(frozen=True)
class SkillPolicyDecision:
    skill_id: str
    source: str
    allowed: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "source": self.source,
            "allowed": self.allowed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SkillPolicyEvaluation:
    allowed_manifests: Dict[str, SkillManifest]
    rejected: list[SkillPolicyDecision]
    preferences: SkillPreferences


@dataclass(frozen=True)
class SkillInstallApprovalRecord:
    approval_id: str
    status: Literal["pending", "approved", "rejected"]
    skill_id: str
    mode_id: str
    phase: str
    task_hash: str
    task_iteration_key: str
    lockout_key: str
    source: str
    acquisition_source: str
    ref: str
    install_target: str
    requester: Dict[str, Any] = field(default_factory=dict)
    origin_payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    resolved_at: float | None = None
    resolution_reason: str = ""
    resolved_by: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "status": self.status,
            "skill_id": self.skill_id,
            "mode_id": self.mode_id,
            "phase": self.phase,
            "task_hash": self.task_hash,
            "task_iteration_key": self.task_iteration_key,
            "lockout_key": self.lockout_key,
            "source": self.source,
            "acquisition_source": self.acquisition_source,
            "ref": self.ref,
            "install_target": self.install_target,
            "requester": dict(self.requester),
            "origin_payload": dict(self.origin_payload),
            "created_at": float(self.created_at or 0.0),
            "updated_at": float(self.updated_at or 0.0),
            "resolved_at": None if self.resolved_at is None else float(self.resolved_at),
            "resolution_reason": self.resolution_reason,
            "resolved_by": dict(self.resolved_by),
        }


@dataclass(frozen=True)
class SkillInstallApprovalDecision:
    status: Literal["approval_required", "pending_existing", "rejected_lockout"]
    reason: str
    record: SkillInstallApprovalRecord | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "record": self.record.to_dict() if self.record is not None else None,
        }


class SkillPolicyService:
    def __init__(self, config: Any, logger_: Optional[logging.Logger] = None) -> None:
        self.config = config
        self._logger = logger_ or logger

    def load_preferences(self, *, session: Any | None = None) -> SkillPreferences:
        global_payload = self._load_scope_preferences(scope_root=self._global_scope_root())
        project_payload = self._load_scope_preferences(scope_root=self._project_scope_root(session))
        return self._merge_preferences(global_payload, project_payload)

    def allows_source(self, source: str) -> bool:
        token = _clean_text(source, max_len=128)
        allowlisted = {
            _clean_text(item, max_len=128)
            for item in (getattr(getattr(self.config, "defaults", None), "skill_allowlisted_sources", None) or [])
            if _clean_text(item, max_len=128)
        }
        if not allowlisted:
            allowlisted = {
                "local:global-registry",
                "local:project-registry",
                "path:absolute",
                "registry:npx-skills",
                "ref:owner-repo-skill",
            }
        return token in allowlisted

    def validate_manifest(self, manifest: SkillManifest) -> SkillPolicyDecision:
        if self.allows_source(manifest.source):
            return SkillPolicyDecision(
                skill_id=manifest.skill_id,
                source=manifest.source,
                allowed=True,
            )
        return SkillPolicyDecision(
            skill_id=manifest.skill_id,
            source=manifest.source,
            allowed=False,
            reason=f"source_not_allowlisted:{manifest.source}",
        )

    def evaluate_manifests(
        self,
        manifests: Dict[str, SkillManifest],
        *,
        session: Any | None = None,
    ) -> SkillPolicyEvaluation:
        allowed: Dict[str, SkillManifest] = {}
        rejected: list[SkillPolicyDecision] = []
        for skill_id, manifest in sorted(manifests.items()):
            decision = self.validate_manifest(manifest)
            if decision.allowed:
                allowed[skill_id] = manifest
                continue
            rejected.append(decision)
        return SkillPolicyEvaluation(
            allowed_manifests=allowed,
            rejected=rejected,
            preferences=self.load_preferences(session=session),
        )

    def resolve_install_target(self, *, session: Any | None = None) -> str:
        project_root = self.resolve_project_install_target(session=session)
        if project_root:
            return project_root
        return self.resolve_global_install_target()

    def resolve_global_install_target(self) -> str:
        return self._global_scope_root()

    def resolve_project_install_target(self, *, session: Any | None = None) -> str | None:
        return self._project_scope_root(session)

    def evaluate_install_request(
        self,
        *,
        session: Any | None,
        mode_id: str,
        phase: str,
        task_hash: str,
        skill_id: str,
    ) -> SkillInstallApprovalDecision:
        task_iteration_key = self.build_task_iteration_key(
            session=session,
            mode_id=mode_id,
            phase=phase,
            task_hash=task_hash,
        )
        lockout_key = self._build_lockout_key(task_iteration_key=task_iteration_key, skill_id=skill_id)
        ledger = self._read_approval_ledger(session=session)
        rejected = self._record_from_payload(dict(ledger.get("rejected", {}) or {}).get(lockout_key))
        if rejected is not None:
            return SkillInstallApprovalDecision(
                status="rejected_lockout",
                reason="rejected_lockout",
                record=rejected,
            )
        for payload in dict(ledger.get("pending", {}) or {}).values():
            record = self._record_from_payload(payload)
            if record is None or record.lockout_key != lockout_key:
                continue
            return SkillInstallApprovalDecision(
                status="pending_existing",
                reason="pending_existing",
                record=record,
            )
        return SkillInstallApprovalDecision(
            status="approval_required",
            reason="admin_approve",
            record=None,
        )

    def register_pending_install(
        self,
        *,
        session: Any | None,
        mode_id: str,
        phase: str,
        task_hash: str,
        skill_id: str,
        source: str,
        acquisition_source: str,
        ref: str,
        install_target: str,
        requester: Dict[str, Any],
        origin_payload: Dict[str, Any],
    ) -> SkillInstallApprovalRecord | None:
        task_iteration_key = self.build_task_iteration_key(
            session=session,
            mode_id=mode_id,
            phase=phase,
            task_hash=task_hash,
        )
        lockout_key = self._build_lockout_key(task_iteration_key=task_iteration_key, skill_id=skill_id)
        ledger_path = self._approval_ledger_path(session=session)
        if not ledger_path:
            return None
        now_ts = time.time()
        approval_id = self._build_approval_id(lockout_key=lockout_key, created_at=now_ts)
        created_record = SkillInstallApprovalRecord(
            approval_id=approval_id,
            status="pending",
            skill_id=_clean_text(skill_id, max_len=128),
            mode_id=_clean_text(mode_id, max_len=64),
            phase=_clean_text(phase, max_len=64),
            task_hash=_clean_text(task_hash, max_len=128),
            task_iteration_key=task_iteration_key,
            lockout_key=lockout_key,
            source=_clean_text(source, max_len=128),
            acquisition_source=_clean_text(acquisition_source, max_len=128),
            ref=_clean_text(ref, max_len=256),
            install_target=os.path.abspath(str(install_target or "")),
            requester=dict(requester or {}),
            origin_payload=dict(origin_payload or {}),
            created_at=now_ts,
            updated_at=now_ts,
        )
        holder: dict[str, SkillInstallApprovalRecord | None] = {"record": None}

        def _updater(current: Dict[str, Any]) -> Dict[str, Any]:
            normalized = self._normalize_approval_ledger(current)
            rejected = dict(normalized.get("rejected", {}) or {})
            if lockout_key in rejected:
                holder["record"] = self._record_from_payload(rejected[lockout_key])
                return normalized
            pending = dict(normalized.get("pending", {}) or {})
            for payload in pending.values():
                existing = self._record_from_payload(payload)
                if existing is None or existing.lockout_key != lockout_key:
                    continue
                holder["record"] = existing
                return normalized
            pending[created_record.approval_id] = created_record.to_dict()
            normalized["pending"] = pending
            holder["record"] = created_record
            return normalized

        update_json_locked(ledger_path, _updater, default=self._empty_approval_ledger())
        return holder["record"]

    def get_pending_install(
        self,
        *,
        session: Any | None,
        approval_id: str,
    ) -> SkillInstallApprovalRecord | None:
        approval_token = _clean_text(approval_id, max_len=256)
        if not approval_token:
            return None
        ledger = self._read_approval_ledger(session=session)
        return self._record_from_payload(dict(ledger.get("pending", {}) or {}).get(approval_token))

    def list_pending_installs(self, *, session: Any | None) -> list[SkillInstallApprovalRecord]:
        ledger = self._read_approval_ledger(session=session)
        pending = []
        for payload in dict(ledger.get("pending", {}) or {}).values():
            record = self._record_from_payload(payload)
            if record is not None:
                pending.append(record)
        pending.sort(key=lambda item: (item.created_at, item.approval_id))
        return pending

    def accept_pending_install(
        self,
        *,
        session: Any | None,
        approval_id: str,
        resolved_by: Dict[str, Any] | None = None,
        resolution_reason: str = "approved_by_admin",
    ) -> SkillInstallApprovalRecord | None:
        return self._resolve_pending_install(
            session=session,
            approval_id=approval_id,
            resolution_status="approved",
            resolved_by=resolved_by,
            resolution_reason=resolution_reason,
        )

    def reject_pending_install(
        self,
        *,
        session: Any | None,
        approval_id: str,
        resolved_by: Dict[str, Any] | None = None,
        resolution_reason: str = "rejected_by_admin",
    ) -> SkillInstallApprovalRecord | None:
        return self._resolve_pending_install(
            session=session,
            approval_id=approval_id,
            resolution_status="rejected",
            resolved_by=resolved_by,
            resolution_reason=resolution_reason,
        )

    def build_task_iteration_key(
        self,
        *,
        session: Any | None,
        mode_id: str,
        phase: str,
        task_hash: str,
    ) -> str:
        session_uid = _clean_text(session_runtime_uid(session), max_len=256) if session is not None else ""
        if not session_uid:
            session_uid = _clean_text(getattr(session, "id", None), max_len=256) if session is not None else ""
        if not session_uid:
            session_uid = "global"
        material = "|".join(
            [
                session_uid,
                _clean_text(mode_id, max_len=64),
                _clean_text(phase, max_len=64),
                _clean_text(task_hash, max_len=128),
            ]
        )
        return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    def _load_scope_preferences(self, *, scope_root: str | None) -> Dict[str, Any]:
        if not scope_root:
            return {}
        path = os.path.join(scope_root, "preferences.yaml")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle.read())
        except Exception:
            self._logger.exception("skill policy: failed to load preferences path=%s", path)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _merge_preferences(self, global_payload: Dict[str, Any], project_payload: Dict[str, Any]) -> SkillPreferences:
        merged = {
            "always_use_skills": _dedupe_strings(global_payload.get("always_use_skills")),
            "prefer_skills": _dedupe_strings(global_payload.get("prefer_skills")),
            "avoid_skills": _dedupe_strings(global_payload.get("avoid_skills")),
            "skill_rules": _normalize_rules(global_payload.get("skill_rules")),
            "skill_discovery_mode": _clean_text(global_payload.get("skill_discovery_mode"), max_len=64) or None,
            "skill_install_policy": _clean_text(global_payload.get("skill_install_policy"), max_len=64) or None,
        }
        overrides = {
            "always_use_skills": _dedupe_strings(project_payload.get("always_use_skills")),
            "prefer_skills": _dedupe_strings(project_payload.get("prefer_skills")),
            "avoid_skills": _dedupe_strings(project_payload.get("avoid_skills")),
            "skill_rules": _normalize_rules(project_payload.get("skill_rules")),
            "skill_discovery_mode": _clean_text(project_payload.get("skill_discovery_mode"), max_len=64) or None,
            "skill_install_policy": _clean_text(project_payload.get("skill_install_policy"), max_len=64) or None,
        }
        for field_name, value in overrides.items():
            if value not in ((), None):
                merged[field_name] = value
        return SkillPreferences(
            always_use_skills=merged["always_use_skills"],
            prefer_skills=merged["prefer_skills"],
            avoid_skills=merged["avoid_skills"],
            skill_rules=merged["skill_rules"],
            skill_discovery_mode=merged["skill_discovery_mode"],
            skill_install_policy=merged["skill_install_policy"],
        )

    def _global_scope_root(self) -> str:
        defaults = getattr(self.config, "defaults", None)
        workdir = os.path.abspath(str(getattr(defaults, "workdir", "") or os.getcwd()))
        registry_paths = list(getattr(defaults, "skill_registry_paths", None) or [".cli-proxy/skills"])
        return self._resolve_registry_path(workdir, registry_paths[0])

    def _project_scope_root(self, session: Any | None) -> str | None:
        if session is None:
            return None
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
        return self._resolve_registry_path(base_root, registry_paths[0])

    @staticmethod
    def _resolve_registry_path(base_root: str, raw_path: Any) -> str:
        token = str(raw_path or "").strip()
        if not token:
            return os.path.abspath(base_root)
        if os.path.isabs(token):
            return os.path.abspath(token)
        return os.path.abspath(os.path.join(base_root, token))

    def _resolve_pending_install(
        self,
        *,
        session: Any | None,
        approval_id: str,
        resolution_status: Literal["approved", "rejected"],
        resolved_by: Dict[str, Any] | None,
        resolution_reason: str,
    ) -> SkillInstallApprovalRecord | None:
        approval_token = _clean_text(approval_id, max_len=256)
        ledger_path = self._approval_ledger_path(session=session)
        if not approval_token or not ledger_path:
            return None
        holder: dict[str, SkillInstallApprovalRecord | None] = {"record": None}
        now_ts = time.time()

        def _updater(current: Dict[str, Any]) -> Dict[str, Any]:
            normalized = self._normalize_approval_ledger(current)
            pending = dict(normalized.get("pending", {}) or {})
            payload = pending.pop(approval_token, None)
            if not isinstance(payload, dict):
                normalized["pending"] = pending
                return normalized
            payload["status"] = resolution_status
            payload["updated_at"] = now_ts
            payload["resolved_at"] = now_ts
            payload["resolution_reason"] = _clean_text(resolution_reason, max_len=256)
            payload["resolved_by"] = dict(resolved_by or {})
            record = self._record_from_payload(payload)
            holder["record"] = record
            normalized["pending"] = pending
            if resolution_status == "rejected" and record is not None:
                rejected = dict(normalized.get("rejected", {}) or {})
                rejected[record.lockout_key] = record.to_dict()
                normalized["rejected"] = rejected
            return normalized

        update_json_locked(ledger_path, _updater, default=self._empty_approval_ledger())
        return holder["record"]

    def _approval_ledger_path(self, *, session: Any | None) -> str | None:
        root = self.resolve_project_install_target(session=session) if session is not None else self.resolve_global_install_target()
        if not root:
            return None
        return os.path.join(root, ".skill_install_approval_ledger.json")

    def _read_approval_ledger(self, *, session: Any | None) -> Dict[str, Any]:
        ledger_path = self._approval_ledger_path(session=session)
        if not ledger_path:
            return self._empty_approval_ledger()
        payload = read_json_locked_if_exists(ledger_path, default=self._empty_approval_ledger())
        return self._normalize_approval_ledger(payload)

    @staticmethod
    def _empty_approval_ledger() -> Dict[str, Any]:
        return {
            "pending": {},
            "rejected": {},
        }

    def _normalize_approval_ledger(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        pending = dict(payload.get("pending", {}) or {}) if isinstance(payload, dict) else {}
        rejected = dict(payload.get("rejected", {}) or {}) if isinstance(payload, dict) else {}
        return {
            "pending": pending,
            "rejected": rejected,
        }

    @staticmethod
    def _record_from_payload(payload: Any) -> SkillInstallApprovalRecord | None:
        if not isinstance(payload, dict):
            return None
        approval_id = _clean_text(payload.get("approval_id"), max_len=256)
        skill_id = _clean_text(payload.get("skill_id"), max_len=128)
        if not approval_id or not skill_id:
            return None
        return SkillInstallApprovalRecord(
            approval_id=approval_id,
            status=_clean_text(payload.get("status"), max_len=32) or "pending",
            skill_id=skill_id,
            mode_id=_clean_text(payload.get("mode_id"), max_len=64),
            phase=_clean_text(payload.get("phase"), max_len=64),
            task_hash=_clean_text(payload.get("task_hash"), max_len=128),
            task_iteration_key=_clean_text(payload.get("task_iteration_key"), max_len=128),
            lockout_key=_clean_text(payload.get("lockout_key"), max_len=128),
            source=_clean_text(payload.get("source"), max_len=128),
            acquisition_source=_clean_text(payload.get("acquisition_source"), max_len=128),
            ref=_clean_text(payload.get("ref"), max_len=256),
            install_target=os.path.abspath(str(payload.get("install_target") or "")),
            requester=dict(payload.get("requester") or {}) if isinstance(payload.get("requester"), dict) else {},
            origin_payload=dict(payload.get("origin_payload") or {})
            if isinstance(payload.get("origin_payload"), dict)
            else {},
            created_at=float(payload.get("created_at") or 0.0),
            updated_at=float(payload.get("updated_at") or 0.0),
            resolved_at=(
                None
                if payload.get("resolved_at") in (None, "")
                else float(payload.get("resolved_at") or 0.0)
            ),
            resolution_reason=_clean_text(payload.get("resolution_reason"), max_len=256),
            resolved_by=dict(payload.get("resolved_by") or {})
            if isinstance(payload.get("resolved_by"), dict)
            else {},
        )

    @staticmethod
    def _build_lockout_key(*, task_iteration_key: str, skill_id: str) -> str:
        material = "|".join([task_iteration_key, _clean_text(skill_id, max_len=128)])
        return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _build_approval_id(*, lockout_key: str, created_at: float) -> str:
        material = f"{lockout_key}|{created_at:.6f}"
        return "skill-approval:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
