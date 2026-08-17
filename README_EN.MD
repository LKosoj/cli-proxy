# CLI Proxy Telegram Bridge

[English version](README_EN.MD) | [Russian version](README.md)

Telegram bot for managing CLI agents (Codex / Gemini / Qwen / Claude / Grok / Kimi / opencode) with multiple sessions, request queueing, and HTML output.

## Features
- Multiple CLI sessions in different directories.
- Request queue and busy control.
- Short output within the Telegram Rich Message limit (up to 32768 UTF-8 characters) is sent as a Telegram message; long output or `force_html=True` is sent as an HTML file (Markdown rendering, ANSI colors, Mermaid diagrams as SVG; on render failure they remain as code blocks).
- Summary/preview for long HTML output is sent as a separate message when summary delivery is enabled.
- Prompt and resume token autodetection, saved in `state.json`.
- Session and thread/scope mapping restore after restart.
- Inline menus for sessions, directories, and commands.
- "Use this directory" button in the directory picker.
- "git clone" button to clone a repo into the chosen directory.
- Inline Git menu (status/fetch/pull ff-only/merge/rebase/diff/log/stash/commit/push) for the current context session.
- Merge/rebase conflict handling with diff/abort/continue/call-agent buttons.
- Admin command `/selfupdate`: runs `git pull --ff-only` in the bot directory and starts service restart on success.
- Manage files in the working directory via `/files` (send, save, rename, delete).
- Current-session reports are available in Telegram via `/reports`; `/reports snapshot` and the "Session report" button in `/sessions` create a human-readable HTML session report in the user's language from run artifacts, saved report history, and the available CLI transcript.
- Task templates (hidden `/preset` command).
- **Agent mode** (via `/sessions` -> `Agent`) - auxiliary AI agent (ReAct) with a toolset (read/write files, search, run commands, web search, etc.).
- MiniApp is the companion web panel next to the bot: session status, files, logs, config, and scheduler.

## Quick start
1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Install ripgrep (recommended for search in Agent mode):
```bash
# Ubuntu/Debian
sudo apt-get install ripgrep

# macOS
brew install ripgrep

# Windows (Chocolatey)
choco install ripgrep

# Or via cargo
cargo install ripgrep
```

ripgrep is used for fast file search with `.gitignore` support.
Without ripgrep, Agent falls back to regular `grep`.

3. Fill `config.yaml`:
- `telegram.token`
- `telegram.whitelist_chat_ids`
- `defaults.workdir`
- optionally `openai_*` for CLI output summaries

`telegram.whitelist_chat_ids` is a critical access-control setting:
- the bot only responds to chats whose `chat_id` is listed;
- if the list is empty, no chat is allowed;
- for groups, `chat_id` is usually negative (for example, `-100...`).

4. Run the bot:
```bash
python bot.py
```

## Thread Mode, Webhooks, and Scheduler

### Thread Mode as the default group workflow
- For supergroups with forum topics, the base operating mode is `thread_mode.mode=group`.
- In this mode every Telegram session gets its own topic, and inbound/outbound routing uses `message_thread_id`. Background reports and mode reports return to the same topic through `NotificationQueueService`.
- The canonical runtime session identifier is always `session_uid`, derived from `ConversationScope`: direct chat uses `chat:<chat_id>`, forum topic uses `thread:<chat_id>:<message_thread_id>`.
- Minimal configuration:
```yaml
thread_mode:
  enabled: true
  mode: group
  topics_chat_id: -1001234567890
```
- BotFather enablement steps:
1. Open `@BotFather` and run `/mybots`.
2. Select the target bot.
3. Open `Bot Settings`.
4. Open `Group Topics` and turn topics on for the bot.
- On startup the bot runs a capability check via `bot.get_me().has_topics_enabled`. If topics are disabled, the process exits with a CRITICAL log and an explicit instruction to return to BotFather.

### Webhooks
- `webhooks.enabled=true` turns on the shared HTTP ingress on the common `aiohttp` listener.
- Webhooks keep working even when `miniapp.enabled=false` because the ingress is shared between MiniApp and the Telegram webhook route from `webhooks.path`.
- For Telegram, the usual webhook path is `webhooks.path: /webhooks/telegram`; requests are verified with `webhooks.secret_token` through `X-Telegram-Bot-Api-Secret-Token` or `X-Webhook-Secret-Token`.
- Recommended configuration:
```yaml
webhooks:
  enabled: true
  public_base_url: https://bot.example.com
  path: /webhooks/telegram
  secret_token: change-me
```
- An accepted webhook is published as `WebhookReceivedEvent`, then `ModeLaunchAdapter` applies the `origin -> mode` allowlist, preserves `correlation_id`/`dry_run`, and delivers final reports through queue-based delivery.

### Scheduler
- `scheduler.enabled=true` turns on the persistent scheduler backed by SQLite.
- Every job stores `cron`, `target_mode`, the owner, and explicit `notification_target.telegram_session_uid` so reports return to the correct Telegram scope.
- Scheduler does not run a mode directly: when a job fires it emits `ScheduledJobEvent`, which then goes through the same policy check and mode launch adapter used by webhook events.
- CRUD for scheduler jobs is available in MiniApp. All `create/update/delete` mutations are written into the audit trail.
- Basic configuration:
```yaml
scheduler:
  enabled: true
  timezone: Europe/Moscow
  tick_interval_sec: 1.0
```

### Installer-based setup (`setup_bot.sh`)
For first-time setup on Ubuntu/Debian, use:

```bash
./setup_bot.sh
```

The script will:
- install system dependencies and Python venv;
- install CLI tools (`codex`, `claude`, `gemini`, `qwen`, `grok`, `kimi`, `opencode`);
- create `config.yaml` and `.env`;
- create/start a `systemd` service.

Non-interactive mode:
```bash
SETUP_BOT_TOKEN="123456:ABCDEF" \
SETUP_WHITELIST_RAW="123456789,-1009876543210" \
SETUP_ADMINLIST_RAW="123456789" \
# Optional: projects for non-admin users (CHATID:/abs/p1,/abs/p2;CHATID2:/abs/p3)
# SETUP_USER_WORKDIRS_RAW="111111111:/opt/projects/a,/opt/projects/b" \
SETUP_WORKDIR="/opt/cli-proxy/workdir" \
OPENAI_API_KEY="sk-..." \
OPENAI_MODEL="gpt-4.1-mini" \
OPENAI_BIG_MODEL="gpt-4.1" \
./setup_bot.sh --non-interactive --service-name cli-proxy-bot --service-user "$(whoami)" --chown no
```

Required in `--non-interactive` mode:
- `SETUP_BOT_TOKEN`
- `SETUP_WHITELIST_RAW`
- `SETUP_ADMINLIST_RAW`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_BIG_MODEL`

### `claude-bot` setup for Claude Code
If you plan to use `Claude Code` with `--dangerously-skip-permissions`, first set up a separate unprivileged user:

```bash
sudo ./scripts/setup-claude-bot.sh
```

You can optionally override the workdir, username, and CLI version:

```bash
sudo ./scripts/setup-claude-bot.sh --workdir /srv/git_projects --username claude-bot --version latest
```

What the script does:
- creates the `claude-bot` user (or the user specified via `--username`) if it does not exist yet;
- creates a shared `cli-proxy-workgroup` group and adds both `root` and `claude-bot` to it;
- configures permissions for `--workdir` (default: `/srv/git_projects`): group `cli-proxy-workgroup` plus `g+rwxs`, so new files inherit the shared group;
- installs `claude` via the official install script as the `claude-bot` user;
- adds `$HOME/.local/bin` to that user's `~/.bashrc`;
- appends Anthropic environment variables to `~/.bashrc` (`ANTHROPIC_MODEL=coder-model`, plus `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` if they are set in the script);
- runs a final validation pass and prints what succeeded and what still needs attention.

After running the script, verify:
- `su - claude-bot -c "claude --version"`: the CLI responds and prints a version;
- `su - claude-bot -c "test -w /srv/git_projects && echo OK"`: the user can write to the workdir (replace with your custom `--workdir` if needed);
- `su - claude-bot -c "test -x ~/.local/bin/claude && echo OK"`: the binary exists in the user's `~/.local/bin`;
- `groups claude-bot`: the user is a member of `cli-proxy-workgroup`;
- for the current root shell, you have re-logged in or run `newgrp cli-proxy-workgroup`, otherwise the new group membership may not apply immediately.

After that, the main bot can continue running as root: `claude` commands are launched automatically via `su - claude-bot -c "..."`.

## Suggested workflow
1. **Start and pick a tool**
   - Open the bot menu and run `/tools` to see available CLIs.
   - Open `/sessions` -> `+ New session`, then choose tool and working directory.

2. **Send a task**
   - Send a normal message - it goes to the current topic/context session.
   - If the message starts with `/`, use `/send /command` or `> /command`.
   - Long messages are buffered for 2 seconds; short ones (<3000) are sent immediately.
- `.txt`, `.md`, `.rst`, `.log`, `.html`, `.htm` attachments (up to 500 KB) are sent immediately and merged with the caption.

3. **Control execution**
   - If the session is busy, the bot offers: cancel, queue, or discard input.
   - Check active status with `/status` or via `/sessions`.
   - Queue can be inspected with `/queue` and cleared with `/clearqueue`.

4. **Handle results**
   - Short results within the Telegram Rich Message limit (up to 32768 UTF-8 characters) arrive as Telegram messages.
   - Long results arrive as HTML files; `force_html=True` forces an HTML file even for short results.
   - Summary or preview for long HTML output arrives as a separate message when summary delivery is enabled.
   - Use `/reports` for session reports; `/reports latest` sends the latest report, `/reports snapshot` creates a human-readable HTML session report in the user's language, and `/reports <filename.md>` sends a specific report file.
   - Use `/files` to work with files (download/save/rename/delete).

5. **Sessions and continuation**
   - Use `/sessions` to switch, rename, or close sessions.
   - Resume tokens are saved and can be restored automatically.

6. **Agent mode**
   - Enable via `/sessions` -> `Agent` - an auxiliary AI agent (ReAct) with tools (files, search, commands).

7. **Git**
   - Git operations are available via `/git` (status, pull, diff, commit, summary, etc.).
   - Admins can use `/selfupdate` (code update + service restart).

## Launching a mode from a webhook or the scheduler
- Webhooks and the scheduler do not talk to the CLI directly: the event goes through a policy check and `ModeLaunchAdapter`, then starts an allowed mode (`agent` or direct CLI).
- The report returns to the same Telegram chat or topic stored in `notification_target.telegram_session_uid`.

## Mode status model
- Base stages are unified via `ModeStatusService.build_common_mode_stage`: `disabled`, `processing task`, `waiting in queue`, `waiting for new request`.
- Agent status includes operational fields: `pending_questions`, `active_plugin_flow`, `queue_origin`.
- These fields are exposed in Telegram status text and in the MiniApp payload.

## Recent Behavior Updates
- **Mode errors in CLI without silent fallback:** on internal `mode.handle_input` errors, input routing no longer silently falls back to plain CLI flow. The error is logged via `logger.exception` and explicitly re-raised.
- **Pending input queue:** pending inputs are stored as a per-chat FIFO queue (`deque`) with limit `5`. New inputs are appended; on overflow, the user gets a notification.
- **Busy-guard for destructive callbacks:** while session is busy, `reset`/`clean`/`disconnect` are rejected by the shared callback router.
- **Mode-level callback audit:** each callback entry is logged to stdout with `session_id`, `mode`, `action`, `chat_id`, timestamp and `INFO` level.
- **Error logging:** all new `except` branches log via `logger.exception`.

## Cross-CLI Session Transfer
- When the active CLI is switched, the bot offers to transfer context from the previous CLI if it has a `resume_token`.
- Transfer is compact by default: the full transcript is read from the source CLI native file and stored as an evidence pack in `.cli-proxy/session-transfer/<transfer-id>/`, while the target CLI receives a short session capsule instead of the whole history.
- The evidence pack contains `manifest.json`, `canonical.jsonl`, and `capsule.md`. The target agent gets these paths and should inspect them only when older context is needed.
- Low-level native writers remain available for compatibility and tests, but the main user flow no longer injects the full transcript into the target CLI context.

## Session Execution Backends
Each CLI can expose a settings-level execution backend: `headless` or `tmux`. This does not replace `tools.<name>.mode`: `mode: headless` remains the compatibility mode for the existing headless path, while `execution_backends` controls which backends the CLI supports.

Example:
```yaml
defaults:
  default_execution_backend: headless

tools:
  qwen:
    mode: headless
    cmd: ["qwen", "--yolo", "--prompt", "{prompt}", "--resume", "{resume}"]
    interactive_cmd: ["qwen", "--session-id", "{session_id}"]
    interactive_resume_cmd: ["qwen", "--resume", "{resume}"]
    execution_backends: ["headless", "tmux"]
    default_execution_backend: headless
```

- `headless` uses the current per-request CLI execution path and preserves existing behavior.
- `tmux` starts one long-lived interactive `tmux` session, writes runtime state under `.cli-proxy/runtime/tmux/` inside the workdir, and submits prompts through the pane/buffer path without the headless command.
- For Claude, Codex, Grok, Qwen, Gemini, and Kimi, intermediate status, final output, and turn completion are read primarily from the native JSONL transcript. `pane.log` and the `DONE` marker remain a fallback for unavailable or unknown transcript formats. Missing output does not interrupt a live tmux session; `Ctrl-C` is sent only for an explicit stop.
- On first start, the tmux backend can pin `resume_token` through `{session_id}` in `interactive_cmd` or through a CLI-specific session flag when the CLI supports it. If an interactive CLI prints a session id, configure `resume_regex` and the token is captured from the pane log.
- Existing tmux sessions survive a graceful bot restart. On startup, the runtime checks them, restores monitoring for an active request, delivers an answer that completed during downtime to the original chat/thread, and then continues the persisted queue. Explicit session close, interrupt, backend switch, active CLI switch, and `force fresh` still stop the corresponding tmux session. A CLI switch closes only the previous CLI's tmux instance for the current bot session; other bot sessions are untouched.
- For reboot recovery, the backend checks tmux session presence before every non-first request. If the tmux session disappeared and `resume_token` is known, it starts `interactive_resume_cmd` with `{resume}` or the built-in resume command for the supported CLI instead of the headless path.
- Built-in resume commands exist for known CLIs: `claude --resume <token>`, `gemini --resume <token>`, `qwen --resume <token>`, `grok --resume <token>`, and `codex resume <token>`. Explicit `interactive_resume_cmd` takes precedence.
- `tools.<name>.tmux_user` optionally runs tmux commands through `su - <user>`. A root systemd service must also set it for CLIs that run as root (for example, `root` for Codex/Grok): the login session moves the tmux server out of the `bot.service` cgroup, so a systemd restart does not kill it together with headless processes. Claude Code normally uses a dedicated `claude-bot` user.
- Backend selection is configured only through settings: `tools.<name>.default_execution_backend` has priority for a concrete CLI, and `defaults.default_execution_backend` is the shared fallback.
- Changing `default_execution_backend` during runtime reload applies to existing sessions when their CLI supports the selected backend; session-level overrides are no longer used, and the UI shows the backend as read-only state.
- While a tmux request is active, the busy-session menu can send a new text message directly to the current pane or keep it as a separate queue item; when the queue is non-empty, the message can also be merged into its last item.
- `tmux` v1 does not support image requests: image input is rejected explicitly, with no silent fallback to headless.
- Rollback: set `defaults.default_execution_backend: headless` or `tools.<name>.default_execution_backend: headless` and run runtime reload. Idle tmux sessions are closed when the backend changes; busy sessions must finish first or be closed/recreated.
- CI covers the tmux path with a guard test: the backend must not use `headless_cmd` or headless CLI execution.

## Configuration
`config.yaml` supports:
- `telegram.whitelist_chat_ids`: allowed Telegram chat id list (mandatory access control)
- `telegram.user_modes`: per-user mode allowlist; supports the registered `agent` mode, virtual `direct_cli` and `orchestrator`, and `"all"` includes both registered modes and these virtual tokens
- `tools.*`: launch commands, mode, prompt/resume/help (including `resume_cmd` for dedicated headless resume commands), `image_cmd` (appended to base command/resume_cmd for image handling), plus `interactive_cmd`, `interactive_resume_cmd`, `execution_backends`, and `default_execution_backend` for settings-level `headless`/`tmux` selection
- `defaults.*`: base dir, timeouts, state path, OpenAI settings, `zai_api_key`/`tavily_api_key`/`jina_api_key` for web search/reader, `github_token` for HTTPS git, `gemini_oauth_client_secret` for refreshing Gemini quota credentials, `default_execution_backend` for new sessions
- `defaults.log_path`: bot log file path (main log). Errors are written to a separate `*_error.log` file with the same rotation rules.
- `defaults.image_temp_dir`: temp image directory (relative to workdir or absolute).
- `defaults.image_max_mb`: max size for a single image (default 10 MB).
- `defaults.cli_json_stream_archive_enabled`: enables raw/normalized JSONL archive for JSON-stream CLIs in `.cli-proxy/cli-json-stream/`; useful for schema-drift and CLI-completion debugging
- `defaults.assistant_preview_enabled`: when enabled, Telegram shows a single live preview built from the latest CLI `assistant_text`, updates it in place during the run, and removes it after the final answer
- `defaults.run_artifacts_*`: durable run traces and retention cleanup (`STATE/PLAN/CHECKPOINTS/METRICS/RECOVERY/EVENTS`) used by doctor/recover/resume and UI run panels
- `defaults.run_doctor_enabled` / `defaults.run_boundary_validation_enabled` / `defaults.run_metrics_enabled`: enable doctor backend, phase gates, and per-run token/cost/duration aggregates in `METRICS.json`
- `defaults.skill_discovery_mode`: `off` disables selection, `suggest` injects only already available skills, `auto` allows discover/install of allowlisted skills into the project-local registry
- `defaults.skill_install_policy`: `manual` only suggests a skill, `admin_approve` writes a pending approval into `.cli-proxy/skills/.skill_install_approval_ledger.json` and requires explicit admin approve/reject, `allowlisted_auto` permits automatic installation of allowlisted discoveries into the project-local registry
- `defaults.skill_registry_paths` / `defaults.skill_allowlisted_sources`: project/global registry roots and allowlisted sources from which skill runtime may select or install skills
- `mcp.*`: TCP bridge for external clients (host/port/token)
- `mcp_clients`: canonical serialized list of MCP clients / HTTP endpoints
- `miniapp.*`: embedded HTTP MiniApp (`enabled`, `bind_host`, `bind_port`, `base_path`, `public_url`, `max_edit_file_size_kb`, `enable_delete`)
- `thread_mode.*`: runtime thread/topic settings (`enabled`, `mode`, `topics_chat_id`, `topic_title_prefix`, `inactivity_ttl_sec`)
- `webhooks.*`: Telegram webhook mode on the shared ingress (`enabled`, `path`, `public_base_url`, `secret_token`, `request_timeout_sec`, `max_payload_bytes`)
- `scheduler.*`: embedded scheduler (`enabled`, `timezone`, `tick_interval_sec`, `max_concurrent_jobs`, `job_timeout_sec`, `misfire_grace_sec`)
- `presets`: task templates list (name + prompt)

### Typed config: `AppConfig` / `AppConfigModel`
- The typed loader first validates YAML/.env/env overlay through the Pydantic model `app/config_runtime/models.py:AppConfigModel`, then adapts the result into the runtime dataclass `AppConfig`.
- Canonical runtime `AppConfig` shape:
```yaml
telegram: {}
tools: {}
defaults: {}
mcp: {}
miniapp: {}
thread_mode: {}
webhooks: {}
scheduler: {}
mcp_clients: []
presets: []
```
- The new `thread_mode`, `webhooks`, and `scheduler` sections are fail-fast validated. For example, `thread_mode.mode=group` requires `thread_mode.topics_chat_id`, while `webhooks.path` and `miniapp.base_path` must start with `/`.

### Env overrides for typed config
- Overlay order: `config.yaml` -> legacy env aliases from `.env`/process env -> `CLI_PROXY__*` overrides. Inside the env layer, process env wins over `.env` for the same variable name.
- Generic override prefix: `CLI_PROXY__`. The path is built with `__`, case is ignored, and values are parsed through YAML (`true`, `30`, `[]`, `{}`, etc.).
- For `defaults.*`, legacy env names are also supported: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BIG_MODEL`, `OPENAI_BASE_URL`, `ZAI_API_KEY`, `TAVILY_API_KEY`, `JINA_API_KEY`, `GITHUB_TOKEN`. They also override YAML values, but remain less explicit than `CLI_PROXY__DEFAULTS__...`.
- Examples for the new sections:
```bash
export CLI_PROXY__THREAD_MODE__ENABLED=true
export CLI_PROXY__THREAD_MODE__MODE=group
export CLI_PROXY__THREAD_MODE__TOPICS_CHAT_ID=-1001234567890

export CLI_PROXY__WEBHOOKS__ENABLED=true
export CLI_PROXY__WEBHOOKS__PUBLIC_BASE_URL=https://example.com
export CLI_PROXY__WEBHOOKS__SECRET_TOKEN=super-secret

export CLI_PROXY__SCHEDULER__ENABLED=true
export CLI_PROXY__SCHEDULER__TIMEZONE=Europe/Moscow
export CLI_PROXY__SCHEDULER__MAX_CONCURRENT_JOBS=2
```

### Canonical serialization and semantic diff
- Canonical output uses only `mcp_clients`.
- HTTP MCP endpoints use the same `mcp_clients` list with `transport=http`, `url`, `headers`, and `timeout_ms`.
- `ConfigService` uses semantic diff: it compares the canonical serialized representation of the file on disk against the new `AppConfig`. That avoids false diffs, file rewrites, and `.bak` creation when nothing actually changed.

### Schema Contracts (modes)
- The Agent mode schema is `modes/agent/schemas.py`.
- Schema feature-gating is removed: validation uses a single unified pipeline.
- Legacy parsing fallback is removed: structured parsing is V2-only via `parse_normalize_validate`/`loads_safe`.

### MiniApp
- `/miniapp` opens the Telegram MiniApp for allowed users (`telegram.admlist_chat_ids` and whitelist+ACL users).
- Shared ingress bind address comes from `miniapp.bind_host`/`miniapp.bind_port` and is used by both MiniApp and the webhook ingress.
- MiniApp base path: `miniapp.base_path` (default `/cli-proxy`), API prefix `/cli-proxy/api/*`.
- For the `/miniapp` button, set `miniapp.public_url` to an absolute URL (for example, `https://example.com/cli-proxy`).
- API auth: `X-Telegram-Init-Data` header with Telegram WebApp signature verification.
- `Logs` scope:
  - 5 log types: `main`, `error`, `agent`, `cli_dialog`, `miniapp`;
  - real-time streaming via WebSocket;
  - history modes: `new only`, `100/200/500/1000 previous`;
  - filter by canonical `session_uid` tied to `ConversationScope`, with a human-readable session label;
  - download exactly the lines currently displayed in MiniApp.
- Log access roles:
  - admin can see all records and all `session_uid` values;
  - regular users can only see their own records/sessions.
- `Config` scope: `config.yaml` form-based editing + diff preview + save + automatic `reload_runtime_config()`.
- `Files` scope: only the `workdir` resolved from explicit `project_slug` + `session_uid`.
  - If there is no explicit context, every `files/*` endpoint (including `files/tree`) returns `409 Session context required`.
  - Editing `config.yaml` via files API is forbidden (Config UI only).
  - Binary / oversized files are blocked for editing.

### Hot-reload matrix
- After Config UI save, `reload_runtime_config()` is always called, but only fields classified
  by `ConfigApplyPolicy` as `hot_reload` are applied in the running process. Everything else
  is reported as `restart_required` and remains a YAML change until process restart or a
  separate operator action.
- Applied without restart:
  - `defaults.cli_json_stream_archive_enabled`, `defaults.assistant_preview_enabled`,
    `defaults.pending_input_confirmation_enabled`;
  - `tools.*` (for new runs and existing sessions where safe);
  - `presets`, `security.*`, `lint_evolution.*`;
  - `telegram.whitelist_chat_ids`, `telegram.admlist_chat_ids`, `telegram.user_workdirs`, `telegram.user_modes`;
  - `miniapp.public_url`, `miniapp.enable_delete`;
  - `mcp.token`, `webhooks.secret_token`;
  - secret fields `defaults.openai_api_key`, `defaults.zai_api_key`, `defaults.github_token`,
    `defaults.tavily_api_key`, `defaults.jina_api_key`.
- Requires process restart or a separate operator action:
  - `telegram.token`;
  - Telegram transport/polling timeouts (`connection_pool_size`, `*_timeout_sec`, `polling_timeout_sec`, `poll_interval_sec`);
  - runtime/skills flags `defaults.run_artifacts_enabled`, `defaults.run_artifacts_retention_days`,
    memory-learning flags `defaults.memory_events_enabled`,
    `defaults.memory_native_cli_hooks_enabled`, `defaults.memory_outcomes_enabled`,
    `defaults.memory_dreaming_enabled`, `defaults.memory_events_retention_days`,
    `defaults.memory_events_max_payload_chars`, `defaults.memory_events_redaction_enabled`,
    `defaults.memory_dreaming_batch_size`, `defaults.run_doctor_enabled`,
    `defaults.run_boundary_validation_enabled`,
    `defaults.run_metrics_enabled`, `defaults.skill_discovery_mode`,
    `defaults.skill_install_policy`, `defaults.skill_registry_paths`,
    `defaults.skill_allowlisted_sources`, `defaults.gemini_oauth_client_secret`;
  - `miniapp.enabled`, `miniapp.bind_host`, `miniapp.bind_port`, `miniapp.base_path`, `miniapp.max_edit_file_size_kb`;
  - `mcp.enabled`, `mcp.host`, `mcp.port`, `mcp_clients`;
  - `thread_mode.*`, `scheduler.*`;
  - `webhooks.enabled`, `webhooks.path`, `webhooks.public_base_url`,
    `webhooks.request_timeout_sec`, `webhooks.max_payload_bytes`.
- MiniApp uses the same policy: diff reports `applied` only for hot-reload fields,
  while `scheduler`, `thread_mode`, `mcp` transport/client settings, and webhook endpoint
  settings do not promise hot-apply.

### Runtime reload and events
- Runtime reload uses the same strict typed loader as startup: the config is re-read, env overrides are applied, validation runs through `AppConfigModel`, and only then is it adapted into `AppConfig`.
- If validation fails, the previous runtime config stays active. The loader logs the error, and reload returns an error status without partially applying the broken YAML.
- On successful reload, the config is safely swapped inside `BotApp`, session runtime, and mode runtimes; the result is also published through `SystemEventBus`.
- Reload events:
  - `runtime.config.reloaded` for a successful reload;
  - `runtime.config.reload_failed` when reload is rejected because of validation or read errors.
- Event payload contains `path`, `status`, `applied`, `restart_required`, and `warnings`. This is the same data shape used by the MiniApp `Config` UI during the preview/save/reload cycle.

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

Grok Build CLI is configured as `tools.grok`; headless mode uses the official
`grok --output-format streaming-json -p "{prompt}"`, and the xAI key can be passed
through `XAI_API_KEY` in `.env` or the process environment. For `/limits`, Grok
shows local usage for the latest project session from `~/.grok/sessions`; xAI
personal RPM/TPM quotas are available in Console and are not exposed through a
stable CLI/API. Grok session transfer uses the same local session store and the
compact transfer capsule.

Kimi Code CLI is configured as `tools.kimi`; headless mode uses the official
prompt mode `kimi --output-format stream-json --prompt "{prompt}"`. `--prompt`
already implies non-interactive mode, and kimi rejects `--yolo`, `--auto` and
`--plan` together with it, so headless carries none of them; for tmux `--yolo` is
set in `interactive_cmd`. Session continuation is token based: kimi prints the
token as the last line of a turn (`{"role":"meta","type":"session.resume_hint",...}`),
the bridge stores it and sends the next request with `--resume <token>` (an alias
of `--session`). `--continue` is deliberately absent: it would pick up the latest
session of the directory instead of starting a new one, and kimi refuses it
together with `--session` anyway (if you add it to `config.yaml`, the bridge drops
it on resume). The key comes from `KIMI_API_KEY` in
`.env` or the process environment, OAuth login is done with `kimi login`. For
`/limits`, Kimi reports that quotas are unavailable: the CLI does not publish
usage. Session transfer, native transcript reading in tmux mode and the recent
session list of the resume picker all read the journal at
`~/.kimi-code/sessions/<workdir key>/<session id>/agents/main/wire.jsonl`; the
workdir key repeats kimi's `encodeWorkDirKey` (`wd_<slug of the directory
name>_<12 hex of sha256 of the absolute path>`). A transferred session also gets a
`profile.bind` record: headless `--resume` binds no profile of its own and dies
with `model.not_configured` without it. The binding is taken from a session kimi
already rendered for this directory, otherwise from `default_model` in
`~/.kimi-code/config.toml`. On the first run in a new
directory kimi asks whether the folder is trusted; in tmux mode the bridge detects
that screen and asks you to open kimi in the workdir once.

opencode is configured as `tools.opencode`; headless mode uses
`opencode run --format json --session "{resume}" "{prompt}"` - the prompt is a
positional argument here, not a flag. `--format json` turns on a line-based
event stream (`text`, `tool_use`, `step_start`, `step_finish`, `reasoning`,
`error`); every line carries `sessionID`, so no `resume_regex` is needed.
opencode has no `--resume` flag: the token goes into `--session`, and with an
empty token the whole `--session {resume}` pair is dropped from the command. The
model is not pinned in `config.yaml` - opencode picks it itself (`opencode
providers`, `~/.config/opencode/opencode.json`), and login is done with
`opencode auth login`. tmux mode is not offered
(`execution_backends: ["headless"]`): the opencode TUI only runs full screen and
no screen parser was written for it. For `/limits`, opencode reports that quotas
are unavailable: they belong to the provider it was pointed at, not to the CLI.
Session transfer and the recent-session resume picker both read the single
database `~/.local/share/opencode/opencode.db` (or
`$XDG_DATA_HOME/opencode/opencode.db`). A transferred session is stamped with the
provider and model of the newest message in that database: on `--session`
opencode resolves the model from the last user message and dies with
`ProviderModelNotFoundError` on an unknown name. If opencode has never run on
this machine there is nothing to copy, and the bridge skips the transfer with a
warning.

To refresh Gemini OAuth credentials while collecting CLI limits, set this in `config.yaml`:
```yaml
defaults:
  gemini_oauth_client_secret: "..."
```

## YOLO/auto‑approve mode
Auto-approvals enabled in `config.yaml`:
- **Gemini CLI**: `--approval-mode yolo`
- **Qwen Code**: `--yolo`
- **Claude Code**: `--dangerously-skip-permissions`
- **Codex**: sends `/approvals full` in interactive mode

OpenAI can be set via `config.yaml` or env:
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

The `/selfupdate` command reads the systemd service name from env variable `CLI_PROXY_SERVICE_NAME`
(default: `cli-proxy-bot`).
- `OPENAI_BASE_URL`

Environment variables take precedence.

Z.AI web search/reader:
- `defaults.zai_api_key` in `config.yaml`
- or env `ZAI_API_KEY` (takes precedence)

Tavily web search/reader:
- `defaults.tavily_api_key` in `config.yaml`
- or env `TAVILY_API_KEY` (takes precedence)
Tavily provides free monthly limits - you can use them.

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
- `/sessions` - session management menu (new/list/status/rename/close/resume/state/queue/clearqueue/reset + agent)
- `/interrupt` - interrupt generation
- `/git` - Git actions for the current context session (inline menu: status/fetch/pull/merge/rebase/diff/log/stash/commit/push/summary/help)
- `/files` - file management in the working directory (send/save/rename/delete)
- `/miniapp` - open MiniApp
- `/reports` - show, create, or send reports for the current session (`latest`, `snapshot`, `generate`, `<filename.md>`)
- `/limits` - limits and usage for the current CLI
- `/tools` - list tools
### Available but hidden from menu
- `/dirs` - browse directories (menu)
- `/newpath <path>` - set path after choosing a tool
- `/cwd <path>` - create session in a directory
- `/setprompt <tool> <regex>` - set prompt_regex for a tool
- `/send <text>` - send text directly to CLI
- `/resume [token]` - show/set resume token
- `/close` - close a session and remove it from saved state (menu)
- `/status` - current context session status (busy, last activity/seconds tick)
- `/rename <id> <name>` or `/rename <name>` - rename session
- `/state [tool path]` - state by tool+path or menu
- `/clearqueue` - clear queue for the current context session
- `/queue` - show queue
- `/preset` - task templates for CLI
- Presets are loaded from `config.yaml` (`presets` section).
- `/metrics` - bot metrics

## Run operations: `doctor` / `recover` / `resume`
- The Agent inline panel and MiniApp `Status -> Runs` expose explicit run operations: `doctor`, `recover`, `resume`.
- `doctor` reads current `STATE.json`, `PLAN.json`, `CHECKPOINTS.json`, `METRICS.json`, `EVENTS.jsonl`, plus the mode legacy store, then writes a structured result into `RECOVERY.json`.
- `recover` records a safe recovery intent based on the doctor recommendation and does not perform arbitrary mid-step replay. If doctor sets `manual_review_required`, automatic replay is not started.
- `resume` is allowed only from a safe boundary selected by doctor/recover; concurrent invocation is blocked by busy/lock/tick guards.
- These run operations are different from `/resume [token]`: the slash command manages the CLI resume token, while `doctor/recover/resume` buttons operate on runtime recovery for the current run.

## State
- `state.json` - stores per-session state: `resume_token`, `summary/updated_at`, queue, active mode (`agent` or direct CLI), agent memory, `project_root`.
- `state.json` - stores orchestrator fields (webhook/scheduler launch): `advanced_orchestrator_enabled`, `orchestrator_pending_input`, `orchestrator_last_mode_output`, `orchestrator_last_mode_id`.
- `state.json` - stores sessions list and scope-bound mappings for restart restore.

## Tests
```bash
pytest -q
```

## Direct CLI input
If you need to send a line starting with `/`, use either:
- `/send /help`
- or prefix `>` in a normal message: `> /help`
