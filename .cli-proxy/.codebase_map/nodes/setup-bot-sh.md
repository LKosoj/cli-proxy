# Node: setup_bot.sh

Generated: 2026-06-03T02:24:29Z

## Purpose
`setup_bot.sh` — интерактивный установщик первого запуска CLI Proxy Telegram Bridge для Ubuntu/Debian (bash, `set -euo pipefail`). За один прогон: ставит системные пакеты через `apt-get` (включая `python3-venv`, `nodejs`, `npm`, `ripgrep`, `setup_bot.sh:115-127`), создаёт venv в `.venv` и ставит зависимости из `requirements.txt` (`setup_bot.sh:129-132`), устанавливает CLI-инструменты codex/claude/gemini/qwen через `npm -g` и grok через официальный установщик xAI (`setup_bot.sh:134-176`), собирает обязательные параметры, генерирует `config.yaml` из `config_example.yaml` и `.env`, затем создаёт и запускает systemd-сервис (`setup_bot.sh:382-434`). Запуск: `./setup_bot.sh` (интерактивно) или `./setup_bot.sh --non-interactive ...` (CI/headless).

## Scope
- Source glob: `setup_bot.sh`
- Estimated files: 1
- Скрипт сборки/деплоя, не часть рантайма бота. Бизнес-логику не содержит — только bootstrap окружения и инсталляцию.

## Instructions for agent
- Перед утверждениями о поведении проверять конкретную строку и ссылаться на `setup_bot.sh:<строка>`.
- Флаги и env-переменные парсятся в `usage()`/`while`-цикле (`setup_bot.sh:23-76`). При добавлении опции синхронизировать три места: парсер `case`, текст `usage()` и блок required-проверок (`setup_bot.sh:193-220`, `:250-254`).
- `config.yaml` собирается встроенным Python-heredoc (`setup_bot.sh:290-363`) поверх загруженного `config_example.yaml`. Любой новый ключ в конфиге, который должен задаваться установщиком, добавлять и в `config_example.yaml`, и в этот heredoc.
- Записываемые ключи конфига конкретны: `telegram.token`, `telegram.whitelist_chat_ids`, `telegram.admlist_chat_ids`, `telegram.user_workdirs`, `defaults.workdir`, `defaults.openai_*`, `defaults.zai_api_key`, `defaults.tavily_api_key`, `defaults.jina_api_key`. Инвариант: админы — подмножество whitelist (`setup_bot.sh:315-318`).
- systemd-юнит генерируется инлайн (`setup_bot.sh:394-414`): `ExecStart=$VENV_DIR/bin/python $REPO_DIR/bot.py`. При переименовании entrypoint `bot.py` или смене venv-пути править здесь.
- Секреты пишутся в `.env` с `chmod 600` (`setup_bot.sh:365-380`) — не логировать значения ключей, не понижать права.

## Source of truth
- `setup_bot.sh` — единственный файл узла.
- Прямые файловые зависимости скрипта:
  - `config_example.yaml` — шаблон-источник для генерации `config.yaml` (`setup_bot.sh:18`, `:295-299`).
  - `requirements.txt` — список pip-зависимостей для venv (`setup_bot.sh:132`).
  - `bot.py` — процессный entrypoint в `ExecStart` systemd-юнита (`setup_bot.sh:408`).
- Артефакты, создаваемые скриптом (не в git): `config.yaml`, `.env`, `.venv/`, `/etc/systemd/system/<service>.service`.

## When to update
- Любой коммит, затрагивающий `setup_bot.sh`.
- Изменение структуры `config_example.yaml` в секциях `telegram.*` / `defaults.*`, которые заполняет heredoc (`setup_bot.sh:341-359`).
- Изменение `requirements.txt`, ломающее установку в venv, либо смена набора системных пакетов apt.
- Переименование/перенос entrypoint `bot.py` или изменение запускаемой команды сервиса.
- Изменение состава устанавливаемых CLI (codex/claude/gemini/qwen/grok) или их npm-пакетов.

## Related nodes
- `nodes/config-example-yaml.md` — шаблон конфига, который читается и материализуется в `config.yaml`.
- `nodes/requirements-txt.md` — pip-зависимости, ставящиеся в `.venv`.
- `nodes/bot-py.md` — entrypoint, запускаемый сгенерированным systemd-сервисом.
- `nodes/config-py.md` — схема `config.yaml`, валидирующая то, что пишет установщик.
- L0-эвристика маппера также связывала узел с `nodes/agent.md`, `nodes/app.md`, `nodes/miniapp.md`, `nodes/session-py.md`, `nodes/tests.md` (confidence=0.76). Это косвенные связи через рантайм бота, а не прямые import/call-зависимости скрипта.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03
