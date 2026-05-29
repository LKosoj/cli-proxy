from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .baseline import (
    AdminBaselineScanner,
    ServerSpec,
    apply_scan_result,
    load_baseline,
)
from .drift import compare_baselines, drifts_summary
from .memory import (
    DEFAULT_COMPACT_KEEP_TAIL,
    DEFAULT_COMPACT_THRESHOLD,
    NoteEntry,
    ServerMemory,
)
from .snapshot_store import (
    AdminSnapshotStore,
    DEFAULT_RETENTION_DAYS,
    SEVERITY_ALARM,
    SEVERITY_WARN,
    canonical_hash,
)

_log = logging.getLogger(__name__)

DAILY_MAINTENANCE_META_KEY = "last_daily_maintenance_ts"


@dataclass
class ServerReconcileReport:
    server_id: str
    ok: bool
    baseline_present: bool
    snapshots_written: int = 0
    drifts_by_severity: Dict[str, int] = field(default_factory=dict)
    drifts_written: int = 0
    alarm_count: int = 0
    warn_count: int = 0
    error: Optional[str] = None
    scan_errors: Dict[str, str] = field(default_factory=dict)


@dataclass
class TickReport:
    ran_at: int
    servers: List[ServerReconcileReport]

    def summary(self) -> Dict[str, int]:
        return {
            "servers_total": len(self.servers),
            "servers_ok": sum(1 for s in self.servers if s.ok),
            "servers_failed": sum(1 for s in self.servers if not s.ok),
            "total_drifts": sum(s.drifts_written for s in self.servers),
            "total_alarms": sum(s.alarm_count for s in self.servers),
            "total_warns": sum(s.warn_count for s in self.servers),
        }


@dataclass
class MaintenanceReport:
    ran_at: int
    servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class AdminReconciler:
    """
    Координирует: scan → compare to baseline → store snapshot → store drifts.
    Также поддерживает daily-maintenance: cleanup retention + compact memory.
    """

    def __init__(
        self,
        workdir: str,
        *,
        scanner: Optional[AdminBaselineScanner] = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        memory_compact_threshold: int = DEFAULT_COMPACT_THRESHOLD,
        memory_compact_keep_tail: int = DEFAULT_COMPACT_KEEP_TAIL,
        llm_summarizer: Optional[Callable[[List[NoteEntry]], str]] = None,
        alarm_hook: Optional[Callable[[str, List[Any]], None]] = None,
    ) -> None:
        self.workdir = str(workdir or "")
        self._scanner = scanner or AdminBaselineScanner(secrets_workdir=self.workdir)
        self._retention_days = int(retention_days)
        self._memory_compact_threshold = int(memory_compact_threshold)
        self._memory_compact_keep_tail = int(memory_compact_keep_tail)
        self._llm_summarizer = llm_summarizer
        self._alarm_hook = alarm_hook

    async def reconcile_server(self, spec: ServerSpec) -> ServerReconcileReport:
        server_id = str(spec.server_id)
        report = ServerReconcileReport(server_id=server_id, ok=False, baseline_present=False)
        try:
            profile = await self._scanner.scan(spec)
        except Exception as exc:
            _log.exception("reconcile: scan failed for %s", server_id)
            report.error = str(exc)
            return report

        report.scan_errors = dict(profile.get("errors") or {})

        baseline = load_baseline(self.workdir, server_id)
        report.baseline_present = baseline is not None

        # если baseline ещё нет — фиксируем первичный скан как baseline, drift'ов не считаем
        if baseline is None:
            try:
                apply_scan_result(self.workdir, server_id, profile)
                report.baseline_present = True
            except Exception as exc:
                _log.exception("reconcile: failed to write baseline for %s", server_id)
                report.error = str(exc)
                return report

        store = AdminSnapshotStore.for_server(
            self.workdir, server_id, retention_days=self._retention_days,
        )
        checks = dict((profile.get("checks") or {}))
        ts = int(time.time())
        snaps_written = 0
        for check_id, value in checks.items():
            if value is None:
                continue
            try:
                store.insert_snapshot(check_id=str(check_id), value=value, ts=ts)
                snaps_written += 1
            except Exception:
                _log.exception("reconcile: failed snapshot check=%s server=%s", check_id, server_id)
        report.snapshots_written = snaps_written

        drifts = []
        if baseline is not None:
            drifts = compare_baselines(baseline, profile)

        written = 0
        for drift in drifts:
            try:
                prev_hash = canonical_hash(drift.prev_value) if drift.prev_value is not None else None
                new_hash = canonical_hash(drift.new_value)
                store.insert_drift(
                    check_id=drift.check_id,
                    severity=drift.severity,
                    new_value=drift.new_value,
                    new_hash=new_hash,
                    prev_value=drift.prev_value,
                    prev_hash=prev_hash,
                    details={"kind": drift.kind, **drift.details},
                    ts=ts,
                )
                written += 1
            except Exception:
                _log.exception(
                    "reconcile: failed to persist drift check=%s server=%s",
                    drift.check_id, server_id,
                )
        report.drifts_written = written
        report.drifts_by_severity = drifts_summary(drifts)
        report.alarm_count = report.drifts_by_severity.get(SEVERITY_ALARM, 0)
        report.warn_count = report.drifts_by_severity.get(SEVERITY_WARN, 0)

        if report.alarm_count > 0 and self._alarm_hook is not None:
            try:
                self._alarm_hook(server_id, drifts)
            except Exception:
                _log.exception("reconcile: alarm hook failed server=%s", server_id)

        report.ok = True
        return report

    async def tick(self, server_specs: Iterable[ServerSpec]) -> TickReport:
        specs = list(server_specs or [])
        reports: List[ServerReconcileReport] = []
        for spec in specs:
            try:
                report = await self.reconcile_server(spec)
            except Exception as exc:
                _log.exception("reconcile: unexpected failure for %s", spec.server_id)
                report = ServerReconcileReport(
                    server_id=str(spec.server_id),
                    ok=False,
                    baseline_present=False,
                    error=str(exc),
                )
            reports.append(report)
        return TickReport(ran_at=int(time.time()), servers=reports)

    def daily_maintenance(self, server_ids: Sequence[str]) -> MaintenanceReport:
        report = MaintenanceReport(ran_at=int(time.time()))
        for sid in list(server_ids or []):
            if not sid:
                continue
            entry: Dict[str, Any] = {"server_id": sid}
            try:
                store = AdminSnapshotStore.for_server(
                    self.workdir, sid, retention_days=self._retention_days,
                )
                cleanup = store.cleanup_retention(max_age_days=self._retention_days)
                entry["cleanup"] = cleanup
                store.vacuum()
                store.set_meta(DAILY_MAINTENANCE_META_KEY, str(report.ran_at))
            except Exception as exc:
                _log.exception("reconcile daily: cleanup failed server=%s", sid)
                entry["cleanup_error"] = str(exc)
            try:
                memory = ServerMemory(self.workdir, sid)
                compact_report = memory.compact_notes(
                    threshold_entries=self._memory_compact_threshold,
                    keep_tail=self._memory_compact_keep_tail,
                    llm_summarizer=self._llm_summarizer,
                )
                entry["memory_compact"] = compact_report
            except Exception as exc:
                _log.exception("reconcile daily: memory compact failed server=%s", sid)
                entry["memory_compact_error"] = str(exc)
            report.servers[sid] = entry
        return report

    async def run_forever(
        self,
        *,
        server_specs_provider: Callable[[], Iterable[ServerSpec]],
        tick_interval_sec: int,
        daily_maintenance_interval_sec: int = 86400,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """
        Опциональный встроенный scheduler. Внешний runner_service может
        использовать `tick` и `daily_maintenance` напрямую — этот метод нужен
        только для standalone режима или тестов.
        """
        interval = max(5, int(tick_interval_sec))
        daily_interval = max(60, int(daily_maintenance_interval_sec))
        last_daily = 0.0
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                specs = list(server_specs_provider() or [])
                await self.tick(specs)
            except Exception:
                _log.exception("reconcile: tick failed")

            now = time.time()
            if now - last_daily >= daily_interval:
                try:
                    ids = [str(s.server_id) for s in (server_specs_provider() or []) if s.server_id]
                    self.daily_maintenance(ids)
                    last_daily = now
                except Exception:
                    _log.exception("reconcile: daily maintenance failed")

            try:
                if stop_event is not None:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    return
                await asyncio.sleep(interval)
            except asyncio.TimeoutError:
                continue


__all__ = [
    "AdminReconciler",
    "DAILY_MAINTENANCE_META_KEY",
    "MaintenanceReport",
    "ServerReconcileReport",
    "TickReport",
]
