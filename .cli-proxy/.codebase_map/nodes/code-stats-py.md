# Node: code_stats.py

Generated: 2026-06-03T02:24:29Z

## Purpose
`code_stats.py` — автономный dev-утилитарный CLI-скрипт для подсчёта объёма кодовой базы. Обходит дерево проекта (`main()` `code_stats.py:252`), собирает файлы по категориям расширений (`get_file_extensions` `code_stats.py:48`: Python, JS/TS, YAML/JSON, Shell, Markdown), считает количество файлов, общее число строк, строк кода (без пустых и комментариев — `count_lines` `code_stats.py:59`) и размер, после чего печатает таблицу с долями или JSON (`--json`). Python разделяется на `main`/`tests` по наличию `tests` в пути. Запуск: `python code_stats.py [path] [-v|--verbose] [--json]`; не часть рантайма бота.

## Scope
- Source glob: `code_stats.py`
- Estimated files: 1
- Только подсчёт статистики. Не импортируется приложением и не влияет на поведение бота/desktop/miniapp; не имеет import/call-зависимостей на другие узлы графа.
- Внутренние границы: исключения путей задаёт `get_exclude_patterns` (`code_stats.py:32`: `.venv`, `__pycache__`, `.git`, `logs`, `.cli-proxy`, `.mypy_cache`, `session_ticks`, `node_modules`, `dist`, `build`); фильтрация директорий/файлов — `collect_files` (`code_stats.py:159`), `should_exclude` (`code_stats.py:132`).

## Instructions for agent
- Перед утверждениями о поведении проверять конкретную функцию в `code_stats.py` и ссылаться на `code_stats.py:<строка>` (зеркало API: `.cli-proxy/.codebase_map/api/code_stats-py.md`).
- Менять список категорий/расширений — только в `get_file_extensions` (`code_stats.py:48`); список исключений — в `get_exclude_patterns` (`code_stats.py:32`). Логику фильтрации в `collect_files`/`should_exclude` держать согласованной с этими наборами.
- Зависит только от стандартной библиотеки (`os`, `argparse`, `pathlib`, `dataclasses`, `typing`) — не добавлять зависимостей от модулей приложения, чтобы сохранить автономность скрипта.
- Факт: dataclass'ы `FileStats` (`code_stats.py:15`) и `TypeStats` (`code_stats.py:24`) объявлены, но не используются — агрегация в `main()` идёт через dict/кортежи. Это существующий мёртвый код; не удалять в рамках несвязанных правок (см. global CLAUDE.md §3).
- Тестов на этот скрипт нет; проверять правки прогоном вручную (`python code_stats.py .` и `python code_stats.py . --json`). Соблюдать `flake8 .`.

## Source of truth
- `code_stats.py` — единственный файл узла.
- Зеркало публичного API: `.cli-proxy/.codebase_map/api/code_stats-py.md`.
- Внешних зависимостей нет (только stdlib); потребителей модуля в репозитории нет — скрипт запускается напрямую как `__main__` (`code_stats.py:413`).

## When to update
- Любой коммит, затрагивающий `code_stats.py`.
- Изменение состава категорий расширений или паттернов исключения (`get_file_extensions`, `get_exclude_patterns`).
- Изменение CLI-интерфейса (`argparse`-аргументы в `main()` `code_stats.py:252`) или формата вывода (таблица/JSON).
- Любое поведенческое изменение логики подсчёта (`count_lines`, `collect_files`, `analyze_files`).

## Module API
Детальные интерфейсы модулей этой области:

- [code_stats.py](../api/code_stats-py.md)

## Related nodes
- (none) — автономный скрипт без import/call-рёбер к другим узлам графа.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
