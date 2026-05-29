# Testing Guidelines

## Test Stack

- Runner: `pytest` (`requirements.txt`).
- Async: `pytest-asyncio`; `pytest.ini` задает `asyncio_default_fixture_loop_scope = function` и регистрирует marker `asyncio`.
- Parallel dependency: `pytest-xdist` есть в `requirements.txt`, но базовая команда проекта остается `.venv/bin/pytest -q`.
- Qt/Desktop: `pytest-qt`, `PySide6`, `qasync`; корневой `conftest.py` выставляет `QT_QPA_PLATFORM=offscreen`, software OpenGL/backend и session fixture `qapp_args`.
- MiniApp/web: `aiohttp` test server и `playwright-cli` используются в `tests/test_miniapp_playwright.py`; для разработки web/MiniApp обязательно проверять через playwright-cli.

## Commands

```bash
.venv/bin/pytest -q tests/test_file.py::test_name
.venv/bin/pytest -q tests/test_file.py
.venv/bin/pytest -q tests/test_area_*.py
.venv/bin/flake8
```

- По умолчанию запускать targeted tests для измененных модулей и ближайших integration paths.
- Полный `.venv/bin/pytest -q` нужен при release/smoke-check или изменениях общих/shared слоев: `modes/sdk/runtime/*`, `modes/sdk/services/*`, `app/services/*`, `config.py`, `bot.py`, `session.py`.
- Для smoke entrypoints есть `tests/smoke/*`; не подменять ими targeted unit/integration проверки.

## Test Layout

- Основной каталог: `tests/**` (483 файла по текущему `rg --files`).
- Smoke tests: `tests/smoke/test_*_smoke.py` и helper `tests/smoke/_smoke_support.py`.
- Корневые fixtures: `conftest.py` для Qt/offscreen и лимита файловых дескрипторов.
- Test-local fixtures: `tests/conftest.py` добавляет repo root в `sys.path`, патчит `BotApp.__init__` для tracking и после каждого теста сбрасывает BotApp async services и `ToolRegistry` singleton.
- Async tests широко используются: текущий скан нашел `@pytest.mark.asyncio` в 90 test-файлах.

## Target Selection

- Runtime/LLM JSON: `tests/test_json_normalizer.py`, `tests/test_json_parsing_unification.py`, `tests/test_cli_contracts.py`, `tests/test_ask_user_schema.py`.
- Modes: `tests/test_agent_mode_plugin.py`, `tests/test_analyst_mode_plugin.py`, `tests/test_manager_mode_plugin.py`, `tests/test_webmaster_mode_plugin.py`, `tests/test_modes_basic_flows_integration.py`.
- Manager orchestration: `tests/test_manager_*`, `tests/test_orchestrator_*`, `tests/test_run_artifacts_integration.py`.
- App services: обычно `tests/test_<service_name>.py` рядом по имени с `app/services/<service_name>.py`.
- Telegram routing/formatting: `tests/test_tg_handlers.py`, `tests/test_telegram_thread_routing.py`, `tests/test_markdown_v2_send_message.py`, `tests/test_telegram_outbound_thread_routing.py`.
- Desktop: `tests/test_desktop_*.py`, widget-specific tests вроде `tests/test_config_editor.py`, `tests/test_task_queue_widget.py`, `tests/test_report_viewer.py`.
- MiniApp: `tests/test_miniapp_*.py`, `tests/test_miniapp_app_js.py`, `tests/test_miniapp_playwright.py`, route/service tests under the same prefix.
- Config changes: `tests/test_config_models.py`, `tests/test_config_loader.py`, `tests/test_config_serialization.py`, `tests/test_config_adapter.py`, `tests/test_miniapp_config_service.py`, `tests/test_config_service_feature_flags.py`, `tests/test_readme_feature_flags_sync.py`.
- Security/rate limits/auth: `tests/test_security_*.py`, `tests/test_telegram_ingress_security.py`, `tests/test_webhook_ingress_service.py`.

## Quality Expectations

- Для bugfix сначала добавить или обновить тест, который воспроизводит проблему, затем чинить код.
- Для новых/измененных config keys проверять sync `config.yaml`/`config_example.yaml`/MiniApp/Desktop UI и соответствующие tests.
- Для новых mode behaviors покрывать callback dispatch, busy/queue semantics, task cancellation, invalid payload/fallback and handoff result.
- Для новых `except`-веток проверять, что ошибка логируется (`logger.exception`) и не теряется контекст.
- Для Telegram output проверять entities/MarkdownV2 pipeline, а не только plain text.

## Ограничения скана

- Обновлено по полному `rg --files` и выборочному чтению `pytest.ini`, `requirements.txt`, `conftest.py`, `tests/conftest.py`, smoke/MiniApp/validation examples; тесты и линтер не запускались, потому что задача была только на обновление map-документов.
