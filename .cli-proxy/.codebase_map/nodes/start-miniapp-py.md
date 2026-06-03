# Node: start_miniapp.py

Generated: 2026-06-03T02:24:29Z

## Purpose
`start_miniapp.py` — самостоятельный dev/диагностический entrypoint для локального запуска **только** HTTP-поверхности MiniApp, без Telegram-поллинга. Корутина `main()` (`start_miniapp.py:9`) настраивает root-логирование в stdout, грузит конфиг через `load_config("config.yaml")` (`config.py`, импорт `start_miniapp.py:6`), принудительно включает MiniApp (`config.miniapp.enabled = True`, `start_miniapp.py:20`), создаёт `BotApp(config)` (`bot.py`, импорт `start_miniapp.py:5`) и поднимает общий aiohttp-листенер `bot_app.shared_http_ingress` (`start_miniapp.py:32`). После старта печатает `http://127.0.0.1:8088/cli-proxy/` (`start_miniapp.py:34`) и удерживает процесс бесконечным `asyncio.sleep` (`start_miniapp.py:37-38`). Запуск процесса — `python start_miniapp.py` через guard `__main__` → `asyncio.run(main())` (`start_miniapp.py:41-49`).

## Scope
- Source glob: `start_miniapp.py`
- Estimated files: 1
- Узел — тонкий launcher: не содержит бизнес-логики и реализации MiniApp. Сама поверхность — `miniapp/**` и `app/services/shared_http_ingress.py`; полная сборка приложения и production-entrypoint — `bot.py` (`main()` `bot.py:2748`).
- В отличие от `python bot.py`, этот скрипт НЕ регистрирует Telegram-хендлеры и не запускает поллинг — только `shared_http_ingress.start()`.

## Instructions for agent
- Перед утверждениями о runtime-поведении проверять конкретные строки в `start_miniapp.py` и связанных файлах; не делать выводов по аналогии с `bot.py`.
- `bot_app.shared_http_ingress` всегда устанавливается в `BotApp.__init__` (`bot.py:189`, из `self.container.shared_http_ingress`). Поэтому fallback-ветка `if not hasattr(...)` (`start_miniapp.py:27-30`), вручную конструирующая `SharedHttpIngress(host=..., port=...)`, при текущем `BotApp` недостижима — учитывать это и не дублировать конфигурацию хоста/порта здесь.
- Конструктор `SharedHttpIngress.__init__` принимает только keyword-only `host`/`port` (`app/services/shared_http_ingress.py:21-28`); fallback-вызов на `start_miniapp.py:30` должен сохранять именованные аргументы.
- Хост/порт по умолчанию (`127.0.0.1:8088`) дублируют `DEFAULT_HOST`/`DEFAULT_PORT` (`app/services/shared_http_ingress.py:15-16`); базовая конфигурация поверхности живёт в `config.miniapp` (`config.py`) и `SharedHttpIngress.from_config` (`app/services/shared_http_ingress.py:40`).
- Скрипт диагностический — менять его, только если меняется контракт `BotApp`/`SharedHttpIngress`; основную логику править в `bot.py`, `app/services/shared_http_ingress.py`, `miniapp/**`.
- После правок прогонять `pytest -q` (смоки `tests/smoke/test_miniapp_server_smoke.py`, `tests/smoke/test_startup_smoke.py`) и `flake8 .`; держать изменения минимальными.

## Source of truth
- `start_miniapp.py` — единственный файл узла.
- Зеркало публичного API: `.cli-proxy/.codebase_map/api/start_miniapp-py.md`.
- Прямые зависимости (импорты `start_miniapp.py:5-6`, плюс lazy-импорт `start_miniapp.py:28`):
  - `bot.py` — класс `BotApp` (`bot.py:112`), атрибут `shared_http_ingress` (`bot.py:189`).
  - `config.py` — `load_config`; поле `config.miniapp`.
  - `app/services/shared_http_ingress.py` — `SharedHttpIngress` (`:12`), `.start()`, `from_config` (`:40`).

## When to update
- Любой коммит, затрагивающий `start_miniapp.py`.
- Любой коммит в `app/**` (особенно `app/services/shared_http_ingress.py`), `bot.py`, `config.py` — узел имеет import/call-зависимость от них.
- Изменение сигнатуры `SharedHttpIngress.__init__` / `from_config` или способа установки `BotApp.shared_http_ingress` (`bot.py:189`).
- Изменение хоста/порта/пути MiniApp (`127.0.0.1:8088/cli-proxy/`) или флага `config.miniapp.enabled`.
- Любое архитектурное или поведенческое изменение в этой области.

## Module API
Детальные интерфейсы модулей этой области:

- [start_miniapp.py](../api/start_miniapp-py.md)

## Related nodes
- `nodes/bot-py.md` — `BotApp` и его атрибут `shared_http_ingress`, confidence=0.90 via L2.
- `nodes/config-py.md` — `load_config`, `config.miniapp`, confidence=0.90 via L2.
- `nodes/app.md` — `app/services/shared_http_ingress.py` (`SharedHttpIngress`), confidence=0.90 via L1/L2.
- `nodes/miniapp.md` — поднимаемая поверхность MiniApp (`miniapp/**`), purpose-level (логическая связь, не прямой импорт).

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
