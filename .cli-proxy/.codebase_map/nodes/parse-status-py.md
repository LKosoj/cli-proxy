# Node: parse_status.py

Generated: 2026-06-03T02:24:29Z

## Purpose
`parse_status.py` — одноразовый dev-/отладочный скрипт в корне репозитория (9 строк). Открывает `miniapp/routes.py` относительным путём, одним `re.search` (`parse_status.py:5`, флаги `re.DOTALL | re.MULTILINE`, паттерн `def _extract_active_session_payload.*?return \{.*?"resume_tokens"`) пытается вырезать тело функции до ключа `"resume_tokens"` в собираемом payload и печатает найденный блок либо `Could not find the payload dict` (`parse_status.py:9`). Не часть рантайма бота/desktop/miniapp.

## Scope
- Source glob: `parse_status.py`
- Estimated files: 1
- Только разовое чтение и regex-извлечение из `miniapp/routes.py`; ничего не пишет и не изменяет.
- Зависит лишь от stdlib (`re`). Целевой путь захардкожен относительно (`open('miniapp/routes.py')`, `parse_status.py:2`) → запускать строго из корня репозитория.

## Instructions for agent
- Факт (проверено `2026-06-03`): искомого символа `_extract_active_session_payload` в `miniapp/routes.py` нет — текущий payload с ключом `"resume_tokens"` (`miniapp/routes.py:1613`) собирается в методе `MiniAppRoutes._build_session_payload` (`miniapp/routes.py:1226`, return-dict с `parse_status.py`-стороны на `miniapp/routes.py:1586`). Поэтому скрипт как написан уйдёт в ветку «Could not find the payload dict». Это устаревший scratch-скрипт.
- Перед утверждениями о поведении проверять `parse_status.py:<строка>` и сверять regex с реальной структурой `miniapp/routes.py`.
- Не «чинить» и не расширять скрипт в рамках несвязанных правок (см. global CLAUDE.md §3). Если он мешает — предложить удаление пользователю, не делать это молча.
- Тестов на скрипт нет; проверка только ручным прогоном `python parse_status.py` из корня репозитория. Соблюдать `flake8 .`.

## Source of truth
- `parse_status.py` — единственный файл узла.
- Входные данные читаются из `miniapp/routes.py` (узел [miniapp](miniapp.md)); API-зеркала для самого скрипта нет.
- Внешних зависимостей нет (только stdlib `re`); потребителей модуля в репозитории нет — `grep parse_status` находит только этот файл.

## When to update
- Любой коммит, затрагивающий `parse_status.py`.
- Переименование/удаление функции сборки payload в `miniapp/routes.py` (`_build_session_payload`) или ключа `"resume_tokens"`, на которые завязан regex.
- Удаление скрипта (тогда удалить и этот узел, и строку в `INDEX.md`).

## Related nodes
- [miniapp](miniapp.md) — источник входных данных (`miniapp/routes.py`). Только file-read, без import/call-рёбер.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
