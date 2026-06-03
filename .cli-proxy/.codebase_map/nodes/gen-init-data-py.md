# Node: gen_init_data.py

Generated: 2026-06-03T02:24:29Z

## Purpose
Standalone dev/CLI-хелпер, который генерирует подписанную строку Telegram WebApp `initData` для заданных `bot_token` и `user_id`. Используется вручную для получения значения заголовка `X-Telegram-Init-Data`, чтобы дёргать аутентифицированные MiniApp-эндпоинты без реального Telegram-клиента.

## Scope
- Source glob: `gen_init_data.py`
- Estimated files: 1
- Содержит одну публичную функцию `build_init_data(bot_token, user_id) -> str` (`gen_init_data.py:9`) и CLI-точку входа `if __name__ == "__main__"` (`gen_init_data.py:26`).

## Instructions for agent
- Это вспомогательный скрипт, не часть рантайма бота/MiniApp; в продакшен-пути он не импортируется.
- Алгоритм подписи ОБЯЗАН совпадать с верификатором `miniapp/auth.py:verify_telegram_init_data` (`miniapp/auth.py:22`): тот же data-check-string (отсортированные `key=value` через `\n`) и тот же секрет `HMAC_SHA256("WebAppData", bot_token)` (`gen_init_data.py:15-17` ↔ `miniapp/auth.py:43-45`). При изменении одной стороны проверить и синхронизировать вторую.
- Поля фиксированы: `query_id="q1"`, `username`/`first_name` синтезируются из `user_id` (`gen_init_data.py:13`). Менять только при изменении контракта верификатора.
- Read only files relevant to the active task. Prefer deterministic checks before edits. Keep changes minimal and validate with tests/linters where applicable.

## Source of truth
- `gen_init_data.py` — реализация генератора `initData`.
- `miniapp/auth.py` — верификатор `initData` (контракт, которому должен соответствовать генератор).

## Module API
Детальные интерфейсы модулей этой области:

- [gen_init_data.py](../api/gen_init_data-py.md)

## When to update
- Any commit touching `gen_init_data.py`.
- Любое изменение схемы подписи/полей в `miniapp/auth.py` (`verify_telegram_init_data`), т.к. генератор должен оставаться зеркальным.
- Изменение тестового хелпера `_build_init_data`, который переизобретает эту же логику локально (например `tests/test_miniapp_auth.py:22`, `tests/test_miniapp_routes_integration.py:29`) — проверить рассинхрон.

## Related nodes
- `nodes/miniapp.md` — `miniapp/auth.py` (верификатор-counterpart) и `miniapp/routes.py` (потребитель заголовка `X-Telegram-Init-Data`).
- `nodes/app.md` — `app/security/auth.py`, `app/security/errors.py` (security-слой вокруг initData).
- `nodes/tests.md` — множество тестов с локальным `_build_init_data`, повторяющим этот скрипт.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
