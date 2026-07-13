# Node: app

Generated: 2026-06-17T10:46:18Z

## Purpose
Instruction node for `app` area.

## Scope
- Source glob: `app/**`
- Estimated files: 163
- Current files: 163 under `app/**` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `app/**`
- `app/__init__.py`
- `app/config_runtime/__init__.py`
- `app/events/__init__.py`
- `app/security/__init__.py`
- `app/services/__init__.py`
- `app/services/lint_evolution/schemas/classification_v1.json`
- `app/services/lint_evolution/schemas/decision_weights.yaml`
- `app/bootstrap.py`
- `app/config_runtime/adapter.py`
- `app/config_runtime/field_paths.py`

## Module API
Детальные интерфейсы модулей этой области:

- [app/services/__init__.py](../api/app/services/__init__-py.md)
- [app/bootstrap.py](../api/app/bootstrap-py.md)
- [app/config_runtime/adapter.py](../api/app/config_runtime/adapter-py.md)
- [app/config_runtime/loader.py](../api/app/config_runtime/loader-py.md)
- [app/config_runtime/models.py](../api/app/config_runtime/models-py.md)
- [app/config_runtime/serialization.py](../api/app/config_runtime/serialization-py.md)
- [app/events/bus.py](../api/app/events/bus-py.md)
- [app/mode_dependencies.py](../api/app/mode_dependencies-py.md)
- [app/security/audit.py](../api/app/security/audit-py.md)
- [app/security/auth.py](../api/app/security/auth-py.md)
- [app/security/errors.py](../api/app/security/errors-py.md)
- [app/security/facade.py](../api/app/security/facade-py.md)
- [app/security/interfaces.py](../api/app/security/interfaces-py.md)
- [app/security/rate_limits.py](../api/app/security/rate_limits-py.md)
- [app/security/validators.py](../api/app/security/validators-py.md)
- [app/services/access_policy_service.py](../api/app/services/access_policy_service-py.md)
- [app/services/actor_identity.py](../api/app/services/actor_identity-py.md)

## When to update
- Any commit touching `app/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any commit touching `desktop/**` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/i18n.md`
- `nodes/locales.md`
- `nodes/miniapp.md`
- `agent` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.95 via L0/L2
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.94 via L0
- `desktop` confidence=0.95 via L0
- `i18n` confidence=0.90 via L0/L1/L2
- `locales` confidence=0.95 via L0
- `miniapp` confidence=0.95 via L0/L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-07-13T10:46:52Z
