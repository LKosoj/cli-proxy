"""Admin-bridge cluster extracted from ApplicationFacade.

All public methods are implemented as @staticmethod accepting ``facade: Any``
as the first positional argument.  This lets ApplicationFacade delegate via

    return DesktopAdminFacade.method(self, ...)

while keeping existing unbound-call tests working:

    ApplicationFacade.method(stub, ...)   # stub is SimpleNamespace
      → DesktopAdminFacade.method(stub, ...)   # same stub forwarded transparently
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.admin_config_service import (
    AdminConfigService,
    AdminConfigServiceError,
    AdminConfigSessionNotFoundError,
    AdminConfigSessionRequiredError,
)


class DesktopAdminFacade:
    """Thin admin-bridge between desktop widgets and the admin service layer.

    All methods are static and receive the owning facade (or a compatible
    duck-typed stub) as the first argument so they can be invoked both as
    instance delegation (``self._admin_facade.method(self, ...)``) and as
    unbound calls in tests (``ApplicationFacade.method(stub, ...)``).
    """

    # ------------------------------------------------------------------
    # Internal helpers (mirror of ApplicationFacade class-level helpers)
    # ------------------------------------------------------------------

    @staticmethod
    def _admin_config_service(facade: Any) -> Any:
        service = getattr(facade, "admin_config_service", None)
        if service is not None:
            return service
        return AdminConfigService(facade)

    @staticmethod
    def _desktop_admin_config_error(exc: AdminConfigServiceError) -> str:
        if isinstance(exc, (AdminConfigSessionNotFoundError, AdminConfigSessionRequiredError)):
            return "session_not_found"
        text = str(exc)
        if text == "session workdir is not set":
            return "session_workdir_empty"
        if text.startswith("invalid YAML: "):
            return "invalid_yaml: " + text[len("invalid YAML: "):]
        if text == "admin config must be a YAML mapping":
            return "config_must_be_mapping"
        if text == "admin config is missing `admin` mapping":
            return "admin config is missing admin mapping"
        if text == "each server must be an object":
            return "server must be a mapping"
        if text.startswith("target must be local|ssh: "):
            return "invalid target for " + text[len("target must be local|ssh: "):]
        if text.startswith("timeout_sec must be numeric: "):
            return "timeout_sec must be numeric for " + text[len("timeout_sec must be numeric: "):]
        if text.startswith("timeout_sec must be > 0: "):
            return "timeout_sec must be > 0 for " + text[len("timeout_sec must be > 0: "):]
        return text

    # ------------------------------------------------------------------
    # Config YAML
    # ------------------------------------------------------------------

    @staticmethod
    def save_admin_config_yaml(
        facade: Any,
        session_uid: str,
        *,
        yaml_text: str,
    ) -> Dict[str, Any]:
        try:
            DesktopAdminFacade._admin_config_service(facade).save_yaml(session_uid, yaml_text)
        except AdminConfigServiceError as exc:
            return {"ok": False, "error": DesktopAdminFacade._desktop_admin_config_error(exc)}
        except Exception:
            facade.logger.exception(
                "desktop save_admin_config_yaml failed session_uid=%s",
                session_uid,
            )
            return {"ok": False, "error": "config_write_failed"}
        return {"ok": True}

    # ------------------------------------------------------------------
    # Monitor servers
    # ------------------------------------------------------------------

    @staticmethod
    def save_admin_monitor_servers(
        facade: Any,
        session_uid: str,
        *,
        servers: List[Dict[str, Any]],
        enabled: Optional[bool] = None,
        interval_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"servers": list(servers or [])}
        if enabled is not None:
            payload["enabled"] = bool(enabled)
        if interval_sec is not None:
            payload["interval_sec"] = interval_sec
        try:
            DesktopAdminFacade._admin_config_service(facade).save_monitor_servers(session_uid, payload)
        except AdminConfigServiceError as exc:
            return {"ok": False, "error": DesktopAdminFacade._desktop_admin_config_error(exc)}
        except Exception:
            facade.logger.exception(
                "desktop save_admin_monitor_servers failed session_uid=%s",
                session_uid,
            )
            return {"ok": False, "error": "monitor_servers_write_failed"}
        return {"ok": True}

    # ------------------------------------------------------------------
    # SSH actions
    # ------------------------------------------------------------------

    @staticmethod
    def save_admin_actions_ssh(
        facade: Any,
        session_uid: str,
        *,
        actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            DesktopAdminFacade._admin_config_service(facade).save_ssh_actions(session_uid, list(actions or []))
        except AdminConfigServiceError as exc:
            return {"ok": False, "error": DesktopAdminFacade._desktop_admin_config_error(exc)}
        except Exception:
            facade.logger.exception(
                "desktop save_admin_actions_ssh failed session_uid=%s",
                session_uid,
            )
            return {"ok": False, "error": "actions_ssh_write_failed"}
        return {"ok": True}

    # ------------------------------------------------------------------
    # Chat memory
    # ------------------------------------------------------------------

    @staticmethod
    def save_admin_chat_memory_md(
        facade: Any,
        session_uid: str,
        *,
        text: str,
    ) -> Dict[str, Any]:
        service = facade._resolve_admin_chat_service()
        if service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        _, workdir = facade._require_session_workdir(session_uid)
        if not workdir:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            service.save_memory_md(workdir, text=str(text or ""))
        except Exception as exc:
            facade.logger.exception(
                "desktop save_admin_chat_memory_md failed session_uid=%s", session_uid
            )
            return {"ok": False, "error": f"write_failed:{exc}"}
        return {"ok": True}

    # ------------------------------------------------------------------
    # Chat pending: reject / approve / post
    # ------------------------------------------------------------------

    @staticmethod
    def reject_admin_chat_pending(
        facade: Any,
        session_uid: str,
        *,
        approval_id: str,
    ) -> Dict[str, Any]:
        service = facade._resolve_admin_chat_service()
        if service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        _, workdir = facade._require_session_workdir(session_uid)
        if not workdir:
            return {"ok": False, "error": "session_workdir_empty"}
        return service.reject_pending(workdir, approval_id=str(approval_id or ""))

    @staticmethod
    async def post_admin_chat_message(
        facade: Any,
        session_uid: str,
        *,
        text: str,
    ) -> Dict[str, Any]:
        service = facade._resolve_admin_chat_service()
        if service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        session, workdir = facade._require_session_workdir(session_uid)
        if session is None or not workdir:
            return {"ok": False, "error": "session_not_found_or_no_workdir"}
        try:
            return await service.send(
                session=session,
                bot_app=facade._desktop_bot_app(),
                text=str(text or ""),
            )
        except Exception as exc:
            facade.logger.exception(
                "desktop post_admin_chat_message failed session_uid=%s", session_uid
            )
            return {"ok": False, "error": f"send_failed:{exc}"}

    @staticmethod
    async def approve_admin_chat_pending(
        facade: Any,
        session_uid: str,
        *,
        approval_id: str,
    ) -> Dict[str, Any]:
        service = facade._resolve_admin_chat_service()
        if service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        session, workdir = facade._require_session_workdir(session_uid)
        if session is None or not workdir:
            return {"ok": False, "error": "session_not_found_or_no_workdir"}
        try:
            return await service.execute_pending(
                session=session, approval_id=str(approval_id or ""),
            )
        except Exception as exc:
            facade.logger.exception(
                "desktop approve_admin_chat_pending failed session_uid=%s", session_uid
            )
            return {"ok": False, "error": f"execute_failed:{exc}"}

    # ------------------------------------------------------------------
    # Run history
    # ------------------------------------------------------------------

    @staticmethod
    def list_admin_runs(
        facade: Any,
        session_uid: str,
        *,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        session = facade.session_service.get_session_by_uid(session_uid)
        if not session:
            return []
        bot_app = facade._desktop_bot_app()
        # TODO(M3): obtain RunArtifactStore via service injection instead of bot_app attribute introspection.
        artifact_store = getattr(bot_app, "mode_run_artifact_store", None)
        if artifact_store is None:
            return []
        try:
            handles = artifact_store.list_runs(
                session=session,
                mode_id="admin",
                limit=int(max(1, limit)),
            )
        except Exception:
            facade.logger.exception(
                "desktop list_admin_runs failed session_uid=%s", session_uid
            )
            return []
        rows: List[Dict[str, Any]] = []
        for handle in handles or []:
            try:
                state = artifact_store.load_state(handle) or {}
            except Exception:
                state = {}
            rows.append(
                {
                    "run_id": str(handle.run_id),
                    "status": str(state.get("status") or "-"),
                    "phase": str(state.get("phase") or "-"),
                    "started_at": state.get("started_at") or state.get("created_at") or "-",
                    "finished_at": state.get("finished_at") or "-",
                }
            )
        return rows

    @staticmethod
    def get_admin_run_detail(
        facade: Any,
        session_uid: str,
        *,
        run_id: str,
        events_limit: int = 50,
    ) -> Optional[Dict[str, Any]]:
        session = facade.session_service.get_session_by_uid(session_uid)
        if not session:
            return None
        bot_app = facade._desktop_bot_app()
        # TODO(M3): obtain RunArtifactStore via service injection instead of bot_app attribute introspection.
        artifact_store = getattr(bot_app, "mode_run_artifact_store", None)
        if artifact_store is None:
            return None
        try:
            handle = artifact_store.get_run(
                session=session, mode_id="admin", run_id=str(run_id or "").strip()
            )
        except Exception:
            facade.logger.exception(
                "desktop get_admin_run_detail get_run failed session_uid=%s run_id=%s",
                session_uid,
                run_id,
            )
            return None
        if handle is None:
            return None
        try:
            state = artifact_store.load_state(handle) or {}
            plan = artifact_store.load_plan(handle) or {}
            checkpoints = artifact_store.load_checkpoints(handle) or {}
            events = artifact_store.load_events_tail(handle, limit=int(max(1, events_limit))) or []
        except Exception:
            facade.logger.exception(
                "desktop get_admin_run_detail load failed session_uid=%s run_id=%s",
                session_uid,
                run_id,
            )
            return None
        return {
            "run_id": str(handle.run_id),
            "session_uid": str(handle.session_uid),
            "mode_id": str(handle.mode_id),
            "state": dict(state),
            "plan": dict(plan),
            "checkpoints": dict(checkpoints),
            "events": list(events),
        }
