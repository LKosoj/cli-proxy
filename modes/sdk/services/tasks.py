from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Dict, List, Optional, Tuple


def _group_key(session_id: str, mode_id: str) -> Tuple[str, str]:
    return (str(session_id), str(mode_id))


def _resolve_session_uid(*, session_id: Optional[str] = None, session_uid: Optional[str] = None) -> str:
    value = session_uid if session_uid not in (None, "") else session_id
    return str(value or "")


@dataclass
class TaskRecord:
    name: str
    task: asyncio.Task


@dataclass
class TaskService:
    """Background asyncio task management grouped by (session_id, mode_id)."""

    tasks: Dict[Tuple[str, str], List[TaskRecord]] = field(default_factory=dict)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def _prune_done(self, group: Tuple[str, str]) -> None:
        alive: List[TaskRecord] = []
        for rec in self.tasks.get(group, []):
            if rec.task.done():
                continue
            alive.append(rec)
        if alive:
            self.tasks[group] = alive
        else:
            self.tasks.pop(group, None)

    def create(
        self,
        *,
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
        mode_id: str,
        coro: Awaitable[Any],
        name: Optional[str] = None,
    ) -> asyncio.Task:
        group = _group_key(_resolve_session_uid(session_id=session_id, session_uid=session_uid), mode_id)
        self._prune_done(group)
        tname = name or "task"
        task = asyncio.create_task(coro)
        self.tasks.setdefault(group, []).append(TaskRecord(name=tname, task=task))

        def _cb(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.log.exception("mode task failed group=%s name=%s err=%s", group, tname, e)
            finally:
                self._prune_done(group)

        task.add_done_callback(_cb)
        return task

    def track(
        self,
        *,
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
        mode_id: str,
        task: Optional[asyncio.Task] = None,
        name: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        group = _group_key(_resolve_session_uid(session_id=session_id, session_uid=session_uid), mode_id)
        self._prune_done(group)
        tracked_task = task or asyncio.current_task()
        if tracked_task is None:
            return None
        for rec in self.tasks.get(group, []):
            if rec.task is tracked_task:
                return tracked_task
        tname = name or tracked_task.get_name() or "task"
        self.tasks.setdefault(group, []).append(TaskRecord(name=tname, task=tracked_task))

        def _cb(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.log.exception("mode task failed group=%s name=%s err=%s", group, tname, e)
            finally:
                self._prune_done(group)

        tracked_task.add_done_callback(_cb)
        return tracked_task

    def list(
        self,
        *,
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
        mode_id: str,
    ) -> List[str]:
        group = _group_key(_resolve_session_uid(session_id=session_id, session_uid=session_uid), mode_id)
        self._prune_done(group)
        return [rec.name for rec in self.tasks.get(group, []) if not rec.task.done()]

    def list_session(
        self,
        *,
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
    ) -> List[str]:
        sid = _resolve_session_uid(session_id=session_id, session_uid=session_uid)
        out: List[str] = []
        for group in list(self.tasks.keys()):
            if group[0] != sid:
                continue
            self._prune_done(group)
            out.extend(rec.name for rec in self.tasks.get(group, []) if not rec.task.done())
        return out

    async def cancel_all(
        self,
        *,
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
        mode_id: str,
        timeout_s: float = 1.0,
    ) -> int:
        group = _group_key(_resolve_session_uid(session_id=session_id, session_uid=session_uid), mode_id)
        self._prune_done(group)
        recs = list(self.tasks.get(group, []))
        if not recs:
            return 0
        for rec in recs:
            rec.task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*(r.task for r in recs), return_exceptions=True), timeout=timeout_s)
        except asyncio.TimeoutError:
            # Caller may decide to wait longer; we still consider them cancelled.
            pass
        self._prune_done(group)
        return len(recs)

    async def cancel_session(
        self,
        *,
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
        timeout_s: float = 1.0,
    ) -> int:
        """
        Cancel all background mode tasks for a session, regardless of mode_id.
        """
        sid = _resolve_session_uid(session_id=session_id, session_uid=session_uid)
        groups = [g for g in list(self.tasks.keys()) if g[0] == sid]
        if not groups:
            return 0

        # Snapshot and cancel atomically (before any await)
        recs: List[TaskRecord] = []
        for group in groups:
            self._prune_done(group)
            recs.extend(self.tasks.get(group, []))

        if not recs:
            return 0

        for rec in recs:
            rec.task.cancel()

        try:
            await asyncio.wait_for(
                asyncio.gather(*(r.task for r in recs), return_exceptions=True),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            pass

        # Re-scan: new tasks may have been added during the await
        for g in [g for g in list(self.tasks.keys()) if g[0] == sid]:
            self._prune_done(g)
        return len(recs)
