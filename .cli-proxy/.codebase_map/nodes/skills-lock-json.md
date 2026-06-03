# Node: skills-lock.json

Generated: 2026-06-03T02:24:29Z

## Purpose
`skills-lock.json` — корневой lockfile установленных агент-скиллов: для каждого скилла фиксирует источник (`source`), тип источника (`sourceType`) и контентный хеш (`computedHash`). Сейчас одна запись — скилл `shadcn` из GitHub `shadcn/ui` (`skills-lock.json:4-7`); поле верхнего уровня `version: 1` задаёт схему lock-файла. Файл намеренно версионируется (tracked) как контракт реестра скиллов.

## Scope
- Source glob: `skills-lock.json` (только корневой файл).
- Estimated files: 1.
- Управляется внешним инструментарием установки скиллов (skill-installer / реестр `shadcn`), а не Python-кодом репозитория: прямых reader/writer в `app/`, `modes/`, `agent/` нет (`rg -n skills-lock` находит только сам файл и `tests/test_runtime_artifacts_policy.py`).
- Не путать с рантайм-сервисами скилл-политики бота (`app/services/skill_registry_service.py`, `app/services/skill_policy_service.py`, `app/services/skill_runtime_service.py`, опции `skill_*` в конфиге) — это отдельный механизм, он не читает и не пишет этот lockfile.

## Instructions for agent
- Inspect `skills-lock.json` before changing the lock: read the current entries (`source`/`sourceType`/`computedHash`) and confirm ownership before adding, removing, or bumping a pinned skill.
- Не редактировать `computedHash`/`source` вручную — значения генерирует инструментарий скиллов; правки делать его командами, иначе хеш разойдётся с контентом.
- Сохранять файл tracked: он намеренно исключён из runtime-ignore (`docs/runtime-artifacts-policy.md`), `git check-ignore --no-index skills-lock.json` обязан давать пустой вывод.
- Держать структуру схемы `version: 1` (`version` + map `skills` с полями `source`/`sourceType`/`computedHash`); менять формат только при осознанном апгрейде версии.
- После правок прогонять `pytest -q tests/test_runtime_artifacts_policy.py` — он фиксирует, что файл остаётся в индексе и не игнорируется глобально.

## Source of truth
- `skills-lock.json` (repo root) — сам lock-файл и единственный источник пинов скиллов.
- `.cli-proxy/.codebase_map/INDEX.md:64` — трекает узел с `source_glob: skills-lock.json`.
- `docs/runtime-artifacts-policy.md:63,88,110` — классифицирует файл как намеренный repository lockfile (решение Task_16) и обязывает держать его в индексе.
- `tests/test_runtime_artifacts_policy.py:67,97,111` — контракт: файл не игнорируется глобально и остаётся единственным tracked среди спорных артефактов; строка `97` ссылается на строку 15 этого узла как на evidence.

## When to update
- Любой коммит, добавляющий/удаляющий/обновляющий запись скилла или меняющий `computedHash`/`source`/`version`.
- Любое изменение классификации файла в `docs/runtime-artifacts-policy.md` или его контракта в `tests/test_runtime_artifacts_policy.py`.
- Любое архитектурное или поведенческое изменение, затрагивающее эту область.

## Related nodes
- `nodes/app.md` — рантайм скилл-сервисы бота (`app/services/skill_*`); смежная, но ОТДЕЛЬНАЯ от этого lockfile область.
- `nodes/tests.md` — `tests/test_runtime_artifacts_policy.py`, фиксирующий контракт tracked/ignore для этого файла.
- `nodes/config-example-yaml.md` — конфиг-поверхность `skill_*` (политика скиллов бота), не путать с этим lock-файлом.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enriched: инвентаризация схемы/записи, семантика внешне-управляемого lockfile, классификация runtime-artifacts-policy/Task_16, тест-контракт; сверено с `skills-lock.json`, `.cli-proxy/.codebase_map/INDEX.md`, `docs/runtime-artifacts-policy.md`, `tests/test_runtime_artifacts_policy.py`)
