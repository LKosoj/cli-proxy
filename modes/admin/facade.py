from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .autonomy_loop import (
    AUTONOMY_GLOBAL_SERVER_ID,
    AdminAutonomyLoop,
    META_ACTIONS_TOTAL,
    META_ACTION_FAILURES_TOTAL,
    META_BASELINES_AUTO_ACCEPTED_TOTAL,
    META_DENIED_TOTAL,
    META_ESCALATIONS_TOTAL,
    META_IGNORED_TOTAL,
    META_LAST_TICK_TS,
    META_TICK_COUNT,
)
from .autonomy_policy import AutonomyPolicy, load_autonomy_policy
from .baseline import (
    AdminBaselineScanner,
    ServerSpec,
    accept_proposed_baseline,
    baseline_path,
    discard_proposed_baseline,
    load_baseline,
    load_proposed_baseline,
    prev_baseline_path,
    proposed_baseline_path,
)
from .config_store import AdminConfigStore
from .memory import NoteEntry, ServerMemory, ServerMemoryError
from .plugin_tools import build_server_dossier
from .prereqs import (
    PREREQS_CHECK_ID,
    PrereqsReport,
    evaluate_prereqs,
    generate_bootstrap_script,
)
from .reconciliation import AdminReconciler, ServerReconcileReport
from .runbook_builder import (
    BuildSpec,
    RunbookBuilderError,
    ScriptInput,
    build_runbook_from_scripts,
)
from .runbook_promoter import (
    PromoteResult,
    promote_runbook,
)
from .runbook_validator import ValidationReport, validate_runbook
from .runbooks import (
    Runbook,
    load_runbooks,
    match_runbooks,
    summarize_runbooks,
)
from .script_runner import ScriptRunResult, run_runbook_step
from .script_sources import (
    ScriptFile,
    load_whitelist,
    read_script_text,
    scan_scripts_directory,
)
from .snapshot_store import (
    AdminSnapshotStore,
    DEFAULT_RETENTION_DAYS,
    SEVERITY_ALARM,
    SEVERITY_WARN,
    admin_root,
    safe_server_id,
    server_dir,
)

_log = logging.getLogger(__name__)


@dataclass
class ServerSummary:
    server_id: str
    label: str
    transport: str
    host: Optional[str]
    tags: List[str]
    baseline_present: bool
    has_proposed_baseline: bool
    last_scan_ts: Optional[int]  # baseline.scanned_at — когда принят текущий эталон
    last_snapshot_ts: Optional[int] = None  # последний фактический scan (из snapshot-store)
    open_drifts: Dict[str, int] = field(default_factory=dict)
    memory_entries: int = 0

    def status(self) -> str:
        if self.open_drifts.get(SEVERITY_ALARM, 0) > 0:
            return "alarm"
        if self.has_proposed_baseline:
            return "proposed_baseline"
        if self.open_drifts.get(SEVERITY_WARN, 0) > 0:
            return "warn"
        if not self.baseline_present:
            return "no_baseline"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server_id": self.server_id,
            "label": self.label,
            "transport": self.transport,
            "host": self.host,
            "tags": list(self.tags),
            "baseline_present": self.baseline_present,
            "has_proposed_baseline": self.has_proposed_baseline,
            "last_scan_ts": self.last_scan_ts,
            "last_snapshot_ts": self.last_snapshot_ts,
            "open_drifts": dict(self.open_drifts),
            "memory_entries": self.memory_entries,
            "status": self.status(),
        }


def parse_server_specs(admin_cfg: Mapping[str, Any]) -> List[ServerSpec]:
    servers = []
    monitor = admin_cfg.get("monitor") if isinstance(admin_cfg, Mapping) else {}
    raw_list = []
    if isinstance(monitor, Mapping):
        raw = monitor.get("servers") or []
        if isinstance(raw, list):
            raw_list = raw
    # альтернативный ключ admin.servers
    if not raw_list:
        maybe = admin_cfg.get("servers") if isinstance(admin_cfg, Mapping) else None
        if isinstance(maybe, list):
            raw_list = maybe
    for item in raw_list:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("server_id") or item.get("id") or item.get("name") or "").strip()
        if not sid:
            continue
        try:
            safe_sid = safe_server_id(sid)
        except Exception:
            _log.warning("skip server with invalid id: %r", sid)
            continue
        transport = str(item.get("transport") or "local").strip().lower()
        spec = ServerSpec(
            server_id=safe_sid,
            transport=transport if transport in ("local", "ssh") else "local",
            host=str(item.get("host") or "").strip() or None,
            user=str(item.get("user") or "").strip() or None,
            port=int(item.get("port") or 22),
            key_path=str(item.get("key_path") or "").strip() or None,
            password_env=str(item.get("password_env") or "").strip() or None,
            ssh_options=tuple(str(o) for o in (item.get("ssh_options") or ())),
            label=str(item.get("label") or "").strip() or None,
            tags=[str(t) for t in (item.get("tags") or []) if str(t or "").strip()],
        )
        servers.append(spec)
    return servers


class AdminAutonomyService:
    """
    Единая точка интеграции для UI (Telegram/MiniApp/Desktop) поверх новых компонентов:
      baseline, memory, drift, snapshots, runbooks, reconciler.
    Stateless — создаётся для каждого вызова.
    """

    def __init__(
        self,
        workdir: str,
        *,
        reconciler_factory: Optional[Callable[[], AdminReconciler]] = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.workdir = str(workdir or "")
        if not self.workdir:
            raise ValueError("workdir is required")
        self._retention_days = int(retention_days)
        self._reconciler_factory = reconciler_factory

    # --- servers ---

    def list_server_specs(self) -> List[ServerSpec]:
        try:
            cfg = AdminConfigStore(self.workdir).load_effective_config()
        except Exception:
            _log.exception("facade: failed to load admin config")
            return []
        admin_cfg = cfg.get("admin") if isinstance(cfg, Mapping) else {}
        return parse_server_specs(admin_cfg or {})

    def list_servers(self) -> List[ServerSummary]:
        out: List[ServerSummary] = []
        for spec in self.list_server_specs():
            out.append(self._summarize_server(spec))
        return out

    def get_server_summary(self, server_id: str) -> Optional[ServerSummary]:
        sid = safe_server_id(server_id)
        for spec in self.list_server_specs():
            if spec.server_id == sid:
                return self._summarize_server(spec)
        # сервер может существовать на диске, но не в config (осиротевший)
        if server_dir(self.workdir, sid).is_dir():
            return self._summarize_server(ServerSpec(server_id=sid, transport="local"))
        return None

    def _summarize_server(self, spec: ServerSpec) -> ServerSummary:
        sid = spec.server_id
        baseline = load_baseline(self.workdir, sid)
        proposed = load_proposed_baseline(self.workdir, sid)
        store = AdminSnapshotStore.for_server(self.workdir, sid, retention_days=self._retention_days)
        drifts = store.drift_stats()
        last_scan = None
        if baseline and isinstance(baseline.get("scanned_at"), str):
            last_scan = baseline.get("scanned_at")
        last_snapshot_ts: Optional[int] = None
        try:
            last_snapshot_ts = store.last_snapshot_ts()
        except Exception:
            _log.exception("facade: last_snapshot_ts failed for %s", sid)
        mem_stats = {"entries": 0}
        try:
            mem_stats = ServerMemory(self.workdir, sid).notes_stats()
        except Exception:
            _log.exception("facade: memory stats failed for %s", sid)
        return ServerSummary(
            server_id=sid,
            label=spec.label or sid,
            transport=spec.transport,
            host=spec.host,
            tags=list(spec.tags or []),
            baseline_present=baseline is not None,
            has_proposed_baseline=proposed is not None,
            last_scan_ts=last_scan,
            last_snapshot_ts=last_snapshot_ts,
            open_drifts=drifts,
            memory_entries=int(mem_stats.get("entries") or 0),
        )

    # --- baseline ---

    def get_baseline(self, server_id: str) -> Dict[str, Any]:
        sid = safe_server_id(server_id)
        baseline = load_baseline(self.workdir, sid) or {}
        proposed = load_proposed_baseline(self.workdir, sid)
        prev_exists = prev_baseline_path(self.workdir, sid).is_file()
        return {
            "server_id": sid,
            "baseline": baseline,
            "baseline_path": str(baseline_path(self.workdir, sid)),
            "proposed": proposed,
            "proposed_path": str(proposed_baseline_path(self.workdir, sid)),
            "has_proposed": proposed is not None,
            "has_prev": prev_exists,
        }

    def accept_baseline(self, server_id: str) -> Dict[str, Any]:
        sid = safe_server_id(server_id)
        return accept_proposed_baseline(self.workdir, sid)

    def discard_baseline_proposal(self, server_id: str) -> bool:
        sid = safe_server_id(server_id)
        return discard_proposed_baseline(self.workdir, sid)

    # --- scan / reconcile ---

    async def rescan_server(self, server_id: str) -> ServerReconcileReport:
        sid = safe_server_id(server_id)
        spec = self._require_server_spec(sid)
        reconciler = self._reconciler()
        return await reconciler.reconcile_server(spec)

    async def rescan_all(self) -> List[ServerReconcileReport]:
        specs = self.list_server_specs()
        reconciler = self._reconciler()
        tick = await reconciler.tick(specs)
        return tick.servers

    def run_daily_maintenance(self) -> Dict[str, Any]:
        specs = self.list_server_specs()
        reconciler = self._reconciler()
        report = reconciler.daily_maintenance([s.server_id for s in specs])
        return {"ran_at": report.ran_at, "servers": dict(report.servers)}

    # --- autonomy ---

    def load_autonomy_policy(self) -> AutonomyPolicy:
        try:
            cfg = AdminConfigStore(self.workdir).load_effective_config()
        except Exception:
            _log.exception("facade: failed to load admin config for autonomy policy")
            cfg = {}
        admin_cfg = cfg.get("admin") if isinstance(cfg, Mapping) else {}
        return load_autonomy_policy(admin_cfg or {})

    def build_autonomy_loop(
        self,
        *,
        policy: Optional[AutonomyPolicy] = None,
        decision_maker: Any = None,
        state_store: Any = None,
        action_runner: Any = None,
    ) -> AdminAutonomyLoop:
        p = policy if policy is not None else self.load_autonomy_policy()
        return AdminAutonomyLoop(
            self.workdir,
            p,
            decision_maker=decision_maker,
            state_store=state_store,
            action_runner=action_runner,
        )

    async def run_autonomy_tick(
        self,
        *,
        policy: Optional[AutonomyPolicy] = None,
        decision_maker: Any = None,
        state_store: Any = None,
        action_runner: Any = None,
    ) -> Dict[str, Any]:
        """
        Один тик автономии:
          1. reconciler.tick по всем серверам
          2. для каждого сервера загрузить свежие drifts и прогнать через autonomy loop
          3. попытаться auto-accept baseline если всё стабильно
        """
        specs = self.list_server_specs()
        reconciler = self._reconciler()
        tick = await reconciler.tick(specs)

        loop_policy = policy if policy is not None else self.load_autonomy_policy()
        if not loop_policy.enabled:
            # Scan + baseline выполнены, но autonomy-loop отключён политикой.
            # last_tick_ts отражает «последний раз мы крутили admin» — его обновляем,
            # tick_count — нет (counts только реальные прогоны loop).
            try:
                AdminSnapshotStore.for_server(
                    self.workdir, AUTONOMY_GLOBAL_SERVER_ID, retention_days=self._retention_days,
                ).set_meta(META_LAST_TICK_TS, str(int(tick.ran_at)))
            except Exception:
                _log.exception("facade: failed to record last_tick_ts (policy disabled)")
            return {
                "ran_at": tick.ran_at,
                "policy_enabled": False,
                "tick": tick.summary(),
                "server_reports": [_server_report_to_dict(r) for r in tick.servers],
                "decisions": [],
                "baselines_auto_accepted": [],
            }

        loop = self.build_autonomy_loop(
            policy=loop_policy,
            decision_maker=decision_maker,
            state_store=state_store,
            action_runner=action_runner,
        )
        loop.record_tick_start()

        decisions: List[Dict[str, Any]] = []
        auto_accepted: List[str] = []
        for report in tick.servers:
            if not report.ok:
                continue
            try:
                drifts = self.list_drifts(
                    report.server_id,
                    limit=max(1, report.drifts_written or 1),
                    open_only=True,
                )
            except Exception:
                _log.exception("facade: failed to fetch drifts sid=%s", report.server_id)
                drifts = []
            if drifts:
                try:
                    decisions.extend(
                        await loop.process_server_drifts(report.server_id, drifts)
                    )
                except Exception:
                    _log.exception("facade: autonomy loop failed sid=%s", report.server_id)
            try:
                accepted_sid = loop.maybe_auto_accept_baseline(
                    report.server_id, drifts_this_tick=int(report.drifts_written or 0),
                )
                if accepted_sid:
                    auto_accepted.append(accepted_sid)
            except Exception:
                _log.exception("facade: baseline auto-accept failed sid=%s", report.server_id)

        return {
            "ran_at": tick.ran_at,
            "policy_enabled": True,
            "tick": tick.summary(),
            "server_reports": [_server_report_to_dict(r) for r in tick.servers],
            "decisions": decisions,
            "baselines_auto_accepted": auto_accepted,
        }

    def autonomy_status(self) -> Dict[str, Any]:
        """
        Глобальный статус автономии: политика + кумулятивные счётчики.
        Читает meta из синтетического сервера AUTONOMY_GLOBAL_SERVER_ID.
        """
        policy = self.load_autonomy_policy()
        store = AdminSnapshotStore.for_server(
            self.workdir, AUTONOMY_GLOBAL_SERVER_ID, retention_days=self._retention_days,
        )

        def _int(key: str) -> int:
            raw = store.get_meta(key)
            try:
                return int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                return 0

        last_tick = _int(META_LAST_TICK_TS)
        return {
            "policy": {
                "enabled": policy.enabled,
                "auto_apply_severities": list(policy.auto_apply_severities),
                "auto_exec_actions": list(policy.auto_exec_actions),
                "max_actions_per_hour": policy.max_actions_per_hour,
                "cooldown_sec": policy.cooldown_sec,
                "baseline_auto_accept_enabled": policy.baseline_auto_accept_enabled,
                "baseline_auto_accept_after_stable_scans": policy.baseline_auto_accept_after_stable_scans,
                "require_dry_run_success": policy.require_dry_run_success,
            },
            "counters": {
                "tick_count": _int(META_TICK_COUNT),
                "actions_executed_total": _int(META_ACTIONS_TOTAL),
                "action_failures_total": _int(META_ACTION_FAILURES_TOTAL),
                "escalations_total": _int(META_ESCALATIONS_TOTAL),
                "ignored_total": _int(META_IGNORED_TOTAL),
                "denied_by_policy_total": _int(META_DENIED_TOTAL),
                "baselines_auto_accepted_total": _int(META_BASELINES_AUTO_ACCEPTED_TOTAL),
                "last_tick_ts": last_tick,
                "last_tick_age_sec": (int(time.time()) - last_tick) if last_tick else None,
            },
        }

    def _reconciler(self) -> AdminReconciler:
        if self._reconciler_factory is not None:
            return self._reconciler_factory()
        return AdminReconciler(
            self.workdir,
            scanner=AdminBaselineScanner(secrets_workdir=self.workdir),
            retention_days=self._retention_days,
        )

    def _require_server_spec(self, server_id: str) -> ServerSpec:
        for spec in self.list_server_specs():
            if spec.server_id == server_id:
                return spec
        raise ValueError(f"server {server_id!r} is not configured")

    # --- memory ---

    def get_memory(self, server_id: str) -> Dict[str, Any]:
        sid = safe_server_id(server_id)
        memory = ServerMemory(self.workdir, sid)
        return {
            "server_id": sid,
            "facts": memory.get_facts(),
            "notes_text": memory.get_notes(),
            "stats": memory.notes_stats(),
            "facts_path": str(memory.facts_file),
            "notes_path": str(memory.notes_file),
        }

    def update_memory_fact(
        self, server_id: str, *, key: str, value: Any, by: Optional[str] = None,
    ) -> Dict[str, Any]:
        sid = safe_server_id(server_id)
        try:
            return ServerMemory(self.workdir, sid).update_fact(key, value, by=by)
        except ServerMemoryError as exc:
            raise ValueError(str(exc))

    def delete_memory_fact(self, server_id: str, key: str) -> bool:
        sid = safe_server_id(server_id)
        return ServerMemory(self.workdir, sid).delete_fact(key)

    def append_memory_note(
        self,
        server_id: str,
        text: str,
        *,
        source: str = "manual",
        tags: Optional[List[str]] = None,
    ) -> NoteEntry:
        sid = safe_server_id(server_id)
        return ServerMemory(self.workdir, sid).append_note(text, source=source, tags=tags)

    def compact_memory(self, server_id: str, *, force: bool = False) -> Dict[str, Any]:
        sid = safe_server_id(server_id)
        return ServerMemory(self.workdir, sid).compact_notes(force=force)

    # --- drifts / snapshots ---

    def list_drifts(
        self,
        server_id: str,
        *,
        limit: int = 50,
        severity_min: Optional[str] = None,
        open_only: bool = True,
    ) -> List[Dict[str, Any]]:
        sid = safe_server_id(server_id)
        store = AdminSnapshotStore.for_server(self.workdir, sid, retention_days=self._retention_days)
        return store.list_drifts(
            limit=int(limit),
            severity_min=severity_min,
            include_acknowledged=not open_only,
        )

    def ack_drift(self, server_id: str, drift_id: int, *, by: Optional[str] = None) -> bool:
        sid = safe_server_id(server_id)
        store = AdminSnapshotStore.for_server(self.workdir, sid)
        return store.ack_drift(int(drift_id), by=by)

    def get_snapshots(
        self,
        server_id: str,
        check_id: str,
        *,
        limit: int = 100,
        since_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        sid = safe_server_id(server_id)
        store = AdminSnapshotStore.for_server(self.workdir, sid)
        return store.snapshots_in_window(check_id, since_ts=since_ts, limit=int(limit))

    def list_snapshot_checks(self, server_id: str) -> List[str]:
        sid = safe_server_id(server_id)
        return AdminSnapshotStore.for_server(self.workdir, sid).all_check_ids()

    # --- runbooks ---

    def list_runbooks(
        self,
        *,
        server_id: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> List[Runbook]:
        sid = safe_server_id(server_id) if server_id else None
        all_rbs = load_runbooks(self.workdir, server_ids=[sid] if sid else None)
        if sid is None and not tags:
            return all_rbs
        return match_runbooks(all_rbs, server_id=sid, tags=list(tags or []))

    def list_runbook_summary(
        self,
        *,
        server_id: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        return summarize_runbooks(self.list_runbooks(server_id=server_id, tags=tags), limit=int(limit))

    def get_runbook(self, runbook_id: str, *, server_id: Optional[str] = None) -> Optional[Runbook]:
        for rb in self.list_runbooks(server_id=server_id):
            if rb.id == runbook_id:
                return rb
        return None

    # --- dossier ---

    def get_dossier(
        self,
        server_id: str,
        *,
        tags: Optional[List[str]] = None,
        recent_drifts_limit: int = 10,
        runbook_limit: int = 5,
    ) -> Dict[str, Any]:
        return build_server_dossier(
            workdir=self.workdir,
            server_id=server_id,
            alert_tags=list(tags or []),
            recent_drifts_limit=recent_drifts_limit,
            runbook_limit=runbook_limit,
        )

    # --- script sources / builder / validator / promoter / runner ---

    def _admin_config(self) -> Mapping[str, Any]:
        try:
            cfg = AdminConfigStore(self.workdir).load_effective_config()
        except Exception:
            _log.exception("facade: failed to load admin config")
            cfg = {}
        admin_cfg = cfg.get("admin") if isinstance(cfg, Mapping) else {}
        return admin_cfg or {}

    def scan_script_sources(self, directory: str) -> List[ScriptFile]:
        """Сканирует каталог под admin.runbook_sources, возвращает список .sh/.bash файлов."""
        admin_cfg = self._admin_config()
        whitelist = load_whitelist(admin_cfg)
        return scan_scripts_directory(directory, whitelist=whitelist)

    def read_script_from_source(self, path: str) -> str:
        admin_cfg = self._admin_config()
        whitelist = load_whitelist(admin_cfg)
        return read_script_text(path, whitelist=whitelist)

    def create_runbook_from_scripts(
        self,
        *,
        title: str,
        dev_server_id: str,
        scripts: Iterable[Mapping[str, Any]],
        rb_id: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        triggers: Optional[Iterable[str]] = None,
        description: str = "",
        force: bool = False,
    ) -> Runbook:
        """
        Собирает runbook из спецификации.

        Каждый элемент `scripts` — маппинг с обязательным `source_path` и опциональными
        `name` / `target_hint`. Body читается сервером через admin.runbook_sources
        whitelist (clients НЕ МОГУТ передавать body inline).
        """
        admin_cfg = self._admin_config()
        whitelist = load_whitelist(admin_cfg)
        script_inputs: List[ScriptInput] = []
        for raw in scripts:
            if not isinstance(raw, Mapping):
                raise RunbookBuilderError("each script entry must be a mapping")
            source_path = str(raw.get("source_path") or "").strip()
            if not source_path:
                raise RunbookBuilderError("script entry requires source_path")
            body = read_script_text(source_path, whitelist=whitelist)
            source_name = Path(source_path).name
            name = str(raw.get("name") or source_name).strip()
            script_inputs.append(
                ScriptInput(
                    name=name,
                    body=body,
                    target_hint=str(raw.get("target_hint") or "local").strip().lower(),
                ),
            )
        spec = BuildSpec(
            title=title,
            dev_server_id=dev_server_id,
            scripts=script_inputs,
            rb_id=rb_id,
            tags=list(tags or []),
            triggers=list(triggers or []),
            description=description,
        )
        return build_runbook_from_scripts(self.workdir, spec, force=force)

    # --- prereqs ---

    def check_server_prereqs(self, server_id: str) -> PrereqsReport:
        """
        Вычисляет PrereqsReport по самым свежим наблюдениям сервера.

        Источники (по приоритету):
          1. snapshot-store: latest_snapshot('admin.prereqs') / latest_snapshot('os.os_release')
             — отражает последний scan независимо от того, был ли baseline accepted.
          2. accepted baseline — fallback, если snapshots ещё не записаны.
        """
        sid = safe_server_id(server_id)

        store = AdminSnapshotStore.for_server(
            self.workdir, sid, retention_days=self._retention_days,
        )

        def _latest(check_id: str) -> Any:
            try:
                snap = store.latest_snapshot(check_id)
            except Exception:
                _log.exception("facade: latest_snapshot failed sid=%s check=%s", sid, check_id)
                return None
            return snap.get("value") if snap else None

        presence_raw = _latest(PREREQS_CHECK_ID)
        os_release = _latest("os.os_release")

        if presence_raw is None or os_release is None:
            baseline = load_baseline(self.workdir, sid) or {}
            checks = baseline.get("checks") if isinstance(baseline, Mapping) else None
            if presence_raw is None and isinstance(checks, Mapping):
                presence_raw = checks.get(PREREQS_CHECK_ID)
            if os_release is None and isinstance(checks, Mapping):
                os_release = checks.get("os.os_release")

        presence: Dict[str, bool] = {}
        if isinstance(presence_raw, Mapping):
            presence = {str(k): bool(v) for k, v in presence_raw.items()}

        distro_id: Optional[str] = None
        id_like: Optional[str] = None
        if isinstance(os_release, Mapping):
            raw_id = os_release.get("ID")
            raw_like = os_release.get("ID_LIKE")
            distro_id = str(raw_id).strip() if raw_id else None
            id_like = str(raw_like).strip() if raw_like else None

        return evaluate_prereqs(presence, distro_id=distro_id, id_like=id_like)

    def generate_bootstrap_runbook(
        self,
        server_id: str,
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Собирает bootstrap-runbook для установки недостающих admin-prereqs на сервере.
        Runbook создаётся с confidence=0.0 (manual-only), один шаг — сгенерированный
        shell-скрипт. Возвращает dict с PrereqsReport + созданный Runbook (или None,
        если ставить нечего).
        """
        sid = safe_server_id(server_id)
        report = self.check_server_prereqs(sid)
        missing_any = bool(report.required_missing or report.recommended_missing)
        if not missing_any:
            return {
                "server_id": sid,
                "report": report.to_dict(),
                "runbook": None,
                "reason": "no_missing_prereqs",
            }
        if not report.installable:
            # distro неизвестен ⇒ авто-установка невозможна.
            return {
                "server_id": sid,
                "report": report.to_dict(),
                "runbook": None,
                "reason": "unknown_pkg_manager",
            }

        script_body = generate_bootstrap_script(report)
        rb_id = f"rb-admin-bootstrap-{sid}"
        title = f"Admin prereqs bootstrap for {sid}"
        description = (
            f"Install missing admin prereqs on {sid} "
            f"(pkg_manager={report.pkg_manager or 'unknown'}). "
            "Manual-only: confidence=0.0."
        )
        spec = BuildSpec(
            title=title,
            dev_server_id=sid,
            scripts=[ScriptInput(name="bootstrap.sh", body=script_body, target_hint="local")],
            rb_id=rb_id,
            tags=["admin", "bootstrap", "prereqs"],
            triggers=[],
            description=description,
        )
        runbook = build_runbook_from_scripts(self.workdir, spec, force=force)

        # Audit-note с контекстом prereqs-сверки: иначе RUNBOOK-BUILT без
        # distro/pm/missing не объясняет оператору, зачем существует bootstrap.
        try:
            ServerMemory(self.workdir, sid).append_note(
                (
                    f"PREREQS-BOOTSTRAP rb={runbook.id} "
                    f"distro={report.distro_id or '-'} "
                    f"pm={report.pkg_manager or '-'} "
                    f"missing_required={list(report.required_missing) or '-'} "
                    f"missing_recommended={list(report.recommended_missing) or '-'} "
                    f"pkgs={list(report.installable)}"
                ),
                source="prereqs",
                tags=["prereqs", "bootstrap"],
            )
        except Exception:
            _log.exception("facade: prereqs audit note failed sid=%s", sid)

        return {
            "server_id": sid,
            "report": report.to_dict(),
            "runbook": {
                "id": runbook.id,
                "title": runbook.title,
                "path": str(runbook.path) if getattr(runbook, "path", None) else None,
                "servers": list(runbook.servers),
                "tags": list(runbook.tags),
            },
            "reason": "bootstrap_ready",
        }

    async def validate_runbook(self, rb_id: str) -> ValidationReport:
        return await validate_runbook(self.workdir, rb_id)

    async def promote_runbook(
        self,
        rb_id: str,
        *,
        add_servers: Iterable[str],
        confidence: Optional[float] = None,
        run_validation: bool = True,
    ) -> PromoteResult:
        return await promote_runbook(
            self.workdir,
            rb_id,
            add_servers=list(add_servers),
            confidence=confidence,
            run_validation=run_validation,
        )

    async def run_runbook_step(
        self,
        *,
        rb_id: str,
        step_name: str,
        server_id: str,
        dry_run: bool = True,
        verify_checksum: bool = True,
        timeout_sec: Optional[float] = None,
    ) -> ScriptRunResult:
        return await run_runbook_step(
            workdir=self.workdir,
            rb_id=rb_id,
            step_name=step_name,
            server_id=server_id,
            dry_run=dry_run,
            verify_checksum=verify_checksum,
            timeout_sec=timeout_sec,
        )

    # --- misc ---

    def global_summary(self) -> Dict[str, Any]:
        servers = self.list_servers()
        totals = {"alarm": 0, "warn": 0, "info": 0, "noise": 0}
        statuses: Dict[str, int] = {}
        for s in servers:
            for sev, cnt in s.open_drifts.items():
                if sev in totals:
                    totals[sev] += int(cnt or 0)
            statuses[s.status()] = statuses.get(s.status(), 0) + 1
        return {
            "server_count": len(servers),
            "statuses": statuses,
            "totals": totals,
            "admin_root": str(admin_root(self.workdir)),
        }


def _server_report_to_dict(report: ServerReconcileReport) -> Dict[str, Any]:
    return {
        "server_id": report.server_id,
        "ok": report.ok,
        "baseline_present": report.baseline_present,
        "snapshots_written": report.snapshots_written,
        "drifts_written": report.drifts_written,
        "drifts_by_severity": dict(report.drifts_by_severity or {}),
        "alarm_count": report.alarm_count,
        "warn_count": report.warn_count,
        "error": report.error,
    }


__all__ = [
    "AdminAutonomyService",
    "ServerSummary",
    "parse_server_specs",
]
