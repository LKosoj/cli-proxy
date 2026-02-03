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
- Separate MTProto menu: task goes to CLI, result is written to an md file and its content is sent to the chosen chat.
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

6) **Extras**
   - Git operations are available via `/git` (status, pull, diff, commit, summary, etc.).
   - MTProto menu (`/mtproto`) sends a task to CLI. CLI writes an md file (timestamped), and the bot sends its content to the chosen chat (up to 12000 chars). Status is always sent to the bot.

## Configuration
`config.yaml` supports:
- `tools.*`: launch commands, mode, prompt/resume/help (including `resume_cmd` for dedicated resume commands) and `image_cmd` (appended to base command/resume_cmd for image handling)
- `defaults.*`: base dir, timeouts, state/toolhelp paths, OpenAI settings, `zai_api_key`/`tavily_api_key` for web search/reader, `github_token` for HTTPS git
- `defaults.log_path`: bot log file path (main log). Errors are written to a separate `*_error.log` file with the same rotation rules.
- `defaults.mtproto_output_dir`: directory for MTProto result md files (relative to workdir or absolute). Files are timestamped.
- `defaults.mtproto_cleanup_days`: delete md files older than N days (default 5).
- `defaults.image_temp_dir`: temp image directory (relative to workdir or absolute).
- `defaults.image_max_mb`: max size for a single image (default 10 MB).
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

Z.AI web search/reader:
- `defaults.zai_api_key` in `config.yaml`
- or env `ZAI_API_KEY` (takes precedence)

Tavily web search/reader:
- `defaults.tavily_api_key` in `config.yaml`
- or env `TAVILY_API_KEY` (takes precedence)

GitHub PAT for HTTPS git:
- `defaults.github_token` in `config.yaml` (used for clone/fetch/pull/push without interactive prompts)

MTProto:
- `mtproto.enabled` enables MTProto menu.
- `mtproto.api_id` and `mtproto.api_hash` — Telegram API credentials.
- `mtproto.session_string` (recommended) or `mtproto.session_path` for authorization.
- `mtproto.targets` — target list (title + peer) shown in the menu.
Flow: message from MTProto menu → executed in active CLI session → CLI writes md file (timestamped) → bot sends its content to the chosen chat (max 12000 chars), status always goes to the bot.

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
- `/mtproto` — MTProto menu: send a task to CLI and receive the result in the chosen chat
  - If called without text, the bot asks for the task (send `-` to cancel).
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
