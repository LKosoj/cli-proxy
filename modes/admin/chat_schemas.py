from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


INTENT_TYPES = (
    "answer",
    "ask_clarification",
    "run_readonly",
    "propose_action",
    "propose_new_action",
    "propose_plan",
    "update_memory",
)

RISK_LEVELS = ("low", "medium", "high")

ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


_BUILTIN_DENYLIST_PATTERNS: Tuple[str, ...] = (
    r"\brm\s+-rf\s+/(\s|$)",
    r"\brm\s+-rf\s+/\*",
    r"\bdd\s+.*\bof=/dev/(sd|nvme|hd|xvd)",
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;:",
    r"\bmkfs(\.[a-z0-9]+)?\s+/dev/",
    r">\s*/dev/(sd|nvme|hd|xvd)",
    r"\bchmod\s+-R\s+777\s+/(\s|$)",
    r"\bchown\s+-R\s+[^\s]+\s+/(\s|$)",
    r"\bshutdown\b.*-h\s+now",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bwipefs\s+",
    r"\buserdel\s+-r\s+root",
    r"\bpasswd\s+root",
    r"\binit\s+0\b",
    r"\binit\s+6\b",
    r"/etc/shadow",
    r"\bcurl\s+.*\|\s*(ba)?sh\b",
    r"\bwget\s+.*\|\s*(ba)?sh\b",
)


def compile_denylist(extra_patterns: Optional[Sequence[str]] = None) -> Tuple[re.Pattern[str], ...]:
    patterns = list(_BUILTIN_DENYLIST_PATTERNS)
    for extra in extra_patterns or ():
        candidate = str(extra or "").strip()
        if candidate:
            patterns.append(candidate)
    compiled: List[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return tuple(compiled)


def matches_denylist(command: str, compiled: Sequence[re.Pattern[str]]) -> Optional[str]:
    text = str(command or "")
    if not text:
        return None
    if _command_matches_rm_force_recursive_root(text):
        return "rm -rf /"
    for rx in compiled:
        if rx.search(text):
            return rx.pattern
    return None


def argv_denylist_hit(argv: Sequence[str]) -> Optional[str]:
    for check in _BUILTIN_ARGV_CHECKS:
        hit = check(argv)
        if hit:
            return hit
    return None


@dataclass
class PlanStep:
    target: str
    action_id: Optional[str] = None
    argv: Optional[List[str]] = None
    timeout_sec: Optional[float] = None
    risk_level: Optional[str] = None
    rationale: str = ""

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"target": self.target}
        if self.action_id:
            payload["action_id"] = self.action_id
        if self.argv is not None:
            payload["argv"] = list(self.argv)
        if self.timeout_sec is not None:
            payload["timeout_sec"] = float(self.timeout_sec)
        if self.risk_level:
            payload["risk_level"] = self.risk_level
        if self.rationale:
            payload["rationale"] = self.rationale
        return payload


@dataclass
class Intent:
    type: str
    text: str = ""
    action_id: Optional[str] = None
    target: Optional[str] = None
    argv: Optional[List[str]] = None
    timeout_sec: Optional[float] = None
    risk_level: Optional[str] = None
    rationale: str = ""
    suggest_save: bool = False
    suggested_action_id: Optional[str] = None
    steps: List[PlanStep] = field(default_factory=list)
    stop_on_error: bool = True
    suggest_save_as_runbook: bool = False
    suggested_runbook_id: Optional[str] = None
    memory_append: str = ""
    clarification_options: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type, "text": self.text}
        if self.action_id:
            payload["action_id"] = self.action_id
        if self.target:
            payload["target"] = self.target
        if self.argv is not None:
            payload["argv"] = list(self.argv)
        if self.timeout_sec is not None:
            payload["timeout_sec"] = float(self.timeout_sec)
        if self.risk_level:
            payload["risk_level"] = self.risk_level
        if self.rationale:
            payload["rationale"] = self.rationale
        if self.suggest_save:
            payload["suggest_save"] = True
        if self.suggested_action_id:
            payload["suggested_action_id"] = self.suggested_action_id
        if self.steps:
            payload["steps"] = [step.as_dict() for step in self.steps]
            payload["stop_on_error"] = bool(self.stop_on_error)
        if self.suggest_save_as_runbook:
            payload["suggest_save_as_runbook"] = True
        if self.suggested_runbook_id:
            payload["suggested_runbook_id"] = self.suggested_runbook_id
        if self.memory_append:
            payload["memory_append"] = self.memory_append
        if self.clarification_options:
            payload["clarification_options"] = list(self.clarification_options)
        return payload


class IntentValidationError(ValueError):
    def __init__(self, message: str, *, intent_type: Optional[str] = None) -> None:
        super().__init__(message)
        self.intent_type = intent_type


@dataclass
class ValidationContext:
    known_aliases: frozenset  # host aliases incl. "local"
    actions_local: Mapping[str, Mapping[str, Any]]
    actions_ssh: Mapping[str, Mapping[str, Any]]
    denylist: Sequence[re.Pattern[str]]

    def action_definition(self, *, target: str, action_id: str) -> Optional[Mapping[str, Any]]:
        bucket = self.actions_local if target == "local" else self.actions_ssh
        return bucket.get(action_id)


def build_validation_context(
    *,
    admin_config: Mapping[str, Any],
    known_aliases: Sequence[str],
    extra_denylist_patterns: Optional[Sequence[str]] = None,
) -> ValidationContext:
    actions_cfg = (admin_config or {}).get("actions") or {}
    actions_local = _normalize_actions_map(actions_cfg.get("local"))
    actions_ssh = _normalize_actions_map(actions_cfg.get("ssh"))

    chat_cfg = ((admin_config or {}).get("chat") or {}) if isinstance(admin_config, Mapping) else {}
    extra_patterns: List[str] = []
    if isinstance(chat_cfg, Mapping):
        raw_patterns = chat_cfg.get("denylist_patterns") or []
        if isinstance(raw_patterns, (list, tuple)):
            extra_patterns.extend(str(p) for p in raw_patterns)
    if extra_denylist_patterns:
        extra_patterns.extend(str(p) for p in extra_denylist_patterns)

    aliases = {str(a).strip() for a in known_aliases if str(a or "").strip()}
    aliases.add("local")
    return ValidationContext(
        known_aliases=frozenset(aliases),
        actions_local=actions_local,
        actions_ssh=actions_ssh,
        denylist=compile_denylist(extra_patterns),
    )


def _normalize_actions_map(raw: Any) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Mapping):
        for key, item in raw.items():
            if not isinstance(item, Mapping):
                continue
            action_id = str(
                item.get("action_id")
                or item.get("id")
                or str(key or "")
            ).strip()
            if not action_id or not ACTION_ID_RE.match(action_id):
                continue
            normalized = dict(item)
            normalized.setdefault("action_id", action_id)
            result[action_id] = normalized
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            action_id = str(item.get("action_id") or item.get("id") or "").strip()
            if not action_id or not ACTION_ID_RE.match(action_id):
                continue
            normalized = dict(item)
            normalized.setdefault("action_id", action_id)
            result[action_id] = normalized
    return result


def _argv_matches_rm_force_recursive_root(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    head = str(argv[0] or "").strip()
    # support "rm" and "/usr/bin/rm" etc.
    if head.rsplit("/", 1)[-1] != "rm":
        return False
    has_recursive = False
    has_force = False
    targets_root = False
    saw_double_dash = False
    for arg in argv[1:]:
        a = str(arg or "")
        if not saw_double_dash and a == "--":
            saw_double_dash = True
            continue
        if not saw_double_dash and a.startswith("--"):
            long_opt = a.split("=", 1)[0]
            if long_opt == "--recursive":
                has_recursive = True
            elif long_opt == "--force":
                has_force = True
            elif long_opt == "--no-preserve-root":
                has_force = True
            continue
        if not saw_double_dash and a.startswith("-") and len(a) > 1:
            short_flags = a[1:]
            if any(c in short_flags for c in ("r", "R")):
                has_recursive = True
            if "f" in short_flags:
                has_force = True
            continue
        if a == "/" or a == "/*":
            targets_root = True
    return has_recursive and has_force and targets_root


def _command_matches_rm_force_recursive_root(command: str) -> bool:
    try:
        tokens = shlex.split(str(command or ""))
    except ValueError:
        tokens = str(command or "").split()
    for idx, token in enumerate(tokens):
        if str(token or "").strip().rsplit("/", 1)[-1] != "rm":
            continue
        if _argv_matches_rm_force_recursive_root(tokens[idx:]):
            return True
    return False


_BUILTIN_ARGV_CHECKS: Tuple[Callable[[Sequence[str]], Optional[str]], ...] = (
    lambda argv: "rm -rf /"
    if _argv_matches_rm_force_recursive_root(argv)
    else None,
)


def parse_intent(payload: Any) -> Intent:
    if not isinstance(payload, Mapping):
        raise IntentValidationError("intent payload must be a JSON object")

    intent_type = str(payload.get("type") or "").strip().lower()
    if intent_type not in INTENT_TYPES:
        raise IntentValidationError(
            f"unknown intent type: {intent_type!r}",
            intent_type=intent_type or None,
        )

    text = str(payload.get("text") or "").strip()
    intent = Intent(type=intent_type, text=text)

    if intent_type == "answer":
        if not text:
            raise IntentValidationError("answer requires non-empty text", intent_type=intent_type)
        return intent

    if intent_type == "ask_clarification":
        if not text:
            raise IntentValidationError("ask_clarification requires non-empty text", intent_type=intent_type)
        options = payload.get("clarification_options") or []
        if isinstance(options, list):
            intent.clarification_options = [str(o).strip() for o in options if str(o or "").strip()]
        return intent

    if intent_type == "update_memory":
        memory_append = str(payload.get("memory_append") or payload.get("text") or "").strip()
        if not memory_append:
            raise IntentValidationError("update_memory requires memory_append text", intent_type=intent_type)
        intent.memory_append = memory_append
        return intent

    if intent_type in ("run_readonly", "propose_action"):
        action_id = str(payload.get("action_id") or "").strip()
        target = str(payload.get("target") or "").strip()
        if not action_id or not ACTION_ID_RE.match(action_id):
            raise IntentValidationError(
                f"{intent_type} requires valid action_id", intent_type=intent_type
            )
        if not target:
            raise IntentValidationError(
                f"{intent_type} requires target", intent_type=intent_type
            )
        intent.action_id = action_id
        intent.target = target
        rationale = str(payload.get("rationale") or "").strip()
        intent.rationale = rationale
        return intent

    if intent_type == "propose_new_action":
        target = str(payload.get("target") or "").strip()
        argv_raw = payload.get("argv")
        if not target:
            raise IntentValidationError("propose_new_action requires target", intent_type=intent_type)
        if not isinstance(argv_raw, list) or not argv_raw:
            raise IntentValidationError("propose_new_action requires non-empty argv", intent_type=intent_type)
        argv = [str(a) for a in argv_raw]
        if any(not a for a in argv):
            raise IntentValidationError("propose_new_action argv entries must be non-empty", intent_type=intent_type)
        risk_level = str(payload.get("risk_level") or "").strip().lower()
        if risk_level not in RISK_LEVELS:
            raise IntentValidationError(
                "propose_new_action requires risk_level ∈ {low, medium, high}",
                intent_type=intent_type,
            )
        timeout_sec = payload.get("timeout_sec")
        intent.target = target
        intent.argv = argv
        intent.risk_level = risk_level
        intent.rationale = str(payload.get("rationale") or "").strip()
        if timeout_sec is not None:
            try:
                intent.timeout_sec = float(timeout_sec)
            except (TypeError, ValueError):
                raise IntentValidationError("timeout_sec must be numeric", intent_type=intent_type)
        intent.suggest_save = bool(payload.get("suggest_save"))
        suggested_action_id = str(payload.get("suggested_action_id") or "").strip()
        if suggested_action_id:
            if not ACTION_ID_RE.match(suggested_action_id):
                raise IntentValidationError(
                    "suggested_action_id is invalid", intent_type=intent_type
                )
            intent.suggested_action_id = suggested_action_id
        return intent

    if intent_type == "propose_plan":
        steps_raw = payload.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise IntentValidationError("propose_plan requires non-empty steps", intent_type=intent_type)
        parsed_steps: List[PlanStep] = []
        for idx, step_raw in enumerate(steps_raw):
            if not isinstance(step_raw, Mapping):
                raise IntentValidationError(
                    f"propose_plan step[{idx}] must be an object", intent_type=intent_type
                )
            target = str(step_raw.get("target") or "").strip()
            if not target:
                raise IntentValidationError(
                    f"propose_plan step[{idx}] requires target", intent_type=intent_type
                )
            action_id = str(step_raw.get("action_id") or "").strip() or None
            argv_raw = step_raw.get("argv")
            argv = None
            if isinstance(argv_raw, list) and argv_raw:
                argv = [str(a) for a in argv_raw]
            if not action_id and not argv:
                raise IntentValidationError(
                    f"propose_plan step[{idx}] requires action_id or argv",
                    intent_type=intent_type,
                )
            risk_level = str(step_raw.get("risk_level") or "").strip().lower() or None
            if risk_level is not None and risk_level not in RISK_LEVELS:
                raise IntentValidationError(
                    f"propose_plan step[{idx}] risk_level invalid",
                    intent_type=intent_type,
                )
            timeout_sec: Optional[float] = None
            if step_raw.get("timeout_sec") is not None:
                try:
                    timeout_sec = float(step_raw.get("timeout_sec"))
                except (TypeError, ValueError):
                    raise IntentValidationError(
                        f"propose_plan step[{idx}] timeout_sec invalid",
                        intent_type=intent_type,
                    )
            parsed_steps.append(
                PlanStep(
                    target=target,
                    action_id=action_id,
                    argv=argv,
                    timeout_sec=timeout_sec,
                    risk_level=risk_level,
                    rationale=str(step_raw.get("rationale") or "").strip(),
                )
            )
        intent.steps = parsed_steps
        intent.stop_on_error = bool(payload.get("stop_on_error", True))
        intent.suggest_save_as_runbook = bool(payload.get("suggest_save_as_runbook"))
        suggested_runbook_id = str(payload.get("suggested_runbook_id") or "").strip()
        if suggested_runbook_id:
            if not ACTION_ID_RE.match(suggested_runbook_id):
                raise IntentValidationError(
                    "suggested_runbook_id is invalid", intent_type=intent_type
                )
            intent.suggested_runbook_id = suggested_runbook_id
        return intent

    raise IntentValidationError(f"unhandled intent type: {intent_type}", intent_type=intent_type)


def revalidate_intent(intent: Intent, *, ctx: ValidationContext) -> Intent:
    if intent.type in ("answer", "ask_clarification", "update_memory"):
        return intent

    if intent.type in ("run_readonly", "propose_action"):
        target = intent.target or ""
        if target != "local" and target not in ctx.known_aliases:
            raise IntentValidationError(
                f"unknown target alias: {target!r}", intent_type=intent.type
            )
        action_def = ctx.action_definition(
            target="local" if target == "local" else "ssh",
            action_id=intent.action_id or "",
        )
        if not action_def:
            raise IntentValidationError(
                f"action_id {intent.action_id!r} is not in allowlist for target={target!r}",
                intent_type=intent.type,
            )
        risk_level = str(action_def.get("risk_level") or "").strip().lower()
        intent.risk_level = risk_level or intent.risk_level
        argv = action_def.get("argv") or []
        if isinstance(argv, list):
            intent.argv = [str(a) for a in argv]
        try:
            intent.timeout_sec = float(action_def.get("timeout_sec")) if action_def.get("timeout_sec") is not None else None
        except (TypeError, ValueError):
            intent.timeout_sec = None

        if intent.type == "run_readonly":
            if risk_level != "low":
                raise IntentValidationError(
                    f"run_readonly requires risk_level=low (got {risk_level!r})",
                    intent_type=intent.type,
                )
            if not bool(action_def.get("read_only")):
                raise IntentValidationError(
                    f"action {intent.action_id!r} is not marked read_only",
                    intent_type=intent.type,
                )
        command_text = " ".join(intent.argv or [])
        hit = matches_denylist(command_text, ctx.denylist)
        if hit:
            raise IntentValidationError(
                f"action command matches denylist: {hit}", intent_type=intent.type
            )
        return intent

    if intent.type == "propose_new_action":
        target = intent.target or ""
        if target != "local" and target not in ctx.known_aliases:
            raise IntentValidationError(
                f"unknown target alias: {target!r}", intent_type=intent.type
            )
        argv_hit = argv_denylist_hit(intent.argv or [])
        if argv_hit:
            raise IntentValidationError(
                f"proposed command matches denylist: {argv_hit}",
                intent_type=intent.type,
            )
        command_text = " ".join(intent.argv or [])
        hit = matches_denylist(command_text, ctx.denylist)
        if hit:
            raise IntentValidationError(
                f"proposed command matches denylist: {hit}", intent_type=intent.type
            )
        return intent

    if intent.type == "propose_plan":
        for idx, step in enumerate(intent.steps):
            target = step.target
            if target != "local" and target not in ctx.known_aliases:
                raise IntentValidationError(
                    f"step[{idx}] unknown target alias: {target!r}",
                    intent_type=intent.type,
                )
            if step.action_id:
                action_def = ctx.action_definition(
                    target="local" if target == "local" else "ssh",
                    action_id=step.action_id,
                )
                if not action_def:
                    raise IntentValidationError(
                        f"step[{idx}] action_id {step.action_id!r} not in allowlist",
                        intent_type=intent.type,
                    )
                step.argv = [str(a) for a in (action_def.get("argv") or [])]
                if step.risk_level is None:
                    step.risk_level = str(action_def.get("risk_level") or "").strip().lower() or None
                if step.timeout_sec is None and action_def.get("timeout_sec") is not None:
                    try:
                        step.timeout_sec = float(action_def.get("timeout_sec"))
                    except (TypeError, ValueError):
                        step.timeout_sec = None
            argv_hit = argv_denylist_hit(step.argv or [])
            if argv_hit:
                raise IntentValidationError(
                    f"step[{idx}] command matches denylist: {argv_hit}",
                    intent_type=intent.type,
                )
            command_text = " ".join(step.argv or [])
            hit = matches_denylist(command_text, ctx.denylist)
            if hit:
                raise IntentValidationError(
                    f"step[{idx}] command matches denylist: {hit}",
                    intent_type=intent.type,
                )
        return intent

    return intent


__all__ = [
    "ACTION_ID_RE",
    "ALIAS_RE",
    "INTENT_TYPES",
    "Intent",
    "IntentValidationError",
    "PlanStep",
    "RISK_LEVELS",
    "ValidationContext",
    "build_validation_context",
    "compile_denylist",
    "matches_denylist",
    "parse_intent",
    "revalidate_intent",
]
