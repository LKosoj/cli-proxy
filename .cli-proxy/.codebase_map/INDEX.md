# Codebase Mapper Instruction Graph

Generated: 2026-06-03T02:24:29Z

This index is the entrypoint for agent instructions.

## Mandatory Workflow
1. Before any edits, read this `INDEX.md` completely.
2. Determine relevant area(s) and open matching files under `.cli-proxy/.codebase_map/nodes/*.md`.
3. Only then inspect source files and implement changes.
4. After changes, update affected node metadata (`When to update`, `Last reviewed`).
5. If node update fails, run targeted repair for that node.

## Runtime Verification and Fallback Policy (Hardcoded)
- Перед любым утверждением о runtime-поведении ОБЯЗАТЕЛЬНО проверить конкретный метод/функцию в коде и сослаться на файл:строка.
- Запрещено делать выводы по аналогии между этапами пайплайна без прямой проверки каждого этапа (decompose/dev/review/final audit).
- Если вопрос про «кто/когда вызывается», отвечать в формате пошаговой цепочки: шаг -> метод -> исполнитель -> зачем.
- При обнаружении своей неточности сначала коротко исправить факт, затем дать проверенные ссылки на код, без догадок.
- Policy matrix по fallback:
- Legacy-потоки (уже существующее поведение в проде): fallback разрешён для обратной совместимости, но должен логироваться и быть явно отражён в отчёте.
- Новый функционал и новые mode-сценарии: fallback запрещён по умолчанию; при ошибке — явный fail с причиной.
- Opt-in fallback: разрешён только после явного согласования с пользователем в текущей задаче или если он явно приходит как требование от пользователя.

## Runtime Files
- `graph.json`: topology and edges.
- `rules.yaml`: update routing rules.
- `state.json`: statuses/queues (`ok|needs_repair|degraded|invalid`).
- `api/`: optional technical interface mirror.

## Core Docs
These files are mandatory context and must be considered before major edits.
- `STACK.md`: Технологический стек, зависимости, рантаймы и инфраструктурные маркеры.
- `INTEGRATIONS.md`: Внешние/внутренние интеграции, точки входа и контракты взаимодействий.
- `ARCHITECTURE.md`: Архитектурная структура модулей, слои и их ответственность.
- `STRUCTURE.md`: Физическая структура репозитория и индексация значимых путей.
- `CONVENTIONS.md`: Кодовые конвенции, практики и стандарты реализации.
- `TESTING.md`: Подход к тестированию, расположение тестов и проверочные правила.
- `CONCERNS.md`: Риски, технический долг и зоны повышенного внимания.

## Nodes
- [tests](nodes/tests.md) - files: 526, source_glob: `tests/**`
- [modes](nodes/modes.md) - files: 205, source_glob: `modes/**`
- [app](nodes/app.md) - files: 152, source_glob: `app/**`
- [agent](nodes/agent.md) - files: 60, source_glob: `agent/**`
- [desktop](nodes/desktop.md) - files: 31, source_glob: `desktop/**`
- [miniapp](nodes/miniapp.md) - files: 19, source_glob: `miniapp/**`
- [tg](nodes/tg.md) - files: 17, source_glob: `tg/**`
- [sessions](nodes/sessions.md) - files: 10, source_glob: `sessions/**`
- [utils](nodes/utils.md) - files: 8, source_glob: `utils/**`
- [i18n](nodes/i18n.md) - files: 5, source_glob: `i18n/**`
- [locales](nodes/locales.md) - files: 4, source_glob: `locales/**`
- [bot.py](nodes/bot-py.md) - files: 1, source_glob: `bot.py`
- [code_stats.py](nodes/code-stats-py.md) - files: 1, source_glob: `code_stats.py`
- [config.py](nodes/config-py.md) - files: 1, source_glob: `config.py`
- [config_example.yaml](nodes/config-example-yaml.md) - files: 1, source_glob: `config_example.yaml`
- [conftest.py](nodes/conftest-py.md) - files: 1, source_glob: `conftest.py`
- [gen_init_data.py](nodes/gen-init-data-py.md) - files: 1, source_glob: `gen_init_data.py`
- [parse_status.py](nodes/parse-status-py.md) - files: 1, source_glob: `parse_status.py`
- [pytest.ini](nodes/pytest-ini.md) - files: 1, source_glob: `pytest.ini`
- [requirements.txt](nodes/requirements-txt.md) - files: 1, source_glob: `requirements.txt`
- [scripts](nodes/scripts.md) - files: 1, source_glob: `scripts/**`
- [session.py](nodes/session-py.md) - files: 1, source_glob: `session.py`
- [setup_bot.sh](nodes/setup-bot-sh.md) - files: 1, source_glob: `setup_bot.sh`
- [skills-lock.json](nodes/skills-lock-json.md) - files: 1, source_glob: `skills-lock.json`
- [start_miniapp.py](nodes/start-miniapp-py.md) - files: 1, source_glob: `start_miniapp.py`
- [summary.py](nodes/summary-py.md) - files: 1, source_glob: `summary.py`

## Runtime Inputs
- map_dir: `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map`
- changed_files: 0
