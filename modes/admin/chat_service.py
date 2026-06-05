from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

import yaml

from app.services.ssh_config_loader import load_ssh_config
from i18n import t

from .action_specs import (
    build_local_command_spec,
    build_ssh_command_spec,
    resolve_exec_action_payload,
)
from .autonomy_policy import AutonomyPolicy, load_autonomy_policy
from .chat_gateway import AdminChatGateway
from .chat_memory import ChatMemory, ChatPendingStore
from .config_store import AdminConfigStore
from .runbooks import global_runbooks_dir
from .transports import (
    LocalCommandSpec,
    LocalSubprocessTransport,
    LocalTransportError,
    SSHCommandSpec,
    SSHSubprocessTransport,
    SSHTransportError,
)


@dataclass
class AutopilotVerdict:
    allowed: bool
    reason: Optional[str] = None


LlmProviderFactory = Callable[[Any], Callable[[str, str], Awaitable[str]]]


class AdminChatService:
    """Pure service encapsulating admin-chat operations for both Telegram and UI clients.

    Returns structured dicts instead of sending Telegram messages, so facade/routes
    can use the same execution path as the Telegram approve-callback.
    """

    def __init__(
        self,
        *,
        local_transport: Optional[LocalSubprocessTransport] = None,
        ssh_transport: Optional[SSHSubprocessTransport] = None,
        llm_provider_factory: Optional[LlmProviderFactory] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._local_transport = local_transport or LocalSubprocessTransport()
        self._ssh_transport = ssh_transport or SSHSubprocessTransport()
        self._llm_provider_factory = llm_provider_factory or _default_llm_provider_factory
        self._log = logger or logging.getLogger(__name__)

    # ---------- sync read/write ----------

    @staticmethod
    def list_messages(workdir: str) -> List[Dict[str, Any]]:
        mem = ChatMemory(str(workdir))
        return [m.as_dict() for m in mem.load_messages()]

    @staticmethod
    def list_pending(workdir: str) -> List[Dict[str, Any]]:
        store = ChatPendingStore(str(workdir))
        return list(store.list_pending())

    @staticmethod
    def get_memory_md(workdir: str) -> str:
        return ChatMemory(str(workdir)).read_memory_md()

    @staticmethod
    def save_memory_md(workdir: str, *, text: str) -> None:
        ChatMemory(str(workdir)).overwrite_memory_md(str(text or ""))

    @staticmethod
    def counters(workdir: str) -> Dict[str, Any]:
        mem = ChatMemory(str(workdir))
        store = ChatPendingStore(str(workdir))
        messages = mem.load_messages()
        last_ts = messages[-1].ts if messages else ""
        return {
            "messages_count": len(messages),
            "pending_count": len(store.list_ids()),
            "last_message_ts": last_ts,
        }

    def reject_pending(
        self,
        workdir: str,
        *,
        approval_id: str,
    ) -> Dict[str, Any]:
        approval_id = str(approval_id or "").strip()
        if not approval_id:
            return {"ok": False, "error": "invalid_approval_id"}
        if not str(workdir or "").strip():
            return {"ok": False, "error": "workdir_missing"}
        store = ChatPendingStore(str(workdir))
        record = store.pop(approval_id)
        if not record:
            return {"ok": False, "error": "approval_not_found"}
        try:
            ChatMemory(str(workdir)).append(
                role="system",
                text=f"rejected approval {approval_id}",
                intent_type="chat_reject",
                meta={"intent": record.get("intent")},
            )
        except Exception:
            self._log.exception("admin chat: memory append on reject failed")
        return {"ok": True, "approval_id": approval_id}

    # ---------- async gateway ----------

    def build_gateway(
        self,
        *,
        session: Any,
        bot_app: Any,
    ) -> Tuple[AdminChatGateway, ChatPendingStore]:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            raise RuntimeError("session workdir is empty")
        try:
            cfg_store = AdminConfigStore(workdir)
            config_payload = cfg_store.load_effective_config()
        except Exception:
            self._log.exception("admin chat: load_effective_config failed")
            config_payload = {}
        admin_cfg = (
            config_payload.get("admin") if isinstance(config_payload, dict) else None
        ) or {}
        aliases = _resolve_known_ssh_aliases(workdir)
        pinned_cli = _resolve_session_cli_mode(session)
        llm_provider = self._llm_provider_factory(bot_app)
        memory = ChatMemory(workdir)
        pending = ChatPendingStore(workdir)
        gateway = AdminChatGateway(
            workdir=workdir,
            llm_provider=llm_provider,
            admin_config=admin_cfg,
            known_aliases=aliases,
            pinned_cli=pinned_cli,
            session_id=str(getattr(session, "id", "") or ""),
            memory=memory,
        )
        return gateway, pending

    async def send(
        self,
        *,
        session: Any,
        bot_app: Any,
        text: str,
        lang: str = 'ru',
    ) -> Dict[str, Any]:
        user_text = str(text or "").strip()
        if not user_text:
            return {"ok": False, "error": "empty_text"}
        try:
            gateway, pending_store = self.build_gateway(
                session=session, bot_app=bot_app
            )
        except Exception as exc:  # noqa: BLE001
            self._log.exception("admin chat: gateway build failed")
            return {"ok": False, "error": f"gateway_init:{exc}"}

        decision = await gateway.handle(user_text, lang=lang)
        intent = decision.intent
        approval_id = str(decision.pending_action_id or "").strip()
        intent_dict = intent.as_dict() if intent else None
        workdir = str(getattr(session, "workdir", "") or "")

        auto_exec = False
        autopilot_blocked_reason: Optional[str] = None
        exec_result: Optional[Dict[str, Any]] = None

        if (
            intent is not None
            and intent.type in ("propose_action", "propose_new_action", "propose_plan")
            and approval_id
        ):
            verdict = self._autopilot_verdict_for(
                intent_dict=intent_dict or {}, workdir=workdir,
            )
            if verdict.allowed:
                exec_result = await self._auto_execute_intent(
                    session=session,
                    intent_dict=intent_dict or {},
                    lang=lang,
                )
                auto_exec = True
                self._record_autopilot_memory(
                    workdir=workdir,
                    intent_type="intent_autopilot_executed",
                    intent_dict=intent_dict or {},
                    extra={"exec_result": exec_result},
                )
            else:
                autopilot_blocked_reason = verdict.reason or "blocked"
                if verdict.reason and verdict.reason != "autopilot disabled":
                    self._record_autopilot_memory(
                        workdir=workdir,
                        intent_type="intent_autopilot_blocked",
                        intent_dict=intent_dict or {},
                        extra={"reason": autopilot_blocked_reason},
                    )
                try:
                    pending_store.save(
                        approval_id,
                        {
                            "approval_id": approval_id,
                            "session_id": str(getattr(session, "id", "") or ""),
                            "intent": intent.as_dict(),
                            "user_text": user_text,
                            "reply_text": decision.reply_text or "",
                            "pinned_cli": _resolve_session_cli_mode(session),
                            "autopilot_blocked": autopilot_blocked_reason
                            if verdict.reason and verdict.reason != "autopilot disabled"
                            else None,
                        },
                    )
                except Exception:
                    self._log.exception(
                        "admin chat: failed to persist pending approval id=%s",
                        approval_id,
                    )
                    return {
                        "ok": False,
                        "error": "pending_save_failed",
                        "intent": intent_dict,
                    }

        if auto_exec:
            effective_approval_id: Optional[str] = None
        else:
            effective_approval_id = approval_id or None

        return {
            "ok": decision.error is None,
            "error": decision.error,
            "reply_text": decision.reply_text or "",
            "intent": intent_dict,
            "pending_action_id": effective_approval_id,
            "clarification_options": list(decision.clarification_options or []),
            "auto_exec": auto_exec,
            "autopilot_blocked": autopilot_blocked_reason
            if (not auto_exec and autopilot_blocked_reason
                and autopilot_blocked_reason != "autopilot disabled")
            else None,
            "exec_result": exec_result,
        }

    def _autopilot_verdict_for(
        self,
        *,
        intent_dict: Mapping[str, Any],
        workdir: str,
    ) -> AutopilotVerdict:
        try:
            cfg_payload = AdminConfigStore(workdir).load_effective_config() if workdir else {}
        except Exception:
            self._log.exception("admin chat: load config for autopilot failed")
            cfg_payload = {}
        admin_cfg = (
            cfg_payload.get("admin") if isinstance(cfg_payload, dict) else None
        ) or {}
        policy = load_autonomy_policy(admin_cfg)
        sid = _resolve_intent_server_id(intent_dict)
        if sid:
            policy = policy.for_server(sid)
        return _evaluate_autopilot(intent_dict, policy)

    async def _auto_execute_intent(
        self,
        *,
        session: Any,
        intent_dict: Mapping[str, Any],
        lang: str = "ru",
    ) -> Dict[str, Any]:
        itype = str(intent_dict.get("type") or "").strip()
        if itype == "propose_action":
            return await self._execute_propose_action(
                session=session, intent=dict(intent_dict), lang=lang,
            )
        if itype == "propose_new_action":
            return await self._execute_adhoc(
                session=session, intent=dict(intent_dict), lang=lang,
            )
        if itype == "propose_plan":
            # Autopilot forces stop_on_error=True regardless of intent value.
            forced = dict(intent_dict)
            forced["stop_on_error"] = True
            return await self._execute_plan(session=session, intent=forced, lang=lang)
        return {"ok": False, "error": f"unsupported_intent:{itype}"}

    def _record_autopilot_memory(
        self,
        *,
        workdir: str,
        intent_type: str,
        intent_dict: Mapping[str, Any],
        extra: Mapping[str, Any],
    ) -> None:
        if not workdir:
            return
        try:
            meta: Dict[str, Any] = {"intent": dict(intent_dict)}
            meta.update(dict(extra))
            itype = str(intent_dict.get("type") or "").strip()
            summary = f"autopilot {intent_type.rsplit('_', 1)[-1]}: {itype}"
            reason = extra.get("reason") if isinstance(extra, Mapping) else None
            if reason:
                summary += f" ({reason})"
            ChatMemory(workdir).append(
                role="system",
                text=summary[:4000],
                intent_type=intent_type,
                meta=meta,
            )
        except Exception:
            self._log.exception("admin chat: autopilot memory append failed")

    async def execute_pending(
        self,
        *,
        session: Any,
        approval_id: str,
        lang: str = "ru",
    ) -> Dict[str, Any]:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir_missing"}
        approval = str(approval_id or "").strip()
        if not approval:
            return {"ok": False, "error": "invalid_approval_id"}
        store = ChatPendingStore(workdir)
        record = store.pop(approval)
        if not record:
            return {"ok": False, "error": "approval_not_found"}
        intent = record.get("intent") if isinstance(record, dict) else None
        if not isinstance(intent, dict):
            return {"ok": False, "error": "pending_payload_corrupt"}
        intent_type = str(intent.get("type") or "").strip().lower()
        if intent_type == "propose_action":
            return await self._execute_propose_action(
                session=session, intent=intent, lang=lang
            )
        if intent_type == "propose_new_action":
            return await self._execute_adhoc(session=session, intent=intent, lang=lang)
        if intent_type == "propose_plan":
            return await self._execute_plan(session=session, intent=intent, lang=lang)
        return {"ok": False, "error": f"unsupported_intent:{intent_type}"}

    # ---------- private execution ----------

    async def _execute_propose_action(
        self,
        *,
        session: Any,
        intent: Dict[str, Any],
        lang: str = "ru",
    ) -> Dict[str, Any]:
        action_id = str(intent.get("action_id") or "").strip()
        target = str(intent.get("target") or "").strip()
        if not action_id:
            return {"ok": False, "error": "missing_action_id"}
        workdir = str(getattr(session, "workdir", "") or "")
        try:
            cfg_payload = AdminConfigStore(workdir).load_effective_config()
        except Exception:
            self._log.exception("admin chat: load_effective_config failed")
            cfg_payload = {}
        action_payload = resolve_exec_action_payload(
            config_payload=cfg_payload,
            target=target,
            action_id=action_id,
        )
        if action_payload is None:
            return {
                "ok": False,
                "error": f"action_not_found:{target}/{action_id}",
            }
        try:
            if target == "local":
                spec = build_local_command_spec(
                    session=session,
                    action_id=action_id,
                    action_payload=action_payload,
                )
                result = await self._local_transport.run(spec)
                payload = _format_local_result(result)
            else:
                spec = build_ssh_command_spec(
                    session=session,
                    action_id=action_id,
                    action_payload=action_payload,
                )
                result = await self._ssh_transport.run(spec)
                payload = _format_ssh_result(result)
        except (ValueError, LocalTransportError, SSHTransportError) as exc:
            self._log.exception(
                "admin chat: propose_action exec failed action_id=%s", action_id
            )
            return {"ok": False, "error": f"exec_failed:{exc}"}
        except Exception as exc:  # noqa: BLE001
            self._log.exception(
                "admin chat: propose_action unexpected failure action_id=%s",
                action_id,
            )
            return {"ok": False, "error": f"unexpected:{exc}"}
        _append_exec_memory(
            workdir=workdir,
            text=_build_result_text(payload, argv=None, lang=lang),
            exit_code=payload.get("exit_code"),
            target=target,
            intent_type="chat_propose_action",
            logger=self._log,
        )
        return payload

    async def _execute_adhoc(
        self,
        *,
        session: Any,
        intent: Dict[str, Any],
        lang: str = "ru",
    ) -> Dict[str, Any]:
        target = str(intent.get("target") or "").strip()
        argv_raw = intent.get("argv")
        if not isinstance(argv_raw, list) or not argv_raw:
            return {"ok": False, "error": "argv_missing"}
        argv = tuple(str(a) for a in argv_raw if str(a))
        timeout = float(intent.get("timeout_sec") or 30.0)
        workdir = str(getattr(session, "workdir", "") or "")
        synthetic_id = f"chat.adhoc.{int(time.time() * 1000) % 100000}"
        try:
            if target == "local":
                spec = LocalCommandSpec(
                    action_id=synthetic_id,
                    argv=argv,
                    cwd=workdir or None,
                    env=None,
                    timeout_sec=timeout,
                )
                result = await self._local_transport.run(spec)
                payload = _format_local_result(result)
            else:
                try:
                    hosts = load_ssh_config(workdir)
                except Exception:
                    self._log.exception("admin chat: load_ssh_config failed")
                    return {"ok": False, "error": "ssh_loader_failed"}
                host_cfg = hosts.get(target) if isinstance(hosts, dict) else None
                if host_cfg is None:
                    return {
                        "ok": False,
                        "error": f"ssh_alias_unknown:{target}",
                    }
                host = str(getattr(host_cfg, "host", "") or "").strip()
                user = str(getattr(host_cfg, "user", "") or "").strip() or None
                port = int(getattr(host_cfg, "port", 22) or 22)
                key_path = str(getattr(host_cfg, "key_file", "") or "").strip()
                password = ""
                if str(getattr(host_cfg, "auth", "") or "").strip() == "password":
                    from app.services.ssh_config_loader import load_ssh_secrets, resolve_ssh_secret

                    password = str(
                        resolve_ssh_secret(
                            load_ssh_secrets(workdir),
                            str(getattr(host_cfg, "password_env", "") or "").strip() or None,
                        )
                        or ""
                    )
                if key_path:
                    key_path = os.path.expanduser(key_path)
                    if not os.path.isabs(key_path):
                        key_path = os.path.abspath(
                            os.path.join(workdir, key_path)
                        )
                spec = SSHCommandSpec(
                    action_id=synthetic_id,
                    argv=argv,
                    host=host,
                    user=user,
                    port=port,
                    key_path=key_path or "",
                    timeout_sec=timeout,
                    password=password or None,
                )
                result = await self._ssh_transport.run(spec)
                payload = _format_ssh_result(result)
        except (LocalTransportError, SSHTransportError) as exc:
            self._log.exception("admin chat: adhoc exec failed")
            return {"ok": False, "error": f"exec_failed:{exc}"}
        except Exception as exc:  # noqa: BLE001
            self._log.exception("admin chat: adhoc unexpected failure")
            return {"ok": False, "error": f"unexpected:{exc}"}

        _append_exec_memory(
            workdir=workdir,
            text=_build_result_text(payload, argv=list(argv), lang=lang),
            exit_code=payload.get("exit_code"),
            target=target,
            intent_type="chat_adhoc",
            argv=list(argv),
            logger=self._log,
        )
        return payload

    async def _execute_plan(
        self,
        *,
        session: Any,
        intent: Dict[str, Any],
        lang: str = "ru",
    ) -> Dict[str, Any]:
        steps_raw = intent.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            return {"ok": False, "error": "plan_steps_missing"}
        workdir = str(getattr(session, "workdir", "") or "")
        if not workdir:
            return {"ok": False, "error": "workdir_missing"}
        stop_on_error = bool(intent.get("stop_on_error", True))
        plan_id = f"chat.plan.{int(time.time() * 1000) % 1000000}"

        try:
            cfg_payload = AdminConfigStore(workdir).load_effective_config()
        except Exception:
            self._log.exception("admin chat: load_effective_config failed")
            cfg_payload = {}
        try:
            ssh_hosts = load_ssh_config(workdir) or {}
        except Exception:
            self._log.exception("admin chat: load_ssh_config failed")
            ssh_hosts = {}

        results: List[Dict[str, Any]] = []
        stopped_early = False
        for idx, step_raw in enumerate(steps_raw, start=1):
            if not isinstance(step_raw, dict):
                results.append({"step_index": idx, "ok": False, "error": "step_invalid"})
                stopped_early = True
                break
            step_result = await self._execute_plan_step(
                session=session,
                workdir=workdir,
                cfg_payload=cfg_payload,
                ssh_hosts=ssh_hosts if isinstance(ssh_hosts, dict) else {},
                step=step_raw,
                step_index=idx,
                plan_id=plan_id,
            )
            results.append(step_result)
            _append_exec_memory(
                workdir=workdir,
                text=_build_result_text(step_result, argv=step_result.get("argv"), lang=lang),
                exit_code=step_result.get("exit_code"),
                target=str(step_raw.get("target") or ""),
                intent_type="chat_plan_step",
                argv=step_result.get("argv"),
                logger=self._log,
            )
            if not step_result.get("ok") and stop_on_error:
                stopped_early = True
                break

        completed_ok = all(r.get("ok") for r in results) and not stopped_early
        total = len(steps_raw)
        completed = len(results)

        runbook_info: Dict[str, Any] = {"runbook_saved": False}
        if (
            completed_ok
            and bool(intent.get("suggest_save_as_runbook"))
            and str(intent.get("suggested_runbook_id") or "").strip()
        ):
            runbook_info = _save_plan_as_runbook(
                workdir=workdir,
                runbook_id=str(intent.get("suggested_runbook_id")).strip(),
                title=str(intent.get("text") or "").strip() or str(intent.get("suggested_runbook_id")),
                steps=steps_raw,
                stop_on_error=stop_on_error,
                logger=self._log,
            )

        return {
            "ok": completed_ok,
            "target_kind": "plan",
            "plan_id": plan_id,
            "total_steps": total,
            "completed_steps": completed,
            "stopped_early": stopped_early,
            "steps": results,
            **runbook_info,
        }

    async def _execute_plan_step(
        self,
        *,
        session: Any,
        workdir: str,
        cfg_payload: Mapping[str, Any],
        ssh_hosts: Mapping[str, Any],
        step: Dict[str, Any],
        step_index: int,
        plan_id: str,
    ) -> Dict[str, Any]:
        target = str(step.get("target") or "").strip()
        action_id = str(step.get("action_id") or "").strip()
        argv_raw = step.get("argv")
        has_argv = isinstance(argv_raw, list) and argv_raw
        if not target:
            return {"step_index": step_index, "ok": False, "error": "step_target_missing"}
        if not action_id and not has_argv:
            return {"step_index": step_index, "ok": False, "error": "step_missing_action_or_argv"}

        synthetic_id = f"{plan_id}.step{step_index}"
        try:
            if action_id:
                action_payload = resolve_exec_action_payload(
                    config_payload=cfg_payload,
                    target=target,
                    action_id=action_id,
                )
                if action_payload is None:
                    return {
                        "step_index": step_index,
                        "ok": False,
                        "error": f"action_not_found:{target}/{action_id}",
                    }
                if target == "local":
                    spec = build_local_command_spec(
                        session=session,
                        action_id=action_id,
                        action_payload=action_payload,
                    )
                    result = await self._local_transport.run(spec)
                    payload = _format_local_result(result)
                else:
                    spec = build_ssh_command_spec(
                        session=session,
                        action_id=action_id,
                        action_payload=action_payload,
                    )
                    result = await self._ssh_transport.run(spec)
                    payload = _format_ssh_result(result)
                payload["argv"] = list(action_payload.get("argv") or [])
            else:
                argv = tuple(str(a) for a in argv_raw if str(a))
                timeout = float(step.get("timeout_sec") or 30.0)
                if target == "local":
                    spec = LocalCommandSpec(
                        action_id=synthetic_id,
                        argv=argv,
                        cwd=workdir or None,
                        env=None,
                        timeout_sec=timeout,
                    )
                    result = await self._local_transport.run(spec)
                    payload = _format_local_result(result)
                else:
                    host_cfg = ssh_hosts.get(target) if isinstance(ssh_hosts, Mapping) else None
                    if host_cfg is None:
                        return {
                            "step_index": step_index,
                            "ok": False,
                            "error": f"ssh_alias_unknown:{target}",
                            "argv": list(argv),
                        }
                    host = str(getattr(host_cfg, "host", "") or "").strip()
                    user = str(getattr(host_cfg, "user", "") or "").strip() or None
                    port = int(getattr(host_cfg, "port", 22) or 22)
                    key_path = str(getattr(host_cfg, "key_file", "") or "").strip()
                    password = ""
                    if str(getattr(host_cfg, "auth", "") or "").strip() == "password":
                        from app.services.ssh_config_loader import load_ssh_secrets, resolve_ssh_secret

                        password = str(
                            resolve_ssh_secret(
                                load_ssh_secrets(workdir),
                                str(getattr(host_cfg, "password_env", "") or "").strip() or None,
                            )
                            or ""
                        )
                    if key_path:
                        key_path = os.path.expanduser(key_path)
                        if not os.path.isabs(key_path):
                            key_path = os.path.abspath(os.path.join(workdir, key_path))
                    spec = SSHCommandSpec(
                        action_id=synthetic_id,
                        argv=argv,
                        host=host,
                        user=user,
                        port=port,
                        key_path=key_path or "",
                        timeout_sec=timeout,
                        password=password or None,
                    )
                    result = await self._ssh_transport.run(spec)
                    payload = _format_ssh_result(result)
                payload["argv"] = list(argv)
        except (ValueError, LocalTransportError, SSHTransportError) as exc:
            self._log.exception(
                "admin chat: plan step %d failed target=%s action_id=%s",
                step_index, target, action_id,
            )
            return {
                "step_index": step_index,
                "ok": False,
                "error": f"exec_failed:{exc}",
                "target": target,
                "action_id": action_id or None,
            }
        except Exception as exc:  # noqa: BLE001
            self._log.exception(
                "admin chat: plan step %d unexpected failure", step_index,
            )
            return {
                "step_index": step_index,
                "ok": False,
                "error": f"unexpected:{exc}",
                "target": target,
                "action_id": action_id or None,
            }

        payload["step_index"] = step_index
        payload["target"] = target
        if action_id:
            payload["action_id"] = action_id
        return payload


# ---------- module-level helpers ----------

def _default_llm_provider_factory(bot_app: Any) -> Callable[[str, str], Awaitable[str]]:
    cfg = getattr(bot_app, "config", None)

    async def _provider(system: str, user: str) -> str:
        from modes.sdk.runtime.openai_client import chat_completion

        return str(
            await chat_completion(
                cfg,
                str(system or ""),
                str(user or ""),
                response_format={"type": "json_object"},
            )
            or ""
        )

    return _provider


def _resolve_known_ssh_aliases(workdir: str) -> List[str]:
    try:
        hosts = load_ssh_config(workdir)
    except Exception:
        return []
    if not isinstance(hosts, dict):
        return []
    return [str(k) for k in hosts.keys() if str(k or "").strip()]


def _resolve_session_cli_mode(session: Any) -> str:
    cli_value = (
        getattr(session, "cli_mode", None)
        or getattr(getattr(session, "cli", None), "active_cli", None)
        or getattr(session, "active_cli", None)
    )
    return str(cli_value or "").strip()


def _format_local_result(result: Any) -> Dict[str, Any]:
    return {
        "ok": int(getattr(result, "returncode", -1)) == 0
        and not bool(getattr(result, "timed_out", False)),
        "target_kind": "local",
        "action_id": str(getattr(result, "action_id", "") or ""),
        "exit_code": int(getattr(result, "returncode", -1)),
        "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "stdout": str(getattr(result, "stdout", "") or ""),
        "stderr": str(getattr(result, "stderr", "") or ""),
    }


def _format_ssh_result(result: Any) -> Dict[str, Any]:
    return {
        "ok": int(getattr(result, "returncode", -1)) == 0
        and not bool(getattr(result, "timed_out", False)),
        "target_kind": "ssh",
        "action_id": str(getattr(result, "action_id", "") or ""),
        "exit_code": int(getattr(result, "returncode", -1)),
        "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "stdout": str(getattr(result, "stdout", "") or ""),
        "stderr": str(getattr(result, "stderr", "") or ""),
        "host": str(getattr(result, "host", "") or ""),
        "user": str(getattr(result, "user", "") or ""),
        "port": int(getattr(result, "port", 22) or 22),
    }


def _build_result_text(payload: Dict[str, Any], *, argv: Optional[List[str]], lang: str = "ru") -> str:
    chunks: List[str] = []
    if argv:
        chunks.append(t("admin.chat.adhoc_command_line", lang, command=" ".join(argv)))
    target_kind = payload.get("target_kind") or ""
    if target_kind == "ssh":
        host = str(payload.get("host") or "")
        user = str(payload.get("user") or "")
        port = str(payload.get("port") or "22")
        target_line = f"{user + '@' if user else ''}{host}:{port}" if host else ""
        if target_line:
            chunks.append(f"Target: {target_line}")
    chunks.append(f"Exit code: {payload.get('exit_code')}")
    if payload.get("timed_out"):
        chunks.append("Timed out: yes")
    stdout = str(payload.get("stdout") or "").strip()
    if stdout:
        chunks.append(f"STDOUT:\n{stdout}")
    stderr = str(payload.get("stderr") or "").strip()
    if stderr:
        chunks.append(f"STDERR:\n{stderr}")
    return "\n\n".join(chunks)


def _append_exec_memory(
    *,
    workdir: str,
    text: str,
    exit_code: Any,
    target: str,
    intent_type: str,
    argv: Optional[List[str]] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    if not workdir:
        return
    try:
        meta: Dict[str, Any] = {"exit_code": exit_code, "target": target}
        if argv is not None:
            meta["argv"] = list(argv)
        ChatMemory(workdir).append(
            role="exec",
            text=(text or "(no output)")[:4000],
            intent_type=intent_type,
            meta=meta,
        )
    except Exception:
        if logger is not None:
            logger.exception("admin chat: memory append on exec failed")


def _save_plan_as_runbook(
    *,
    workdir: str,
    runbook_id: str,
    title: str,
    steps: List[Any],
    stop_on_error: bool,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    safe_id = "".join(c for c in runbook_id if c.isalnum() or c in ("-", "_"))
    if not safe_id:
        return {"runbook_saved": False, "runbook_error": "invalid_runbook_id"}
    try:
        books_dir = global_runbooks_dir(workdir)
        books_dir.mkdir(parents=True, exist_ok=True)
        path = books_dir / f"{safe_id}.md"
        servers: List[str] = []
        for step in steps:
            if isinstance(step, Mapping):
                target = str(step.get("target") or "").strip()
                if target and target != "local" and target not in servers:
                    servers.append(target)
        frontmatter = {
            "id": safe_id,
            "title": title or safe_id,
            "servers": servers,
            "tags": ["chat-saved"],
            "auto_plan": {
                "steps": [dict(step) for step in steps if isinstance(step, Mapping)],
                "stop_on_error": bool(stop_on_error),
            },
        }
        body_lines = [f"Saved from admin chat. Steps: {len(steps)}."]
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                continue
            target = str(step.get("target") or "")
            action_id = str(step.get("action_id") or "")
            argv = step.get("argv")
            if action_id:
                body_lines.append(f"{idx}. [{target}] action={action_id}")
            elif isinstance(argv, list):
                body_lines.append(f"{idx}. [{target}] argv={' '.join(str(a) for a in argv)}")
            else:
                body_lines.append(f"{idx}. [{target}]")
        text = (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
            + "---\n"
            + "\n".join(body_lines)
            + "\n"
        )
        path.write_text(text, encoding="utf-8")
        return {
            "runbook_saved": True,
            "runbook_id": safe_id,
            "runbook_path": str(path),
        }
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            logger.exception("admin chat: runbook save failed id=%s", safe_id)
        return {"runbook_saved": False, "runbook_error": f"save_failed:{exc}"}


def _evaluate_autopilot(
    intent: Mapping[str, Any],
    policy: AutonomyPolicy,
) -> AutopilotVerdict:
    if not policy.enabled:
        return AutopilotVerdict(False, "autopilot disabled")
    itype = str(intent.get("type") or "").strip()
    if itype == "propose_action":
        action_id = str(intent.get("action_id") or "").strip()
        if not action_id:
            return AutopilotVerdict(False, "missing action_id")
        if policy.permits_action(action_id):
            return AutopilotVerdict(True)
        return AutopilotVerdict(
            False, f"action '{action_id}' not in auto_exec_actions"
        )
    if itype == "propose_new_action":
        argv_raw = intent.get("argv")
        argv = [str(a) for a in argv_raw] if isinstance(argv_raw, list) else []
        if policy.permits_adhoc_argv(argv):
            return AutopilotVerdict(True)
        head = argv[0] if argv else ""
        return AutopilotVerdict(
            False, f"command '{head}' not in auto_exec_adhoc_commands"
        )
    if itype == "propose_plan":
        steps_raw = intent.get("steps")
        steps = list(steps_raw) if isinstance(steps_raw, list) else []
        if not steps:
            return AutopilotVerdict(False, "empty plan")
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                return AutopilotVerdict(False, f"step {idx}: invalid")
            step_verdict = _evaluate_plan_step(step, policy)
            if not step_verdict.allowed:
                return AutopilotVerdict(False, f"step {idx}: {step_verdict.reason}")
        return AutopilotVerdict(True)
    return AutopilotVerdict(False, f"unsupported intent type '{itype}'")


def _evaluate_plan_step(
    step: Mapping[str, Any],
    policy: AutonomyPolicy,
) -> AutopilotVerdict:
    action_id = str(step.get("action_id") or "").strip()
    if action_id:
        if policy.permits_action(action_id):
            return AutopilotVerdict(True)
        return AutopilotVerdict(
            False, f"action '{action_id}' not in auto_exec_actions"
        )
    argv_raw = step.get("argv")
    argv = [str(a) for a in argv_raw] if isinstance(argv_raw, list) else []
    if argv:
        if policy.permits_adhoc_argv(argv):
            return AutopilotVerdict(True)
        head = argv[0] if argv else ""
        return AutopilotVerdict(
            False, f"command '{head}' not in auto_exec_adhoc_commands"
        )
    return AutopilotVerdict(False, "step has neither action_id nor argv")


def _resolve_intent_server_id(intent: Mapping[str, Any]) -> str:
    """Returns a single server_id for policy.for_server(...) or "" if ambiguous/local-only."""
    itype = str(intent.get("type") or "").strip()
    if itype == "propose_plan":
        steps_raw = intent.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            return ""
        targets = set()
        for step in steps_raw:
            if isinstance(step, Mapping):
                t = str(step.get("target") or "").strip()
                if t and t != "local":
                    targets.add(t)
        if len(targets) == 1:
            return next(iter(targets))
        return ""
    target = str(intent.get("target") or "").strip()
    if target and target != "local":
        return target
    return ""


__all__ = [
    "AdminChatService",
    "AutopilotVerdict",
    "LlmProviderFactory",
]
