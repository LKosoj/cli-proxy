from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

from .errors import DenyReasonCode, SecurityValidationError
from .interfaces import PathValidationResult


class BasicValidatorService:
    def __init__(
        self,
        *,
        max_text_length: int | None = None,
        deny_substrings: Sequence[str] = (),
        user_max_text_length: Mapping[str, int] | None = None,
        required_context_keys: Sequence[str] = (),
        default_deny_names: Sequence[str] = (),
        default_deny_extensions: Sequence[str] = (),
        roots_by_user: Mapping[str, str] | None = None,
        roots_by_session: Mapping[str, str] | None = None,
        text_rules: Mapping[str, Any] | None = None,
        path_rules: Mapping[str, Any] | None = None,
    ) -> None:
        self._max_text_length = int(max_text_length) if max_text_length else None
        self._deny_substrings = tuple(str(item) for item in deny_substrings if str(item or ""))
        self._user_max_text_length = {str(k): int(v) for k, v in (user_max_text_length or {}).items()}
        self._required_context_keys = tuple(str(item) for item in required_context_keys if str(item or ""))
        self._default_deny_names = tuple(str(item) for item in default_deny_names if str(item or ""))
        self._default_deny_extensions = tuple(str(item) for item in default_deny_extensions if str(item or ""))
        self._roots_by_user = {str(k): str(v) for k, v in (roots_by_user or {}).items()}
        self._roots_by_session = {str(k): str(v) for k, v in (roots_by_session or {}).items()}
        self._text_rules = dict(text_rules or {})
        self._path_rules = dict(path_rules or {})

    def require_text(
        self,
        value: str,
        *,
        field_name: str = "input",
        context: Mapping[str, Any] | None = None,
    ) -> str:
        ctx = dict(context or {})
        text = str(value or "").strip()
        token_field = str(field_name or "input")
        if not text:
            raise SecurityValidationError(
                DenyReasonCode.REQUIRED_FIELD,
                f"{token_field} is required",
                details={"field_name": token_field},
            )

        for key in self._required_context_keys:
            if ctx.get(key) in (None, ""):
                raise SecurityValidationError(
                    DenyReasonCode.REQUIRED_CONTEXT,
                    f"context.{key} is required",
                    details={"context_key": key, "field_name": token_field},
                )

        max_length = self._effective_max_text_length(ctx)
        if max_length is not None and len(text) > max_length:
            raise SecurityValidationError(
                DenyReasonCode.MAX_LENGTH_EXCEEDED,
                f"{token_field} exceeds max_length={max_length}",
                details={"field_name": token_field, "max_length": max_length, "length": len(text)},
            )

        for token in self._deny_substrings:
            if token and token in text:
                raise SecurityValidationError(
                    DenyReasonCode.DENIED_SUBSTRING,
                    f"{token_field} contains denied substring {token!r}",
                    details={"field_name": token_field, "substring": token},
                )

        for rule_name, rule in self._text_rules.items():
            self._apply_rule(
                rule,
                payload=text,
                rule_name=rule_name,
                field_name=token_field,
                context=ctx,
            )

        return text

    def resolve_path(
        self,
        root: str,
        rel_path: str,
        *,
        deny_names: Sequence[str] = (),
        deny_extensions: Sequence[str] = (),
        context: Mapping[str, Any] | None = None,
    ) -> PathValidationResult:
        ctx = dict(context or {})
        effective_root = self._effective_root(root, ctx)
        root_real = os.path.realpath(str(effective_root or "").strip())
        if not root_real:
            raise SecurityValidationError(
                DenyReasonCode.ROOT_REQUIRED,
                "root is required",
            )

        raw_rel = str(rel_path or "").strip()
        if raw_rel in {"", ".", "/"}:
            resolved = root_real
        else:
            normalized = raw_rel.replace("\\", "/").lstrip("/")
            resolved = os.path.realpath(os.path.join(root_real, normalized))

        if not (resolved == root_real or resolved.startswith(root_real + os.sep)):
            raise SecurityValidationError(
                DenyReasonCode.PATH_ESCAPES_ROOT,
                "path escapes root",
                details={"root": root_real, "input_path": raw_rel},
            )

        base_name = os.path.basename(resolved).lower()
        protected_names = self._normalized_names((*self._default_deny_names, *deny_names))
        if base_name in protected_names:
            raise SecurityValidationError(
                DenyReasonCode.PROTECTED_NAME,
                "path targets protected name",
                details={"base_name": base_name},
            )

        protected_exts = self._normalized_extensions((*self._default_deny_extensions, *deny_extensions))
        if any(base_name.endswith(ext) for ext in protected_exts):
            raise SecurityValidationError(
                DenyReasonCode.PROTECTED_EXTENSION,
                "path targets protected extension",
                details={"base_name": base_name, "extensions": protected_exts},
            )

        protected_paths = {
            os.path.realpath(str(item or "").strip())
            for item in (ctx.get("protected_paths") or ())
            if str(item or "").strip()
        }
        if resolved in protected_paths:
            raise SecurityValidationError(
                DenyReasonCode.PROTECTED_PATH,
                "path targets protected path",
                details={"resolved_path": resolved},
            )

        protected_prefixes = tuple(
            str(item or "").strip().lower()
            for item in (ctx.get("protected_name_prefixes") or ())
            if str(item or "").strip()
        )
        for prefix in protected_prefixes:
            if base_name.startswith(prefix):
                raise SecurityValidationError(
                    DenyReasonCode.PROTECTED_NAME,
                    "path targets protected name prefix",
                    details={"base_name": base_name, "prefix": prefix},
                )

        relative_path = "." if resolved == root_real else os.path.relpath(resolved, root_real).replace("\\", "/")
        result = PathValidationResult(
            root=root_real,
            input_path=raw_rel,
            resolved_path=resolved,
            relative_path=relative_path,
        )

        for rule_name, rule in self._path_rules.items():
            self._apply_rule(
                rule,
                payload=result,
                rule_name=rule_name,
                field_name="path",
                context=ctx,
            )

        return result

    def _effective_max_text_length(self, context: Mapping[str, Any]) -> int | None:
        subject_keys = self._context_subject_keys(context)
        for key in subject_keys:
            if key in self._user_max_text_length:
                return int(self._user_max_text_length[key])
        return self._max_text_length

    def _effective_root(self, root: str, context: Mapping[str, Any]) -> str:
        session_id = str(context.get("session_id") or "").strip()
        if session_id and session_id in self._roots_by_session:
            return self._roots_by_session[session_id]
        for key in self._context_subject_keys(context):
            if key in self._roots_by_user:
                return self._roots_by_user[key]
        return str(root or "")

    @staticmethod
    def _context_subject_keys(context: Mapping[str, Any]) -> tuple[str, ...]:
        keys: list[str] = []
        for token in ("user_id", "chat_id"):
            value = context.get(token)
            if value in (None, ""):
                continue
            keys.append(str(value))
        return tuple(keys)

    @staticmethod
    def _normalized_names(values: Sequence[str]) -> set[str]:
        return {str(name or "").strip().lower() for name in values if str(name or "").strip()}

    @staticmethod
    def _normalized_extensions(values: Sequence[str]) -> tuple[str, ...]:
        result = []
        for ext in values:
            token = str(ext or "").strip().lower()
            if not token:
                continue
            result.append(token if token.startswith(".") else f".{token}")
        return tuple(result)

    @staticmethod
    def _apply_rule(
        rule: Any,
        *,
        payload: Any,
        rule_name: str,
        field_name: str,
        context: Mapping[str, Any],
    ) -> None:
        try:
            result = rule(payload, field_name=field_name, context=context)
        except TypeError:
            result = rule(payload)
        except SecurityValidationError:
            raise
        except Exception as exc:
            raise SecurityValidationError(
                DenyReasonCode.VALIDATION_RULE_FAILED,
                f"validation rule {rule_name} failed: {exc}",
                details={"rule_name": rule_name, "field_name": field_name, "error": str(exc)},
            ) from exc

        if result is False:
            raise SecurityValidationError(
                DenyReasonCode.VALIDATION_RULE_REJECTED,
                f"validation rule {rule_name} rejected {field_name}",
                details={"rule_name": rule_name, "field_name": field_name},
            )
        if isinstance(result, str) and result.strip():
            raise SecurityValidationError(
                DenyReasonCode.VALIDATION_RULE_REJECTED,
                result.strip(),
                details={"rule_name": rule_name, "field_name": field_name},
            )


def build_validator_service(
    validator_config: Mapping[str, Any] | None,
    *,
    custom_text_rules: Mapping[str, Any] | None = None,
    custom_path_rules: Mapping[str, Any] | None = None,
) -> BasicValidatorService:
    config = dict(validator_config or {})
    text_cfg = dict(config.get("text") or {})
    path_cfg = dict(config.get("path") or {})

    text_rule_registry = dict(custom_text_rules or {})
    path_rule_registry = dict(custom_path_rules or {})

    enabled_text_rule_names = tuple(text_cfg.get("enabled_rules") or ())
    enabled_path_rule_names = tuple(path_cfg.get("enabled_rules") or ())
    missing_text_rules = [name for name in enabled_text_rule_names if name not in text_rule_registry]
    missing_path_rules = [name for name in enabled_path_rule_names if name not in path_rule_registry]
    if missing_text_rules:
        joined = ", ".join(sorted(str(name) for name in missing_text_rules))
        raise SecurityValidationError(
            DenyReasonCode.UNKNOWN_TEXT_VALIDATION_RULES,
            f"unknown text validation rules: {joined}",
            details={"missing_rules": tuple(sorted(str(name) for name in missing_text_rules))},
        )
    if missing_path_rules:
        joined = ", ".join(sorted(str(name) for name in missing_path_rules))
        raise SecurityValidationError(
            DenyReasonCode.UNKNOWN_PATH_VALIDATION_RULES,
            f"unknown path validation rules: {joined}",
            details={"missing_rules": tuple(sorted(str(name) for name in missing_path_rules))},
        )

    enabled_text_rules = {
        name: text_rule_registry[name]
        for name in enabled_text_rule_names
    }
    enabled_path_rules = {
        name: path_rule_registry[name]
        for name in enabled_path_rule_names
    }

    return BasicValidatorService(
        max_text_length=int(text_cfg["max_length"]) if text_cfg.get("max_length") else None,
        deny_substrings=tuple(text_cfg.get("deny_substrings") or ()),
        user_max_text_length=dict(text_cfg.get("user_max_length") or {}),
        required_context_keys=tuple(text_cfg.get("required_context_keys") or ()),
        default_deny_names=tuple(path_cfg.get("deny_names") or ()),
        default_deny_extensions=tuple(path_cfg.get("deny_extensions") or ()),
        roots_by_user=dict(path_cfg.get("roots_by_user") or {}),
        roots_by_session=dict(path_cfg.get("roots_by_session") or {}),
        text_rules=enabled_text_rules,
        path_rules=enabled_path_rules,
    )


__all__ = ["BasicValidatorService", "SecurityValidationError", "build_validator_service"]
