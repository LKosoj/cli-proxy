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
- Inline Git menu (status/fetch/pull ff-only/merge/rebase/diff/log/stash/commit/push) for the active session.
- Merge/rebase conflict handling with diff/abort/continue/call-agent buttons.
- Cached tool help in `toolhelp.json`.
- Separate MTProto menu: task goes to CLI, result is sent to the chosen chat.
- Send files from the working directory via `/files`.
- Task templates (hidden `/preset` command).

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
- `tools.*`: launch commands, mode, prompt/resume/help (including `resume_cmd` for dedicated resume commands)
- `defaults.*`: base dir, timeouts, state/toolhelp paths, OpenAI settings, `github_token` for HTTPS git
- `defaults.log_path`: bot log file path
- `mtproto.*`: MTProto enablement, `api_id/api_hash`, `session_string` or `session_path`, `targets` list for the send menu
- `mcp.*`: TCP bridge for external clients (host/port/token)
- `presets`: task templates list (name + prompt)

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

GitHub PAT for HTTPS git:
- `defaults.github_token` in `config.yaml` (used for clone/fetch/pull/push without interactive prompts)

MTProto:
- `mtproto.enabled` enables MTProto menu.
- `mtproto.api_id` and `mtproto.api_hash` — Telegram API credentials.
- `mtproto.session_string` (recommended) or `mtproto.session_path` for authorization.
- `mtproto.targets` — target list (title + peer) shown in the menu.
Flow: message from MTProto menu → executed in active CLI session → result delivered to the chosen chat.

## Bot commands
### Visible in Telegram menu
- `/new` — create a session (inline menu)
- `/sessions` — session management menu (use/status/rename/close/resume/state/queue/clearqueue)
- `/interrupt` — interrupt generation
- `/git` — Git actions for the active session (inline menu: status/fetch/pull/merge/rebase/diff/log/stash/commit/push/summary/help)
- `/mtproto` — MTProto menu: send a task to CLI and receive the result in the chosen chat
- `/files` — send a file from the working directory
- `/tools` — list tools
- `/toolhelp` — show tool /commands
### Available but hidden from menu
- `/dirs` — browse directories (menu)
- `/newpath <path>` — set path after choosing a tool
- `/use` — set active session (menu)
- `/cwd <path>` — create session in a directory
- `/setprompt <tool> <regex>` — set prompt_regex for a tool
- `/send <text>` — send text directly to CLI
- `/resume [token]` — show/set resume token
- `/close` — close a session and remove it from saved state (menu)
- `/status` — active session status (busy, last activity/seconds tick)
- `/rename <id> <name>` or `/rename <name>` — rename session
- `/state [tool path]` — state by tool+path or menu
- `/clearqueue` — clear queue for active session
- `/queue` — show queue
- `/preset` — task templates for CLI
- Presets are loaded from `config.yaml` (`presets` section).
- `/metrics` — bot metrics

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
