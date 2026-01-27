# CLI Proxy Telegram Bridge

[English version](README_EN.MD) | [Русская версия](README.md)

Telegram bot for managing CLI agents (Codex / Gemini / Qwen / Claude) with multiple sessions, request queueing, HTML output, and cached tool help (/commands).

## Features
- Multiple CLI sessions in different directories.
- Request queue and busy control.
- Output always sent as an HTML file (ANSI colors preserved).
- Short preview: OpenAI summary or first 4000 chars.
- Prompt and resume token autodetection, saved in `state.json`.
- Active session restore after restart.
- Inline menus for sessions, directories, and commands.
- “Use this directory” button in the directory picker.
- “git clone” button to clone a repo into the chosen directory.
- Cached tool help in `toolhelp.json`.

## Quick start
1) Install dependencies:
```bash
pip install -r requirements.txt
```

2) Fill `config.yaml`:
- `telegram.token`
- `telegram.whitelist_chat_ids`
- `defaults.workdir`
- optionally `openai_*` for CLI output summaries

3) Run the bot:
```bash
python bot.py
```

## Configuration
`config.yaml` supports:
- `tools.*`: launch commands, mode, prompt/resume/help
- `defaults.*`: base dir, timeouts, state/toolhelp paths, OpenAI settings

Per-tool environment variables:
```yaml
tools:
  codex:
    env:
      OPENAI_API_KEY: "..."
      OPENAI_MODEL: "..."
```
These variables are added to the CLI process environment.

`${VAR}` expansions are supported.
`null` values are ignored.

## YOLO/auto‑approve mode
Auto-approvals enabled in `config.yaml`:
- **Gemini CLI**: `--approval-mode yolo`
- **Qwen Code**: `--yolo`
- **Claude Code**: `--dangerously-skip-permissions`
- **Codex**: sends `/approvals full` in interactive mode

OpenAI can be set via `config.yaml` or env:
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_BASE_URL`

Environment variables take precedence.

## Bot commands
- `/tools` — list tools
- `/new` — create a session (inline menu)
- `/sessions` — list sessions
- `/use` — set active session (menu)
- `/close` — close a session (menu)
- `/status` — active session status
- `/interrupt` — interrupt generation
- `/queue` — show queue
- `/clearqueue` — clear queue for active session
- `/dirs` — browse directories (menu)
- `/gitclone` — clone repo into a directory (menu)
- `/cwd <path>` — create session in a directory
- `/rename <id> <name>` or `/rename <name>` — rename session
- `/resume [token]` — show/set resume token
- `/state [tool path]` — state by tool+path or menu
- `/toolhelp` — show tool /commands
- `/commands` — commands menu
- `/menu` — menu hint (menu is via Telegram button)
- `/start` — show menu hint
- `/send <text>` — send text directly to CLI
- `/purge <id>` — delete session from saved state

## State
- `state.json` — last point (resume + summary) for tool+workdir
- `state.json` — stores last active session (tool+workdir)
- `state.json` — stores sessions list (name, queue) for restart restore
- `toolhelp.json` — cached /help per tool

## Tests
```bash
pytest -q
```

## Direct CLI input
If you need to send a line starting with `/`, use either:
- `/send /help`
- or prefix `>` in a normal message: `> /help`
