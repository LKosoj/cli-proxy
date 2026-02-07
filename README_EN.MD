# CLI Proxy Telegram Bridge

[English version](README_EN.MD) | [Русская версия](README.md)

Telegram bot for managing CLI agents (Codex / Gemini / Qwen / Claude) with multiple sessions, request queueing, HTML output, and cached tool help (/commands).

## Features
- Multiple CLI sessions in different directories.
- Request queue and busy control.
- Output always sent as an HTML file (Markdown rendering, ANSI colors, Mermaid diagrams as SVG; on render failure they remain as code blocks).
- Short preview: OpenAI summary or first 4000 chars.
- Prompt and resume token autodetection, saved in `state.json`.
- Active session restore after restart.
- Inline menus for sessions, directories, and commands.
- “Use this directory” button in the directory picker.
- “git clone” button to clone a repo into the chosen directory.
- Inline Git menu (status/fetch/pull ff-only/merge/rebase/diff/log/stash/commit/push) for the active session.
- Merge/rebase conflict handling with diff/abort/continue/call-agent buttons.
- Cached tool help in `toolhelp.json`.
- Send files from the working directory via `/files`.
- Task templates (hidden `/preset` command).
- **Agent mode** (`/agent`) — AI agent (ReAct) with a toolset (read/write files, search, run commands, web search, etc.), plans and executes tasks step by step.
- **Manager mode** (`/manager`) — multi-agent orchestration: automatic task decomposition into subtasks, development via CLI, review via Agent, acceptance-criteria-based arbitration, and a final report.

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

## Suggested workflow
1) **Start and pick a tool**
   - Open the bot menu and run `/tools` to see available CLIs.
   - Create a session via `/new` and select the working directory.

2) **Send a task**
   - Send a normal message — it goes to the active session.
   - If the message starts with `/`, use `/send /command` or `> /command`.
   - Long messages are buffered for 2 seconds; short ones (<3000) are sent immediately.
- `.txt`, `.md`, `.rst`, `.log`, `.html`, `.htm` attachments (up to 500 KB) are sent immediately and merged with the caption.

3) **Control execution**
   - If the session is busy, the bot offers: cancel, queue, or discard input.
   - Check active status with `/status` or via `/sessions`.
   - Queue can be inspected with `/queue` and cleared with `/clearqueue`.

4) **Handle results**
   - You receive a short announcement first (summary or preview).
   - Full output is sent as an HTML file — open it to review details.
   - Use `/files` to download files from the working directory.

5) **Sessions and continuation**
   - Use `/sessions` to switch, rename, or close sessions.
   - Resume tokens are saved and can be restored automatically.

6) **Agent mode**
   - Enable via `/agent` — the bot will use an AI agent (ReAct) with tools (files, search, commands) to complete tasks.
   - Agent and Manager are mutually exclusive — enabling one disables the other.

7) **Manager mode**
   - Enable via `/manager` — the system automatically decomposes a task into subtasks, delegates development to CLI, review to an Agent, and makes decisions based on acceptance criteria.
   - Progress and task statuses are displayed in the chat.
   - Plan management: status, pause, reset — via the `/manager` inline menu.
   - Requires configured OpenAI API and an active CLI session.

8) **Extras**
   - Git operations are available via `/git` (status, pull, diff, commit, summary, etc.).

## Configuration
`config.yaml` supports:
- `tools.*`: launch commands, mode, prompt/resume/help (including `resume_cmd` for dedicated resume commands) and `image_cmd` (appended to base command/resume_cmd for image handling)
- `defaults.*`: base dir, timeouts, state/toolhelp paths, OpenAI settings, `zai_api_key`/`tavily_api_key`/`jina_api_key` for web search/reader, `github_token` for HTTPS git
- `defaults.log_path`: bot log file path (main log). Errors are written to a separate `*_error.log` file with the same rotation rules.
- `defaults.image_temp_dir`: temp image directory (relative to workdir or absolute).
- `defaults.image_max_mb`: max size for a single image (default 10 MB).
- `defaults.manager_*`: Manager mode settings — max tasks, max attempts, decompose/dev/review timeouts, report truncation, auto-resume, debug logging to `.manager/`
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

Z.AI web search/reader:
- `defaults.zai_api_key` in `config.yaml`
- or env `ZAI_API_KEY` (takes precedence)

Tavily web search/reader:
- `defaults.tavily_api_key` in `config.yaml`
- or env `TAVILY_API_KEY` (takes precedence)
Tavily provides free monthly limits — you can use them.

Jina.ai web search/reader:
- `defaults.jina_api_key` in `config.yaml`
- or env `JINA_API_KEY` (takes precedence)

GitHub PAT for HTTPS git:
- `defaults.github_token` in `config.yaml` (used for clone/fetch/pull/push without interactive prompts)

## Attachments
- Text files: `.txt`, `.md`, `.rst`, `.log`, `.html`, `.htm` (up to 500 KB). Bot combines caption + file contents and sends to CLI.
- Images: `.png`, `.jpg`, `.jpeg` (photo/document). Requires `image_cmd` for the selected CLI.
  If current tool lacks `image_cmd`, bot reports that images are unsupported.
  Caption is used as the prompt text.

## Bot commands
### Visible in Telegram menu
- `/new` — create a session (inline menu)
- `/sessions` — session management menu (use/status/rename/close/resume/state/queue/clearqueue)
- `/interrupt` — interrupt generation
- `/git` — Git actions for the active session (inline menu: status/fetch/pull/merge/rebase/diff/log/stash/commit/push/summary/help)
- `/files` — send a file from the working directory
- `/agent` — enable/disable AI agent mode
- `/manager` — project manager mode (decompose → develop → review → arbitrate)
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
