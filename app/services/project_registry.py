from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

from app.services.actor_identity import normalize_actor_id
from app.services.path_normalization import normalize_state_path
from app.services.state_repository import get_state_repository

logger = logging.getLogger(__name__)


class ProjectRegistryError(RuntimeError):
    """Base error for project registry operations."""


class ProjectConflictError(ProjectRegistryError):
    """Raised when a project cannot be registered due to uniqueness conflict."""


class ProjectOwnershipError(ProjectRegistryError):
    """Raised when a project belongs to another owner."""


class ProjectNotFoundError(ProjectRegistryError):
    """Raised when a project is not found in registry."""


@dataclass(frozen=True)
class ProjectRecord:
    slug: str
    name: str
    path: str
    enabled: bool
    owner_id: str


class ProjectRegistry:
    TABLE_NAME = "projects"

    def __init__(self, state_path: str) -> None:
        self.db_path = str(get_state_repository(normalize_state_path(state_path)).db_path)
        self._lock = threading.RLock()
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                        slug TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL UNIQUE,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        owner_id TEXT NOT NULL,
                        created_at REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL DEFAULT 0
                    )
                    """
                )
                columns = {
                    str(row["name"] or "")
                    for row in conn.execute(f"PRAGMA table_info({self.TABLE_NAME})").fetchall()
                }
                if "owner_id" not in columns:
                    raise ProjectRegistryError(
                        "project registry schema is outdated: projects table is missing required column owner_id"
                    )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_owner_enabled
                    ON {self.TABLE_NAME}(owner_id, enabled, slug)
                    """
                )

    @staticmethod
    def _normalize_path(path: str) -> str:
        token = str(path or "").strip()
        if not token:
            raise ProjectRegistryError("project path is required")
        normalized = os.path.realpath(token)
        if not os.path.isdir(normalized):
            raise ProjectRegistryError("project path does not exist")
        return normalized

    @staticmethod
    def _normalize_name(name: Optional[str], path: str) -> str:
        token = str(name or "").strip()
        if token:
            return token
        base = os.path.basename(os.path.normpath(path))
        return base or path

    @classmethod
    def _slugify(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(value or ""))
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
        token = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value.lower()).strip("-")
        return token or "project"

    def _row_to_record(self, row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            slug=str(row["slug"] or ""),
            name=str(row["name"] or ""),
            path=str(row["path"] or ""),
            enabled=bool(int(row["enabled"] or 0)),
            owner_id=str(row["owner_id"] or ""),
        )

    def _find_by_path_conn(self, conn: sqlite3.Connection, path: str) -> Optional[ProjectRecord]:
        row = conn.execute(
            f"""
            SELECT slug, name, path, enabled, owner_id
            FROM {self.TABLE_NAME}
            WHERE path = ?
            """,
            (str(path),),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def _find_by_slug_conn(self, conn: sqlite3.Connection, slug: str) -> Optional[ProjectRecord]:
        row = conn.execute(
            f"""
            SELECT slug, name, path, enabled, owner_id
            FROM {self.TABLE_NAME}
            WHERE slug = ?
            """,
            (str(slug),),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def _allocate_slug_conn(self, conn: sqlite3.Connection, base_slug: str) -> str:
        slug = self._slugify(base_slug)
        candidate = slug
        counter = 2
        while self._find_by_slug_conn(conn, candidate) is not None:
            candidate = f"{slug}-{counter}"
            counter += 1
        return candidate

    def validate_registration(self, *, path: str, owner_id: str | int) -> None:
        normalized_path = self._normalize_path(path)
        owner = normalize_actor_id(owner_id, default_surface="telegram")
        if not owner:
            raise ProjectRegistryError("owner_id is required")
        with self._lock:
            with self._connect() as conn:
                existing = self._find_by_path_conn(conn, normalized_path)
                if existing is None:
                    return
                if str(existing.owner_id) != owner:
                    raise ProjectOwnershipError(
                        f"project path is already owned by another actor: {normalized_path}"
                    )

    def register_project(
        self,
        *,
        path: str,
        owner_id: str | int,
        name: Optional[str] = None,
        enabled: bool = True,
        slug: Optional[str] = None,
    ) -> ProjectRecord:
        normalized_path = self._normalize_path(path)
        owner = normalize_actor_id(owner_id, default_surface="telegram")
        if not owner:
            raise ProjectRegistryError("owner_id is required")
        project_name = self._normalize_name(name, normalized_path)
        project_enabled = 1 if bool(enabled) else 0
        created_at = float(time.time())

        with self._lock:
            with self._connect() as conn:
                existing = self._find_by_path_conn(conn, normalized_path)
                if existing is not None:
                    if str(existing.owner_id) != owner:
                        raise ProjectOwnershipError(
                            f"project path is already owned by another actor: {normalized_path}"
                        )
                    conn.execute(
                        f"""
                        UPDATE {self.TABLE_NAME}
                        SET name = ?, enabled = ?, updated_at = ?
                        WHERE path = ?
                        """,
                        (project_name, project_enabled, created_at, normalized_path),
                    )
                    refreshed = self._find_by_path_conn(conn, normalized_path)
                    assert refreshed is not None
                    return refreshed

                requested_slug = str(slug or "").strip()
                if requested_slug:
                    allocated_slug = self._slugify(requested_slug)
                    slug_owner = self._find_by_slug_conn(conn, allocated_slug)
                    if slug_owner is not None:
                        raise ProjectConflictError(f"project slug already exists: {allocated_slug}")
                else:
                    allocated_slug = self._allocate_slug_conn(conn, os.path.basename(normalized_path) or project_name)

                conn.execute(
                    f"""
                    INSERT INTO {self.TABLE_NAME}(
                        slug, name, path, enabled, owner_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        allocated_slug,
                        project_name,
                        normalized_path,
                        project_enabled,
                        str(owner),
                        created_at,
                        created_at,
                    ),
                )
                created = self._find_by_path_conn(conn, normalized_path)
                assert created is not None
                return created

    def get_by_path(self, path: str) -> Optional[ProjectRecord]:
        normalized_path = self._normalize_path(path)
        with self._lock:
            with self._connect() as conn:
                return self._find_by_path_conn(conn, normalized_path)

    def require_owner(self, *, path: str, owner_id: str | int) -> ProjectRecord:
        normalized_path = self._normalize_path(path)
        owner = normalize_actor_id(owner_id, default_surface="telegram")
        if not owner:
            raise ProjectRegistryError("owner_id is required")
        with self._lock:
            with self._connect() as conn:
                record = self._find_by_path_conn(conn, normalized_path)
                if record is None:
                    raise ProjectNotFoundError(f"project is not registered: {normalized_path}")
                if str(record.owner_id) != owner:
                    raise ProjectOwnershipError(
                        f"project path is already owned by another actor: {normalized_path}"
                    )
                return record

    def list_projects(self, *, owner_id: Optional[str | int] = None, enabled_only: bool = False) -> list[ProjectRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(normalize_actor_id(owner_id, default_surface="telegram"))
        if enabled_only:
            clauses.append("enabled = 1")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT slug, name, path, enabled, owner_id
                    FROM {self.TABLE_NAME}
                    {where_sql}
                    ORDER BY owner_id, slug
                    """,
                    params,
                ).fetchall()
        return [self._row_to_record(row) for row in rows]
