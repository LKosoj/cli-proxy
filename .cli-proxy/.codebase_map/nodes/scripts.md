# Node: scripts

Generated: 2026-06-03T02:24:29Z

## Purpose
Операционные (provisioning) shell-скрипты для подготовки хоста под запуск CLI Proxy. Это не код приложения — скрипты выполняются с правами root и настраивают системное окружение. Сейчас в каталоге один скрипт — `scripts/setup-claude-bot.sh`, который создаёт системного пользователя `claude-bot`, общую группу и устанавливает Claude CLI.

## Scope
- Source glob: `scripts/**`
- Файлы (1): `scripts/setup-claude-bot.sh`
- Граница: host-provisioning, запуск `sudo`/root, bash (`set -e`). Не содержит Python и бизнес-логики режимов.

## Instructions for agent
- Перед правками прочитать `scripts/setup-claude-bot.sh` целиком; это root-скрипт с `set -e`.
- Сохранять идемпотентность проверок (`id`, `getent group`, `command -v`) — повторный запуск не должен ломать состояние.
- Не хардкодить секреты: `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` дописываются в `~/.bashrc` пользователя.
- Проверка после правок: `bash -n scripts/setup-claude-bot.sh` (синтаксис), при наличии — `shellcheck`. Pytest-тестов для этой области нет.
- Сообщения вывода — на русском, с цветовыми кодами; держать единый стиль echo.

## Source of truth
- `scripts/setup-claude-bot.sh`

Ключевое поведение (проверено по коду):
- Создаёт пользователя (по умолчанию `claude-bot`) и группу `cli-proxy-workgroup`, выставляет setgid и `g+rwxs` на WORKDIR (по умолчанию `/srv/git_projects`).
- Ставит Claude CLI через `curl -fsSL https://claude.ai/install.sh | bash`, добавляет `~/.local/bin` в PATH.
- Аргументы: `--workdir`, `--username`, `--version`.
- Финальный шаг подсказывает запуск бота: `python <WORKDIR>/cli-proxy/bot.py`.

## When to update
- Любой коммит, затрагивающий `scripts/**`.
- Изменения provisioning-логики: пользователь/группа/права, поток установки Claude CLI, набор env-переменных, дефолтные `--workdir`/`--username`.
- Изменение пути/способа запуска бота, на который ссылается скрипт (`bot.py`).

## Related nodes
- `nodes/setup-bot-sh.md` — родственный provisioning-скрипт `setup_bot.sh` в корне репозитория.
- `nodes/bot-py.md` — конечный шаг скрипта запускает `bot.py`.
- В `graph.json` рёбра для `scripts` не объявлены (узел изолирован по графу).

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03T02:24:29Z
