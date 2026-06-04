# Node: app

Generated: 2026-06-03T02:24:29Z

## Purpose
Service Layer (слой 2 архитектуры): транспорт-агностичная бизнес-логика, DI-обвязка режимов, типизированный config runtime, безопасность и шина событий. Не содержит транспортного кода (`tg/`, `desktop/`, `miniapp/`) и не реализует режимы (`modes/`); потребляется из `bot.py` и SDK-сервисов.

Подпакеты:
- `app/services/` — 90+ сервисов (CLI-стримы и лимиты, git/SSH-операции, скиллы, планировщик, сессии, runtime-наблюдаемость и т.д.); плюс `lint_evolution/` (эволюция lint-правил) и `session_transfer/` (перенос сессий между CLI-агентами Codex/Gemini/Qwen/Claude/Grok).
- `app/config_runtime/` — Pydantic-модели конфига, загрузчик с ENV-оверрайдами, адаптер в `AppConfig`, сериализация YAML.
- `app/security/` — `SecurityFacade` поверх auth/audit/rate-limits/validators.
- `app/events/` — `SystemEventBus` и типы событий.
- `app/bootstrap.py`, `app/mode_dependencies.py` — composition: сборка сервисов и `ModeDependencies` для DI в режимы.

## Scope
- Source glob: `app/**`
- Current files: 154 under `app/**` as of last review.
- Корневые модули: `app/bootstrap.py`, `app/mode_dependencies.py`.
- Подпакеты: `app/services/` (+ `app/services/lint_evolution/`, `app/services/session_transfer/`), `app/config_runtime/`, `app/security/`, `app/events/`.

## Instructions for agent
- Read only files relevant to the active task; сервисов >90 — не загружать всё.
- Импорты в `app/services/__init__.py` ленивые (PEP 562 `__getattr__`): не добавлять жадных импортов подмодулей — это создаёт цикл `config -> app.services -> ... -> config`.
- Сервисы транспорт-агностичны: не тянуть сюда зависимости из `tg/`, `desktop/`, `miniapp/`; режимы получают сервисы через `app/mode_dependencies.py`, а не напрямую.
- Публичные интерфейсы пакетов `config_runtime`/`security`/`events` экспортируются через их `__init__.py` (`__all__`) — обновлять при добавлении классов.
- Prefer deterministic checks before edits. Keep changes minimal and validate with `pytest -q` and `flake8 .`.

## Source of truth
Код — единственный источник истины; ниже точки входа для каждого подпакета:
- `app/bootstrap.py` — сборка сервисов (composition).
- `app/mode_dependencies.py` — построение `ModeDependencies` для DI в режимы.
- `app/services/__init__.py` — ленивый реестр публичных сервисов (`__getattr__`, PEP 562).
- `app/config_runtime/__init__.py` → `models.py`, `loader.py`, `adapter.py`, `serialization.py`, `field_paths.py`.
- `app/security/__init__.py` → `facade.py`, `auth.py`, `audit.py`, `rate_limits.py`, `validators.py`, `interfaces.py`, `errors.py`.
- `app/events/__init__.py` → `bus.py` (`SystemEventBus` и типы событий).
- `app/services/lint_evolution/` (`README.md`, `schemas/classification_v1.json`, `schemas/decision_weights.yaml`).
- `app/services/session_transfer/` (`service.py`, `capsule.py`, `reader_*.py`, `writer_*.py`).

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
- [app/services/admin_config_service.py](../api/app/services/admin_config_service-py.md)
- [app/services/advanced_orchestrator_service.py](../api/app/services/advanced_orchestrator_service-py.md)
- [app/services/app_runtime_service.py](../api/app/services/app_runtime_service-py.md)
- [app/services/artifact_intent_service.py](../api/app/services/artifact_intent_service-py.md)
- [app/services/assistant_preview_service.py](../api/app/services/assistant_preview_service-py.md)
- [app/services/claude_env_checker.py](../api/app/services/claude_env_checker-py.md)
- [app/services/claude_jsonl_monitor.py](../api/app/services/claude_jsonl_monitor-py.md)
- [app/services/cli_dialog_logger.py](../api/app/services/cli_dialog_logger-py.md)
- [app/services/cli_json_stream.py](../api/app/services/cli_json_stream-py.md)
- [app/services/cli_limits_service.py](../api/app/services/cli_limits_service-py.md)
- [app/services/config_apply_policy.py](../api/app/services/config_apply_policy-py.md)
- [app/services/config_service.py](../api/app/services/config_service-py.md)

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
- `nodes/miniapp.md`
- `nodes/modes.md`
- `agent` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.90 via L0/L2
- `config.py` confidence=0.90 via L0/L2
- `config_example.yaml` confidence=0.95 via L0
- `desktop` confidence=0.76 via L0
- `i18n` confidence=0.90 via L1/L2
- `miniapp` confidence=0.95 via L0/L1/L2
- `modes` confidence=0.95 via L0/L1/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enriched: Purpose/Scope/Instructions/Source of truth)
