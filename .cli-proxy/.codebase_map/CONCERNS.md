# Concerns

Обновлено: 2026-05-01. Основа: `rg --files` по основным source/test area, чтение `.cli-proxy/.codebase_map/INDEX.md`, core map docs/nodes, `modes/DEVELOPMENT.md` и выборочное чтение runtime-файлов.

## Карта кодовой базы

- `.cli-proxy/.codebase_map/INDEX.md`, `STRUCTURE.md` и area `nodes/*.md` синхронизированы с текущим `rg --files`; drift counts теперь должен ловить `tests/test_codebase_map_counts.py` с tolerance 0.
- `.cli-proxy/.codebase_map/api/` неполно отражает source tree. Примеры текущих source-файлов без очевидного API-зеркала: `app/services/skill_runtime_service.py`, `app/services/run_operations_service.py`, `app/services/input_dispatch_service.py`, `modes/sdk/orchestrator_runner.py`, `desktop/widgets/session_settings.py`. Для точных выводов опираться на source-файлы.

## Архитектурная связность

- `modes/DEVELOPMENT.md` требует mode-код через `BaseMode`/SDK-сервисы, но реализации все еще передают и читают `bot_app`: `modes/agent/mode.py`, `modes/analyst/mode.py`, `modes/manager/mode.py`, `modes/admin/mode.py`, `modes/codebase_mapper/mode.py`. Риск: новая mode-логика легко привязывается к Telegram/BotApp и сложнее синхронизируется с Desktop/MiniApp.
- Desktop fake `BotApp` в `desktop/services/application_facade.py` зафиксирован как legacy compatibility shim, не extension point; public surface охраняется тестом `test_desktop_bot_app_legacy_adapter_public_surface_is_allowlisted`. Риск остается для старого mode-кода, но новые Desktop-facing возможности должны идти через SDK services/dependencies.
- Messaging/output остается переходным слоем: `modes/sdk/services/messaging.py` абстрагирует transport, но прямые/private send-пути остаются в `tg/handlers.py`, `tg/message_processor.py`, `sessions/session_run_service.py`, `sessions/session_output_service.py`, `desktop/services/application_facade.py`. Риск: расхождение форматирования, thread routing и доставки после behavioral changes.
- Config/runtime изменения проходят через несколько поверхностей: `config.py`, `app/config_runtime/models.py`, `app/config_runtime/loader.py`, `app/config_runtime/serialization.py`, `miniapp/services/config_service.py`, `miniapp/static/app.js`, `desktop/widgets/config_editor.py`, `README.md`, `README_EN.MD`. Риск: частичное обновление config keys создает drift между runtime, UI и документацией.
- Shared runtime state пересекает `bot.py`, `app/bootstrap.py`, `session.py`, `sessions/*`, `app/services/*`, `modes/sdk/*`. Изменения в этих слоях требуют более широких targeted tests, чем один локальный unit-тест.

## Ошибки и наблюдаемость

- В operational paths есть silent `except Exception: pass`: `sessions/session_run_service.py`, `sessions/session_output_service.py`, `app/services/session_service.py`, `app/services/run_operations_service.py`, `app/services/git_ops_service.py`, `app/services/logging_service.py`, `modes/codebase_mapper/runtime.py`, `modes/admin/transports/local.py`, `modes/admin/transports/ssh.py`, `desktop/widgets/session_manager.py`. Риск: production failure выглядит как зависшее UI/state без диагностического лога.
- Существующие исключения `md2=False` остаются в `modes/sdk/services/input_routing.py` и `modes/admin/mode.py`. Новый Telegram output не должен копировать этот паттерн без проверки `tg/markdown.py` и entities pipeline.
- JSON parsing централизован в `modes/sdk/runtime/json_normalizer.py` и широко используется: `modes/sdk/orchestrator_runner.py`, `modes/manager/services/execution_service.py`, `modes/analyst/mode.py`, `modes/webmaster/mode.py`, `modes/admin/analyzer.py`, `app/services/cli_json_stream.py`, `miniapp/routes.py`. Любая правка normalizer имеет большой blast radius.

## Repository hygiene

- Git отслеживает state/runtime-like артефакты: `SESSION.json`, `miniapp.pid`, `full_ui.yaml`, `skills-lock.json`. Их изменения надо считать намеренными, а не случайным test/runtime output.
- `miniapp/package.json` есть, но активного JS build pipeline не видно; MiniApp behavior живет напрямую в `miniapp/static/app.js` и требует browser/playwright проверки при UI-правках.

## Ограничения

- Тесты, линтер и runtime не запускались: задача была статическим обновлением map-документа.
- Скан файлов был полным, но чтение source было выборочным; список рисков не является исчерпывающим.
