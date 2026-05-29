# Node: setup_bot.sh

Generated: 2026-04-27T22:43:23Z

## Purpose
Instruction node for `/srv/git_projects/cli-proxy/setup_bot.sh`, the Ubuntu/Debian first-time setup script for CLI Proxy Telegram Bridge.
The script installs OS/Python/npm dependencies, collects required bot/OpenAI settings, writes local runtime config files, and creates/enables a systemd service for `/srv/git_projects/cli-proxy/bot.py`.

## Scope
- Source glob: `/srv/git_projects/cli-proxy/setup_bot.sh`
- Estimated files: 1
- Covers argument/env parsing, interactive prompts, dependency installation, virtualenv setup, config generation, `.env` generation, systemd unit creation, service start, and optional ownership changes.
- Reads `/srv/git_projects/cli-proxy/config_example.yaml` and `/srv/git_projects/cli-proxy/requirements.txt`.
- Writes `/srv/git_projects/cli-proxy/config.yaml`, `/srv/git_projects/cli-proxy/.env`, and `/srv/git_projects/cli-proxy/.venv/`.
- Does not define bot runtime behavior; service execution enters `/srv/git_projects/cli-proxy/bot.py`.

## Instructions for agent
- Before changing setup behavior, read `/srv/git_projects/cli-proxy/setup_bot.sh` and the target files it reads or writes.
- Keep edits shellcheck-friendly Bash with `set -euo pipefail`; preserve interactive and `--non-interactive` flows.
- If adding or changing generated config keys, update both `/srv/git_projects/cli-proxy/config.yaml` and `/srv/git_projects/cli-proxy/config_example.yaml`, then check MiniApp config editor impact.
- Do not hardcode secrets or commit generated local files such as `/srv/git_projects/cli-proxy/.env`.
- Prefer targeted validation: `bash -n /srv/git_projects/cli-proxy/setup_bot.sh` for syntax, plus focused tests for any touched Python/config consumers.

## Source of truth
- `/srv/git_projects/cli-proxy/setup_bot.sh`
- `/srv/git_projects/cli-proxy/config_example.yaml` for default config structure consumed by the script.
- `/srv/git_projects/cli-proxy/requirements.txt` for Python dependencies installed into `/srv/git_projects/cli-proxy/.venv/`.

## When to update
- Any commit touching `/srv/git_projects/cli-proxy/setup_bot.sh`.
- Any change to setup arguments, supported `SETUP_*` env vars, required API keys, generated `config.yaml`/`.env` fields, dependency installation, or systemd unit contents.
- Any change to `/srv/git_projects/cli-proxy/config_example.yaml` or `/srv/git_projects/cli-proxy/requirements.txt` that changes first-time setup expectations.

## Related nodes
- `config-example-yaml.md`
- `requirements-txt.md`
- `bot-py.md`

## Last reviewed
- 2026-04-27T23:38:42Z
