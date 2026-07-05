# CLI Proxy Telegram Bridge

[English version](README_EN.MD) | [Russian version](README.md)

Telegram bot for managing CLI agents (Codex / Gemini / Qwen / Claude / Grok) with multiple sessions, request queueing, and HTML output.

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
- Current-session reports are available in Telegram via `/reports`; `/reports snapshot` and the "Session report" button in `/sessions` create a human-readable HTML session report in the user's language from run artifacts, saved report history, and the available CLI transcript. Manager final reports are automatically saved into the shared report history.
- Task templates (hidden `/preset` command).
- **Agent mode** (via `/sessions` -> `Agent`) - AI agent (ReAct) with a toolset (read/write files, search, run commands, web search, etc.), plans and executes tasks step by step.
- **Manager mode** (via `/sessions` -> `Manager`) - multi-agent orchestration: automatic task decomposition into subtasks, development via CLI, review via Agent, acceptance-criteria-based arbitration, and a final report.
- **SDD mode** (via `/sessions` -> `SDD`) - Spec-Driven Development flow `specify -> plan -> tasks -> analyze` with approval gates, EARS criteria, intelligent project initialization, `specs/<NNN>-<slug>/` artifacts, an ADR log `.cli-proxy/decisions.md`, and handoff of accepted `tasks.md` into Manager.
- **Webmaster mode** (via `/sessions` -> `Webmaster`) - `intent -> confirm -> dev_cli -> validation_cli -> feedback` cycle for web-development tasks. Successful gate is accepted only when all checklist items are `PASS`.
- **Admin mode** (via `/sessions` -> `Admin`) - background `Monitor -> Analyzer -> Executor -> Notifier` pipeline for incident monitoring and remediation, with security policies, interactive `ask_user`, and hash-based approval override reuse.
- **Mapper mode** (via `/sessions` -> `Mapper`) - builds a codebase map in `.cli-proxy/.codebase_map/` via 4 parallel focus passes (tech/arch/quality/concerns), maintains an instruction md-graph (`INDEX.md`, `nodes/*.md`), supports init/re-init/deep-verify workflows, and updates incrementally using `git diff`.
  Map artifacts are automatically injected into Analyst and Manager context (when a map already exists).
- Orchestrator inter-mode handoff: on confirmed transition, next mode receives full `session.orchestrator.last_mode_output`, not a shortened summary.

## Quick start
1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Install ripgrep (recommended for Mapper mode):
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

ripgrep is used for fast file scanning with `.gitignore` support.
Without ripgrep, Mapper mode will be slower but will still support `.gitignore` via the `pathspec` library.

3. Fill `config.yaml`:
- `telegram.token`
- `telegram.whitelist_chat_ids`
- `defaults.workdir`
- optionally `openai_*` for CLI output summaries

`telegram.whitelist_chat_ids` is a critical access-control setting:
- the bot only responds to chats whose `chat_id` is listed;
- if the list is empty, no chat is allowed;
- for groups, `chat_id` is usually negative (for example, `-100...`).

3. Run the bot:
```bash
python bot.py
```

## Thread Mode, Webhooks, and Scheduler

### Thread Mode as the default group workflow
- For supergroups with forum topics, the base operating mode is `thread_mode.mode=group`.
- In this mode every Telegram session gets its own topic, and inbound/outbound routing uses `message_thread_id`. Background reports and mode reports return to the same topic through `NotificationQueueService`.
- The canonical runtime session identifier is always `session_uid`, derived from `ConversationScope`: direct chat uses `chat:<chat_id>`, forum topic uses `thread:<chat_id>:<message_thread_id>`, and Desktop uses `desktop:<session_id>`.
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
- CRUD for scheduler jobs is already available in both MiniApp and Desktop. All `create/update/delete` mutations are written into the audit trail.
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
- install CLI tools (`codex`, `claude`, `gemini`, `qwen`, `grok`);
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

## Desktop application
The project also includes a desktop client built with `PySide6 + qasync`. It reuses the same domain services (`app/services`, `sessions`, `modes`) through a facade layer.

Run:
```bash
python desktop/main.py
```

Desktop architecture entry points:
- `desktop/main.py` - desktop composition root, service bootstrap, and window startup.
- `desktop/main_window.py` - main window shell and UI event routing. Visual rendering of V2 reaction events (retry, reroute, needs_input) and validation statuses (not_run).
- `desktop/services/application_facade.py` - desktop orchestration and mode/runtime integration, subscribing to ReactionEngine events.
- `desktop/widgets/*` - UI components without duplicating domain logic.
- The `Reports` tab uses the same shared report history as Telegram `/reports`; Desktop does not create a second report copy and only displays or locally exports the selected Markdown to PDF.

For layering and placement rules, see `ARCHITECTURE.md`.

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
   - Use `/reports` for session reports; `/reports latest` sends the latest report, `/reports snapshot` creates a human-readable HTML session report in the user's language, `/reports generate` creates one from the current Manager plan, and `/reports <filename.md>` sends a specific report file.
   - Use `/files` to work with files (download/save/rename/delete).

5. **Sessions and continuation**
   - Use `/sessions` to switch, rename, or close sessions.
   - Resume tokens are saved and can be restored automatically.

6. **Agent mode**
   - Enable via `/sessions` -> `Agent Agent` - the bot will use an AI agent (ReAct) with tools (files, search, commands) to complete tasks.
   - Agent and Manager are mutually exclusive - enabling one disables the other.

7. **Manager mode**
   - Enable via `/sessions` -> `Manager Manager` - the system automatically decomposes a task into subtasks, delegates development to CLI, review to an Agent, and makes decisions based on acceptance criteria.
   - Progress and task statuses are displayed in the chat.
   - Plan management: status, pause, reset - via the Manager inline menu.
   - Dynamic decomposition uses `min_tasks_dynamic` from `agent/manager.py`
     (`_min_tasks_dynamic(analysis)`):
     `max(MIN_TASKS_FLOOR, len(requirements)*MIN_TASKS_PER_REQ, len(remaining_work)*MIN_TASKS_PER_REMAINING) + ceil(not_done_checklist/3)`.
   - Structural plan validation uses:
     `TASK_COUNT_BELOW_MIN` (task count is below `min_tasks_dynamic`) and
     `TASK_TOO_BROAD_REQ_COVERAGE` (a task covers too many requirements).
   - Decomposition/atomicity constants (`MIN_TASKS_FLOOR`, `MIN_TASKS_PER_REQ`,
     `MIN_TASKS_PER_REMAINING`, `ATOMICITY_MAX_REQS_PER_TASK`) are internal
     to Manager and are not exposed in `config.yaml`.
   - Requires configured OpenAI API and an active CLI session.

8. **SDD mode**
   - Enable via `/sessions` -> `SDD` - the mode drives a feature through `specify`, `plan`, `tasks`, and `analyze` with approval and revision gates at each stage.
   - Specification and plan artifacts are written to `specs/<NNN>-<slug>/spec.md` and `plan.md`; tasks are written to `tasks.md`, and the requirements-vs-plan coverage report to `analyze.md`; at the terminal `analyze` gate key decisions are recorded in `.cli-proxy/decisions.md` (ADR), after which the accepted plan is handed off to Manager via `MANAGER_CONTINUE_TOKEN`.
   - The SDD menu includes `Initialize project`: for an existing codebase the mode first refreshes Codebase Mapper, then writes `specs/_project/project-profile.generated.md`, `pack_manifest.json`, and related SDD artifacts; for an empty directory it creates templates under `specs/_templates/`.
   - Initialization selects extensible artifact packs from project evidence (`Cargo.toml`, `sdkconfig`, `platformio.ini`, `.csproj`, `pyproject.toml`, `package.json`, OpenAPI/AsyncAPI, and other markers). If built-in packs do not cover the project, it creates a proposed pack under `.cli-proxy/sdd/packs/proposed/`; proposed packs are not auto-selected on later runs and must be manually promoted to project packs before they are treated as confirmed rules.
   - You can start directly from the original intent or route the intent through Analyst first, then continue SDD from the refined specification.
   - Requires configured OpenAI API and a working directory.

9. **Analyst mode**
   - Enable via `/sessions` -> `Analyst Analyst` - the system converts the initial request into a complete technical specification.
   - During analysis it may use agent tools (brainstorming, sequential thinking, web research, and CLI analysis via commands).
   - If data is missing, it asks clarifying questions and supports answers via buttons or `Custom Custom answer`.
   - Agent, Manager, and Analyst are mutually exclusive modes.

10. **Webmaster mode**
   - Enable via `/sessions` -> `Webmaster Webmaster` - the mode executes intent/confirm/dev/validation cycle and returns a structured defects/checklist report.
   - On invalid JSON from `intent_plugin`, graceful fallback is used and the process does not crash.
   - Destructive callbacks (`reset`, `clean`, `disconnect`) are protected by busy-guard and are rejected while the session is running.

11. **Admin mode**
   - Mode control is only through `/admin ...` for the current context session: `/admin`, `help`, `enable`, `disable`, `status`, `incidents`, `actions`, `check`, `run`, `dry-run on|off`, `ack`, `mute`, `unmute`, `approvals list|revoke|clear`.
   - Admin enable/disable is stored separately in `admin_session_state` by `(chat_id, session_id)` and does not change `session.modes.active_mode`.
   - On `enable`, background `Monitor -> Analyzer -> Executor -> Notifier` loop starts via `TaskService`; on `disable` and session close, Admin tasks are cancelled.
   - Analyzer Rule Engine has priority over LLM fallback and handles mandatory scenarios: `502 + php-fpm down -> restart_php_fpm`, `postgresql down -> restart_postgresql`, `cpu_high -> notify_admin`, `ssl expiring -> notify_admin`.
   - Executor applies Security Policies (cooldown/rate-limit/maintenance window/dry-run/mandatory notify), persists actions in `admin_actions`, incidents in `admin_incidents`, alert state in `admin_alerts_state`, and tracks notification delivery SLA metrics.
   - Risky and low-confidence decisions require `ask_user` confirmation; approved decisions are cached in `admin_approved_overrides` by hash (`action_id + params`) and reused without repeated prompt.
   - Approval-management commands: `/admin approvals list|revoke|clear`.

12. **Extras**
   - Git operations are available via `/git` (status, pull, diff, commit, summary, etc.).
   - Admins can use `/selfupdate` (code update + service restart).

## Inter-mode handoff (orchestrator)
- After a mode run finishes, orchestrator may suggest switching to another mode.
- On confirm (`✅`), the next mode input becomes the full payload from `session.orchestrator.last_mode_output`.
- Until confirmation, pending transition input is stored in `session.orchestrator.pending_input`.
- If no full payload is available, the next mode receives the original user request.
- Two sequential runs with different intent are isolated: handoff always uses data from the latest completed run only.

Example:
1. Manager completes a run and returns a full JSON report.
2. The result is stored in `session.orchestrator.last_mode_output`.
3. Orchestrator suggests switching to another mode.
4. After confirmation, the target mode receives that full JSON payload.

## Mode status model
- Base stages are unified via `ModeStatusService.build_common_mode_stage` / `build_common_mode_stage`: `disabled`, `processing task`, `waiting in queue`, `waiting for new request`.
- Domain modes may override stage text with specialized helpers: `build_manager_mode_stage` and `build_webmaster_mode_stage`.
- Agent and Analyst statuses include operational fields: `pending_questions`, `active_plugin_flow`, `template`, `queue_origin`.
- Fields `pending_questions`, `active_plugin_flow`, `template`, `queue_origin` are exposed in Telegram status text and in miniapp payload, so you can see exactly where the mode is waiting.

## Recent Behavior Updates
- **Analyst pre-run reset (Telegram + Desktop):** before Analyst run and when Analyst is enabled again, the app performs a narrow mode-scoped reset instead of a full session reset. It clears analyst-specific state, blocking clarification leftovers, stale pending questions, runtime cache, and marks the stale unfinished Analyst run artifact as finished.
- **Session fields preserved during reset:** Analyst pre-run reset preserves `session.project_root`, `session.agent_memory`, `session.modes.active_mode`, and `session.manager_quiet_mode`.
- **Desktop Agent runtime cleanup:** Desktop Agent cleanup actions (`clean_all` and `clean_session`) are functionally equivalent to Telegram and use real cleanup operations: `clear_sandbox`, `clear_session_files`, `clear_session_cache`.
- **Mode errors in CLI without silent fallback:** on internal `mode.handle_input` errors, input routing no longer silently falls back to plain CLI flow. The error is logged via `logger.exception` and explicitly re-raised.
- **Pending input queue:** pending inputs are stored as a per-chat FIFO queue (`deque`) with limit `5`. New inputs are appended; on overflow, the user gets a notification.
- **Desktop mode task telemetry:** terminal events are emitted without duplicates and classified as: `task:cancelled` (cancelled), `task:completed` (no exception), `task:failed` (exception present).
- **Manager resume/pending prompt on ask_user failure:** if `ask_user` fails while choosing resume action, the original pending prompt is kept, user is notified to retry, and pending is cleared only after successful resume.
- **Busy-guard for destructive callbacks:** while session is busy, `reset`/`clean`/`disconnect` are rejected for all modes by the shared callback router.
- **Mode-level callback audit:** each callback entry is logged to stdout with `session_id`, `mode`, `action`, `chat_id`, timestamp and `INFO` level.
- **Handoff full payload:** `session.orchestrator.last_mode_output` stores full final mode result and passes it to the next mode without reducing to `_plan_summary`.
- **Operational status details:** Agent/Analyst statuses now include `pending_questions`, `active_plugin_flow`, `template`, `queue_origin`.
- **Error logging:** all new `except` branches log via `logger.exception`.
- **Admin background pipeline:** full `Monitor -> Analyzer -> Executor -> Notifier` chain is enabled via `TaskService`, and is cancelled correctly on `/admin disable`, session close, or session switch.
- **Admin E2E scenarios:** verified chains `502 + php-fpm down -> restart -> notify` and `postgresql down -> ask_user -> restart`, including resilience to single-server local failures (other servers continue processing).
- **Admin approvals reuse:** risky-action approvals are reused through hash override (`admin_approved_overrides`), with `/admin approvals list|revoke|clear` commands.
- **Admin skill install approvals:** when `defaults.skill_install_policy=admin_approve`, auto-discovery does not write the skill immediately and instead creates a pending approval in the project-local ledger `.cli-proxy/skills/.skill_install_approval_ledger.json`; an admin resolves it through `/admin skills list|approve <approval_id>|reject <approval_id>`, the Desktop Admin panel, or the MiniApp Admin tab.
- **Admin destructive recovery guard:** native local/ssh remediation explicitly bypasses the skill selector, and doctor does not auto-replay destructive execution; only rebuilt-state/manual confirmation outcomes are allowed (`manual_review_required`, safe restart from boundary).

## Project Prompts for Manager/Webmaster
- **File locations:** project prompt files are stored inside the session workdir:
  - `.cli-proxy/.manager/prompt/prompts.yaml`
  - `.cli-proxy/.manager/prompt/learning.yaml`
  - `.cli-proxy/.webmaster/prompt/prompts.yaml`
  - `.cli-proxy/.webmaster/prompt/learning.yaml`
- **Auto-bootstrap:** files are created automatically when a session is created (`SessionManager.create`) and additionally verified when mode is enabled (Manager/Webmaster). For Manager, `prompts.yaml` is initialized from a full built-in template (UI + orchestrator prompts), and `learning.yaml` is created as a base YAML file.
- **Invalid YAML behavior:** if `prompts.yaml` or `learning.yaml` is broken, fails schema validation (including required prompt keys), mode shows an explicit UI error, logs traceback via `logger.exception`, and mode is not started.
- **Runtime source:** Manager/Webmaster read prompts only from project YAML in the current `session.workdir`. Prompts are reloaded on every mode enable, so YAML edits take effect after re-enable without restarting the process.

## Cross-CLI Session Transfer
- When the active CLI is switched, the bot offers to transfer context from the previous CLI if it has a `resume_token`.
- Transfer is compact by default: the full transcript is read from the source CLI native file and stored as an evidence pack in `.cli-proxy/session-transfer/<transfer-id>/`, while the target CLI receives a short session capsule instead of the whole history.
- The evidence pack contains `manifest.json`, `canonical.jsonl`, and `capsule.md`. The target agent gets these paths and should inspect them only when older context is needed.
- Low-level native writers remain available for compatibility and tests, but the main user flow no longer injects the full transcript into the target CLI context.

## Configuration
`config.yaml` supports:
- `telegram.whitelist_chat_ids`: allowed Telegram chat id list (mandatory access control)
- `telegram.user_modes`: per-user mode allowlist; supports registered modes (`agent`, `analyst`, `manager`, `sdd`, `webmaster`, `codebase_mapper`), virtual `direct_cli` and `orchestrator`, and `"all"` includes both registered modes and these virtual tokens
- `tools.*`: launch commands, mode, prompt/resume/help (including `resume_cmd` for dedicated resume commands) and `image_cmd` (appended to base command/resume_cmd for image handling)
- `defaults.*`: base dir, timeouts, state path, OpenAI settings, `zai_api_key`/`tavily_api_key`/`jina_api_key` for web search/reader, `github_token` for HTTPS git, `gemini_oauth_client_secret` for refreshing Gemini quota credentials
- `defaults.log_path`: bot log file path (main log). Errors are written to a separate `*_error.log` file with the same rotation rules.
- `defaults.image_temp_dir`: temp image directory (relative to workdir or absolute).
- `defaults.image_max_mb`: max size for a single image (default 10 MB).
- `defaults.manager_*`: Manager mode settings - max tasks, max attempts, decompose/dev/review timeouts, report truncation, auto-resume, auto-commit (`manager_auto_commit`) after each approved step, response archive in `.cli-proxy/.manager/response/`, prompt-learning data in `.cli-proxy/.manager/prompt/`
- `defaults.cli_json_stream_archive_enabled`: enables raw/normalized JSONL archive for JSON-stream CLIs in `.cli-proxy/cli-json-stream/`; useful for schema-drift and CLI-completion debugging
- `defaults.assistant_preview_enabled`: when enabled, Telegram and Desktop show a single live preview built from the latest CLI `assistant_text`, update it in place during the run, and remove it after the final answer
- Manager dynamic-decomposition and atomicity constants stay internal in
  `agent/manager.py` and are not configurable via `config.yaml`.
- `defaults.analyst_use_cli_timeout_sec` / `defaults.webmaster_use_cli_timeout_sec`: dedicated `use_cli` timeouts for Analyst/Webmaster
- `defaults.webmaster_validation_max_fix_iterations`: maximum internal fix iterations after failed validation
- `defaults.codebase_mapper_usage`: Mapper operation mode (`auto|enabled|disabled`; currently `auto` behaves as `enabled`)
- `defaults.run_artifacts_*`: durable run traces and retention cleanup (`STATE/PLAN/CHECKPOINTS/METRICS/RECOVERY/EVENTS`) used by doctor/recover/resume and UI run panels
- `defaults.run_doctor_enabled` / `defaults.run_boundary_validation_enabled` / `defaults.run_metrics_enabled`: enable doctor backend, phase gates, and per-run token/cost/duration aggregates in `METRICS.json`
- `defaults.skill_discovery_mode`: `off` disables selection, `suggest` injects only already available skills, `auto` allows discover/install of allowlisted skills into the project-local registry
- `defaults.skill_install_policy`: `manual` only suggests a skill, `admin_approve` writes a pending approval into `.cli-proxy/skills/.skill_install_approval_ledger.json` and requires explicit admin approve/reject, `allowlisted_auto` permits automatic installation of allowlisted discoveries into the project-local registry
- `defaults.skill_registry_paths` / `defaults.skill_allowlisted_sources`: project/global registry roots and allowlisted sources from which skill runtime may select or install skills
- `mcp.*`: TCP bridge for external clients (host/port/token)
- `mcp_clients`: canonical serialized list of MCP clients / HTTP endpoints
- `miniapp.*`: embedded admin HTTP MiniApp (`enabled`, `bind_host`, `bind_port`, `base_path`, `public_url`, `max_edit_file_size_kb`, `enable_delete`)
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
- Canonical mode schemas are located in:
  - `modes/manager/schemas.py`
  - `modes/webmaster/schemas.py`
  - `modes/agent/schemas.py`
  - `modes/analyst/schemas.py`
  - `modes/sdd/schemas.py`
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
- MiniApp and Desktop use the same policy: diff reports `applied` only for hot-reload fields,
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
- `/sessions` - session management menu (new/list/status/rename/close/resume/state/queue/clearqueue/reset + agent/manager/SDD/analyst/webmaster/mapper)
- `/interrupt` - interrupt generation
- `/git` - Git actions for the current context session (inline menu: status/fetch/pull/merge/rebase/diff/log/stash/commit/push/summary/help)
- `/files` - file management in the working directory (send/save/rename/delete)
- `/miniapp` - open the administration MiniApp
- `/reports` - show, create, or send reports for the current session (`latest`, `snapshot`, `generate`, `<filename.md>`)
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
- `/admin` - open Admin menu and brief status for the current context session
- `/admin help|enable|disable|status` - core Admin mode controls
- `/admin incidents [N] | /admin actions [N]` - incidents/actions journal for the current session
- `/admin check <server_id> | /admin run <action_id> <server_id>` - run check/run only for server/action from session admin config
- `/admin dry-run on|off` - toggle dry-run for the current session
- `/admin ack <incident_id> | /admin mute <minutes> | /admin unmute` - incident acknowledgement and mute controls
- `/admin approvals list|revoke <id>|clear` - manage approved overrides
- `/admin skills list|approve <approval_id>|reject <approval_id>` - inspect and resolve pending skill install approvals for `skill_install_policy=admin_approve`

## Run operations: `doctor` / `recover` / `resume`
- Analyst, Agent, Manager, and Webmaster inline panels expose explicit run operations: `doctor`, `recover`, `resume`.
- The same actions are available in Desktop through the `Runs` panel and in MiniApp through `Status -> Runs`.
- `doctor` reads current `STATE.json`, `PLAN.json`, `CHECKPOINTS.json`, `METRICS.json`, `EVENTS.jsonl`, plus the mode legacy store, then writes a structured result into `RECOVERY.json`.
- `recover` records a safe recovery intent based on the doctor recommendation and does not perform arbitrary mid-step replay.
- `resume` is allowed only from a safe boundary selected by doctor/recover; concurrent invocation is blocked by busy/lock/tick guards.
- These run operations are different from `/resume [token]`: the slash command manages the CLI resume token, while `doctor/recover/resume` buttons operate on runtime recovery for the current run.

## State
- `state.json` - stores per-session state: `resume_token`, `summary/updated_at`, queue, active mode (`agent`/`manager`/`sdd`/`analyst`/`webmaster`/`codebase_mapper`), agent memory, `project_root`.
- `state.json` - stores inter-mode handoff/orchestrator fields: `advanced_orchestrator_enabled`, `orchestrator_pending_input`, `orchestrator_last_mode_output`, `orchestrator_last_mode_id`.
- `state.json` - stores sessions list and scope-bound mappings for restart restore.

## Tests
```bash
pytest -q
```

## Direct CLI input
If you need to send a line starting with `/`, use either:
- `/send /help`
- or prefix `>` in a normal message: `> /help`
