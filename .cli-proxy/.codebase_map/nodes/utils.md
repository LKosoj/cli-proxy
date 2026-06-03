# Node: utils

Generated: 2026-06-03T02:24:29Z

## Purpose
Низкоуровневый leaf-пакет транспорт- и режим-агностичных хелперов: сборка argv CLI-команд, детект prompt/resume-маркеров в выводе CLI, очистка/нормализация ANSI-текста, рендер текста/Markdown/ANSI в HTML, пути sandbox и `.cli-proxy`, форматирование заголовков/лейблов сессий, безопасный запуск корутин, резолв языка для фоновых задач и сборка/валидация source-артефактов. Не содержит бизнес-логики режимов; импортируется широко (≈77 файлов: `tg/`, `desktop/`, `miniapp/`, `app/services/`, `agent/`, `modes/`, `sessions/`, `bot.py`, `session.py`, `summary.py`, тесты).

## Scope
- Source glob: `utils/**`
- Files: 8 (`utils/*.py`, ~1100 строк), пакет помечен `utils/__init__.py`.
- Публичный фасад — `utils/__init__.py` (`__all__`): реэкспорт из `cli`, `html_renderer`, `paths`, `text`, `ui`.
- НЕ реэкспортируются через `__init__`: `utils/lang.py` и `utils/source_artifact.py` — импортировать напрямую (`from utils.lang import resolve_user_lang`, `from utils.source_artifact import ...`).

## Instructions for agent
- Read only files relevant to the active task; не грузить всю область целиком.
- `utils/cli.py` — построение CLI-вызова: `build_command(cmd_template, prompt, resume, image) -> (argv, use_stdin)` раскрывает плейсхолдеры `{prompt}`/`{resume}`/`{image}` и снимает `--continue`/`--resume`, когда `resume is None`; `use_stdin=True`, если `{prompt}` не найден. `detect_resume_regex` ищет `thread_id`/`conversation_id`/`session_id`/`resume id`/`--resume`. Менять только согласованно с шаблонами команд CLI в `config.yaml`/`config_example.yaml` и контрактами `modes/sdk/runtime/cli_contracts.py`.
- `utils/text.py` — единственный источник ANSI-логики: `strip_ansi`/`strip_ansi_codes`/`has_ansi`, `normalize_text` (снятие ANSI + удаление блока `mcp:`-строк + дедуп повторяющихся блоков), `extract_tick_tokens`, `build_preview` (обрезка с суффиксом `...(обрезано)...`). Не дублировать regex в других модулях — переиспользовать.
- `utils/html_renderer.py` — `render_html`/`ansi_to_html`/`render_markdown`/`make_html_file`. Markdown через `markdown-it`, ANSI→HTML с allowlist тегов; mermaid-блоки рендерятся сетевым запросом к `mermaid.ink` ТОЛЬКО при `allow_network_fetch=True` (в Qt/desktop-потоке флаг не выставляется во избежание фриза). Зависит от `tg.markdown.to_markdown_v2`.
- `utils/paths.py` — пути sandbox (`sandbox_root`/`sandbox_shared_dir`/`sandbox_session_dir`/`legacy_sandbox_session_dir`), `cli_proxy_root`/`cli_proxy_artifact_path`, `is_within_root` (containment-проверка через `realpath`+`commonpath`). Токен сессии санитизируется через `sessions.scoped_key.sanitize_scoped_key_token` — формат менять только согласованно с `nodes/sessions.md`.
- `utils/ui.py` — `format_session_title`/`format_session_label`/`format_session_selector_label`, `status_dot`, `ensure_async` (планирует корутину только на запущенном loop, трекает `parent._background_tasks`, логирует и закрывает незапланированную корутину). Доступ к атрибутам сессии — через локальные `_direct_attr`/`_has_attr`, не падать на отсутствующих полях.
- `utils/lang.py` — `resolve_user_lang(config, user_id, chat_id)`; читает `config.telegram.user_languages`, `config.defaults.default_language`, сверяет с `i18n.resolver.SUPPORTED_LANGS`, fallback `"ru"`. НЕ авто-детектит из Telegram `language_code` (это делается на inbound-границе).
- `utils/source_artifact.py` — CLI и API сборки/валидации `source-*.zip`: `build_source_artifact`/`inspect_source_artifact`/`validate_source_artifact`, списки `SOURCE_ARTIFACT_INCLUDE`/`REQUIRED_ARTIFACT_MEMBERS`/`FORBIDDEN_ARTIFACT_MEMBERS`. При изменении верхнеуровневой структуры репозитория синхронизировать эти кортежи.
- Prefer deterministic checks before edits; держать изменения минимальными и валидировать `pytest -q` и `flake8 .`.

## Source of truth
- `utils/__init__.py` — публичный фасад (`__all__`).
- `utils/cli.py` — `build_command`, `detect_prompt_regex`, `detect_resume_regex`, `resolve_env_value`.
- `utils/text.py` — ANSI/нормализация/превью.
- `utils/html_renderer.py` — рендер HTML/Markdown/ANSI, mermaid.
- `utils/paths.py` — sandbox- и `.cli-proxy`-пути, `is_within_root`.
- `utils/ui.py` — лейблы сессий, `status_dot`, `ensure_async`.
- `utils/lang.py` — `resolve_user_lang` (не в `__init__`).
- `utils/source_artifact.py` — сборка/валидация source-артефактов (не в `__init__`).

## Module API
Детальные интерфейсы модулей этой области:

- [utils/cli.py](../api/utils/cli-py.md)
- [utils/html_renderer.py](../api/utils/html_renderer-py.md)
- [utils/lang.py](../api/utils/lang-py.md)
- [utils/paths.py](../api/utils/paths-py.md)
- [utils/source_artifact.py](../api/utils/source_artifact-py.md)
- [utils/text.py](../api/utils/text-py.md)
- [utils/ui.py](../api/utils/ui-py.md)

## When to update
- Любой коммит, затрагивающий `utils/**`.
- Изменения upstream-контрактов, на которые `utils` опирается напрямую: `sessions/scoped_key.py` (`paths.py`), `tg/markdown.py` (`html_renderer.py`), `i18n/resolver.py` (`SUPPORTED_LANGS` в `lang.py`).
- Изменения схемы конфигурации, читаемой `utils`: поля `telegram.user_languages`/`defaults.default_language` в `config.py`, шаблоны CLI-команд в `config.yaml`/`config_example.yaml` (влияют на `build_command`/`resolve_env_value`).
- Изменение верхнеуровневой структуры репозитория или smoke-тестов — синхронизировать `SOURCE_ARTIFACT_INCLUDE`/`REQUIRED_ARTIFACT_MEMBERS` в `source_artifact.py`.
- Любое архитектурное или поведенческое изменение в этой области.

## Related nodes
Upstream (импортируются из `utils`):
- `nodes/sessions.md` — `sessions/scoped_key.py` (санитизация токена пути).
- `nodes/tg.md` — `tg/markdown.py` (`to_markdown_v2`).
- `nodes/i18n.md` — `i18n/resolver.py` (`SUPPORTED_LANGS`).
- `nodes/config-py.md`, `nodes/config-example-yaml.md` — схема и значения конфигурации.

Downstream (основные потребители `utils`):
- `nodes/desktop.md`, `nodes/miniapp.md`, `nodes/tg.md`, `nodes/app.md`, `nodes/agent.md`, `nodes/modes.md`, `nodes/sessions.md`, `nodes/bot-py.md`, `nodes/session-py.md`, `nodes/summary-py.md`.

Тесты области: `tests/test_utils_ui.py`, `tests/test_build_command_resume_image.py`, `tests/test_html_renderer.py`, `tests/test_html_render_quotes.py`, `tests/test_html_render_headings.py`, `tests/smoke/test_source_artifact_smoke.py`.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
