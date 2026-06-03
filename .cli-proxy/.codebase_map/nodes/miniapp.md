# Node: miniapp

Generated: 2026-06-03T02:24:29Z

## Purpose
Транспортный слой: aiohttp-веб-интерфейс администратора/оператора, открываемый как Telegram Mini App. Бизнес-логики не содержит — делегирует в `app/services/*`, SDK-сервисы и mode-плагины. Сервер `miniapp/server.py` (`MiniAppServer`) монтируется на общий ingress (`app/services/shared_http_ingress.py`) под `config.miniapp.base_path` (по умолчанию `/cli-proxy`); создаётся в `bot.py:280`, запускается/останавливается из `app/services/lifecycle_service.py:36`/`:99`. Standalone-запуск для разработки — `start_miniapp.py` (форсирует `config.miniapp.enabled=True`, отдаёт на `http://127.0.0.1:8088/cli-proxy/`).

## Scope
- Source glob: `miniapp/**`
- Estimated files: 19
- Подкаталоги: `miniapp/services/` (бекенд-сервисы UI: config draft, файлы, логи), `miniapp/static/` (SPA на vanilla JS: `index.html`, `app.js`, `styles.css`).

## Instructions for agent
- Это транспортный слой: бизнес-логику не размещать здесь; маршрутизировать через `app/services/*`, SDK-сервисы и mode-плагины (`modes/`). Доступ к `BotApp` — только как к контейнеру сервисов.
- Доступ fail-closed: каждый хендлер начинается с `_require_access`/`_require_admin` (`miniapp/routes.py`). Поток: `_extract_init_data` (header `X-Telegram-Init-Data`) → `bot_app.security.authenticate(strategy="telegram_init_data")` → rate limit `miniapp.ingress` → `authorize(scope="miniapp")`; админ-эндпоинты дополнительно `scope="miniapp.admin"`. Подпись initData проверяется в `miniapp/auth.py` (`verify_telegram_init_data`, HMAC-SHA256).
- WebSocket-эндпоинты (`/api/status/ws`, логи) аутентифицируются короткоживущими HMAC-тикетами (`_issue_ws_ticket`/`_consume_ws_ticket`, TTL 60с), а не initData напрямую.
- Новые эндпоинты добавлять как `routes_<area>.py`: dataclass `<Area>RouteServices` + `register_<area>_routes(app, ctx, services)`, общие зависимости через `MiniAppRouteContext` (`miniapp/route_context.py`); регистрировать в `MiniAppRoutes.register` (`miniapp/routes.py:4522`).
- Синхронизировать функциональность с ботом (`tg/`) и Desktop (`desktop/`) — общий контракт сервисов (требование `CLAUDE.md`).
- Новые опции конфигурации добавлять и в `config.yaml`, и в `config_example.yaml`; секреты в ответах редактировать (см. `miniapp/services/config_service.py`).
- Для локального тестирования auth-пути генерировать валидный `initData` через `gen_init_data.py` (та же схема HMAC `WebAppData`/SHA256, что и `miniapp/auth.py:verify_telegram_init_data`) и передавать в заголовке `X-Telegram-Init-Data`.
- Читать только файлы под задачу (`miniapp/routes.py` ~4600 строк, `miniapp/static/app.js` ~7200 строк — точечно через Grep); изменения держать минимальными и проверять `pytest -q tests/test_miniapp_routes_integration.py tests/test_shared_http_ingress.py`.

## Source of truth
- `miniapp/server.py` — `MiniAppServer`, монтаж на shared ingress, `_runtime_guard` (gate по `enabled`/`base_path`), `start`/`stop`.
- `miniapp/auth.py` — `verify_telegram_init_data`, `TelegramMiniAppUser`, `MiniAppAuthError`.
- `miniapp/routes.py` — `MiniAppRoutes`: композиция submodule-роутов, гварды доступа, WS-тикеты, сериализация runs/sessions; `register()` на строке 4522.
- `miniapp/route_context.py` — `MiniAppRouteContext` (общие зависимости route-модулей).
- `miniapp/routes_config.py` — config view/draft/validate/save (`ConfigRouteServices`).
- `miniapp/routes_admin.py` — админ-конфиг и операции (`AdminRouteServices`).
- `miniapp/routes_scheduler.py` — планировщик задач (`SchedulerRouteServices`).
- `miniapp/routes_logs.py` — чтение/стрим логов, WS (`LogsRouteServices`).
- `miniapp/routes_ssh.py` — SSH-хосты/секреты (`SshRouteServices`).
- `miniapp/routes_json.py` — парсинг/валидация JSON-тел (`JsonRouteServices`).
- `miniapp/routes_foundation.py` — шаблон route-модуля (`FoundationRouteServices`).
- `miniapp/services/config_service.py` — валидация/диф/редактирование секретов config-черновика.
- `miniapp/services/files_service.py` — compat-реэкспорт `app/services/session_files_service.py`.
- `miniapp/services/logs_service.py` — чтение и парсинг логов сессий.
- `miniapp/static/index.html`, `miniapp/static/app.js`, `miniapp/static/styles.css` — SPA на vanilla JS; вкладки: config, files, logs, status, scheduler, settings, admin. Внешние зависимости с CDN: Telegram WebApp JS SDK (`telegram.org/js/telegram-web-app.js`) и Ace editor `1.43.6` (jsdelivr).
- `start_miniapp.py` — standalone dev-лаунчер.

## Module API
Детальные интерфейсы модулей этой области:

- [miniapp/auth.py](../api/miniapp/auth-py.md)
- [miniapp/route_context.py](../api/miniapp/route_context-py.md)
- [miniapp/routes.py](../api/miniapp/routes-py.md)
- [miniapp/routes_admin.py](../api/miniapp/routes_admin-py.md)
- [miniapp/routes_config.py](../api/miniapp/routes_config-py.md)
- [miniapp/routes_foundation.py](../api/miniapp/routes_foundation-py.md)
- [miniapp/routes_json.py](../api/miniapp/routes_json-py.md)
- [miniapp/routes_logs.py](../api/miniapp/routes_logs-py.md)
- [miniapp/routes_scheduler.py](../api/miniapp/routes_scheduler-py.md)
- [miniapp/routes_ssh.py](../api/miniapp/routes_ssh-py.md)
- [miniapp/server.py](../api/miniapp/server-py.md)

## When to update
- Any commit touching `miniapp/**`.
- Any commit touching `agent/**` because this node has import/call dependency on it.
- Any commit touching `app/**` because this node has import/call dependency on it.
- Any commit touching `bot.py` because this node has import/call dependency on it.
- Any commit touching `config.py` because this node has import/call dependency on it.
- Any commit touching `config_example.yaml` because this node has import/call dependency on it.
- Any architecture or behavior change affecting this area.

## Related nodes
- `nodes/agent.md`
- `nodes/app.md`
- `nodes/bot-py.md`
- `nodes/config-py.md`
- `nodes/config-example-yaml.md`
- `nodes/desktop.md`
- `nodes/modes.md`
- `nodes/session-py.md`
- `agent` confidence=0.95 via L0
- `app` confidence=0.95 via L0/L1/L2
- `bot.py` confidence=0.76 via L0
- `config.py` confidence=0.90 via L2
- `config_example.yaml` confidence=0.89 via L0
- `desktop` confidence=0.76 via L0
- `modes` confidence=0.90 via L0/L1/L2
- `session.py` confidence=0.95 via L0/L2

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03T02:37:00Z
