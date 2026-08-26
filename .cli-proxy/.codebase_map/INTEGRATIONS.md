# Integrations

Обновлено по полному `rg --files` и выборочному чтению ключевых файлов фокуса `tech`.

## Telegram
- **Bot API / polling** — `python-telegram-bot==22.6`; bootstrap and handlers: `bot.py`, `tg/wiring.py`.
- **Core commands** — registry in `tg/command_registry.py`; includes session control, git/files, MiniApp, limits, modes and lint evolution admin commands.
- **Callbacks** — centralized callback router and action modules: `tg/callbacks.py`, `tg/callback_actions/*`.
- **Thread mode** — `chat:*` / `thread:*` scopes and forum-topic routing: `sessions/conversation_scope.py`, `app/services/session_thread_manager.py`, `tg/command_policy.py`.
- **Outbound formatting** — Markdown/entities transport and chunking: `tg/markdown.py`, `app/services/telegram_transport.py`.
- **Unread-отметка сессии** — ручной флаг «вернуться позже», живёт в `Session.unread` и персистится в `state.json` наравне с остальным состоянием сессии (`session.py`; отсутствие ключа в старом состоянии читается как `False`). Единая точка доступа — `is_session_unread`/`set_session_unread` (`sessions/session_state_access.py`). Переключается кнопкой обзора сессии `sess_unread_toggle:<uid>` (`tg/handlers.py::_unread_toggle_button`, обработчик `tg/callbacks.py::_cb_sess_unread_toggle`, видимость — action `unread` в `app/services/menu_visibility_policy.py`, доступен и не-админу). Маркер `🔵` показывается в заголовке статуса (`sessions/session_status.py`), в списке сессий (`sessions/session_ui.py`) и в селекторах сессий MiniApp (поле `unread` в payload `miniapp/routes.py`, отрисовка `miniapp/static/app.js`).

## MiniApp HTTP Surface
- **Shared ingress** — one aiohttp listener for MiniApp and webhooks: `app/services/shared_http_ingress.py`.
- **MiniApp mount** — base path from `miniapp.base_path`, default `/cli-proxy`: `miniapp/server.py`, `config.py`.
- **Auth** — Telegram WebApp `initData` HMAC verification through `SecurityFacade`: `miniapp/auth.py`, `miniapp/routes.py`, `app/security/facade.py`.
- **Main API routes** — config, files, logs, status websockets, runs, modes, scheduler, SSH and admin endpoints: `miniapp/routes.py`.
- **Static UI** — `miniapp/static/index.html`, `miniapp/static/app.js`, `miniapp/static/styles.css`; Ace Editor and Telegram SDK are loaded from CDN.

## Webhooks And Events
- **Webhook ingress** — `GET /health` and configured `POST webhooks.path`: `app/services/webhook_ingress_service.py`.
- **Webhook security** — secret headers `X-Telegram-Bot-Api-Secret-Token` / `X-Webhook-Secret-Token`: `app/services/webhook_ingress_service.py`.
- **Delivery dedupe** — delivery id claim storage: `app/services/webhook_delivery_repository.py`.
- **System event bus** — typed events for Telegram, MiniApp, Desktop, webhooks, scheduler, mode launch, notifications, security audit and runtime config reload: `app/events/bus.py`.

## Desktop
- **Entry point** — `desktop/main.py`.
- **Facade** — orchestration, mode runtime wiring, sessions, scheduler, SSH, run operations and notifications: `desktop/services/application_facade.py`.
- **UI shell** — Qt main window and widgets: `desktop/main_window.py`, `desktop/widgets/*`.
- **Desktop state** — JSON state file from `defaults.desktop_state_path`: `desktop/services/desktop_state_service.py`, `app/services/config_service.py`.
- **Desktop identity/projects** — project ownership and scheduler notification targets: `desktop/services/desktop_identity_provider.py`, `app/services/project_registry.py`.

## Modes
- **Mode discovery** — `ModeLoader` loads `PLUGIN` from mode packages: `modes/registry.py`.
- **Mode SDK contracts** — `BaseMode`, `PlanStep`, `ExecutorRequest`, `ExecutorResponse`, Manager plan models: `modes/sdk/base.py`, `modes/sdk/runtime/contracts.py`.
- **Mode dependencies** — typed dependency bundle with run artifacts, doctor, boundary validation, skills, tasks, dialogs and SSH: `app/mode_dependencies.py`.
- **Mode launch surfaces** — Telegram commands, MiniApp `/api/modes/launch`, Desktop facade and scheduler events: `tg/command_registry.py`, `miniapp/routes.py`, `desktop/services/application_facade.py`, `app/services/mode_launch_adapter.py`.

## CLI Providers
- **Configured tools** — `codex`, `claude`, `gemini`, `qwen` in `config_example.yaml`.
- **Execution contracts** — session execution, active CLI switching and routed calls: `session.py`, `agent/cli_routing.py`.
- **JSON/progress streams** — CLI stream adapters and transcript readers: `app/services/cli_json_stream.py`, `app/services/cli_backends/transcript_reader.py`, `app/services/cli_backends/codex_rollout_tail.py`.
- **Transcript anchoring** — prompts carry no service markers: the journal is bound by CLI `session_id` (or, without one, by the freshest file inside the workspace directories), the start offset is the first record newer than `started_at`, and the persisted `TranscriptLocator` is reused on later polls: `app/services/cli_backends/transcript_reader.py`, `app/services/cli_backends/tmux_backend.py`.
- **Prompt delivery into a pane** — the buffer is pasted with `tmux paste-buffer -p` (bracket markers), otherwise a CLI treats the following Enter as a newline inside the paste and the prompt stays in the input box: `app/services/cli_backends/tmux_driver.py`. Paste and Enter form one indivisible step: a cancellation in between (tmux reread, bot shutdown) still submits the pasted text, otherwise the prompt would sit in the CLI input box while the bot waits for an answer that never comes: `app/services/cli_backends/tmux_backend.py`.
- **Санитизация ESC во вставляемом prompt** — `write_prompt_temp` заменяет каждый байт `\x1b` (ESC) на печатный суррогат `␛` (U+241B) перед `paste-buffer -p`: `paste-buffer -p` оборачивает вставку в bracketed-paste маркеры `ESC[200~ … ESC[201~`, и «живой» ESC внутри текста промпта (например, из пересланного в Telegram лога с ANSI-раскраской) закрывал рамку досрочно, из-за чего хвост промпта попадал в CLI как отдельные нажатия клавиш: `app/services/cli_backends/tmux_driver.py`.
- **Env-контракт для native-хуков CLI** — переменная окружения `CLI_PROXY_SESSION_UID` (значение — канонический `session_runtime_uid(session)`) прокидывается в процесс CLI: через `tmux -e KEY=VALUE` при создании панели (`TmuxDriver.new_session`, требует tmux ≥ 3.2) для tmux-backend, и напрямую через `env`/inline-префикс команды (для claude, запускаемого через `su -`, где login shell чистит окружение) в `session.py` (`_run_headless`, `_ensure_child`) для headless/interactive путей: `app/services/cli_backends/tmux_driver.py`, `app/services/cli_backends/tmux_backend.py`, `session.py`. Ту же переменную на старте читает `app/services/memory_native_hook_adapter.py` (`build_native_memory_event`/`record_native_hook_payload`, параметр `session_uid_override`, `main()`), чтобы события native-хуков CLI писались под тем же uid, что и события agent-режима (`app/services/task_bearing_cli_hook_service.py`) — до этого изменения два пространства id не пересекались и связать события было невозможно; исходный внутренний id самого CLI сохраняется в metadata как `native_session_id`.
- **Turn completion** — a turn is closed only by the CLI journal: `turn_duration` (claude), `task_complete` (codex), `turn_completed` (grok), `turn.ended` (kimi), assistant text without tool calls (qwen/gemini); the quiet timeout stays the last-resort signal: `app/services/cli_backends/transcript_reader.py`.
- **Диагностика по заголовку панели и BEL** — `PaneSignalScanner` (`app/services/cli_backends/pane_signals.py`) — stateful-сканер, разбирает сырой поток панели по кускам (escape-последовательность может разорваться на границе чанка, состояние переживает вызов): вытаскивает OSC-заголовки (Ps 0/1/2) и считает настоящие BEL-звонки (байт 0x07 внутри OSC — терминатор, а не звонок). `classify_pane_title` (`app/services/cli_backends/pane_title_status.py`) превращает заголовок в статус `working|permission|idle|None` по таблицам глифов Claude/Gemini или по generic-эвристике для остальных CLI. `tmux_backend.py` кладёт оба сигнала в `ExecutionResult.diagnostics` (`title_status`, `bell_count`) и пишет их строкой `tmux request finished ...` в `logs/bot.log` при завершении хода — журнал и есть потребитель сигнала, `diagnostics` дальше по коду читается только по ключам `transcript_path`/`failure_reason`. Это исключительно наблюдаемость: единственным авторитетным источником завершения хода остаётся транскрипт CLI, заголовок панели на это решение не влияет.
- **CLI limits** — usage/status summaries for supported CLIs: `app/services/cli_limits_service.py`.
- **CLI limits sources** — Claude OAuth usage API, Codex `app-server` JSON-RPC (`account/rateLimits/read`), Gemini `retrieveUserQuota`, Grok TUI probe, local transcripts (Claude/Qwen) and the opencode SQLite database: `app/services/cli_limits_service.py`.
- **Token cost estimates** — LiteLLM price list cached under `<state dir>/.cli-proxy/runtime/model_prices.json`: `app/services/model_pricing.py`.
- **Quota burn rate** — usage history for forecasts stored in `JsonStateRepository` namespace `_cli_limits_trend`: `app/services/cli_limits_trend.py`.

## Tool Plugins
- **Local tools** — all `agent/plugins/*.py` are loaded dynamically by `modes/sdk/runtime/tooling/loader.py`.
- **Tool registry** — schema validation, argument coercion, timeout handling, allowed tools and parallelizable execution: `modes/sdk/runtime/tooling/registry.py`.
- **Telegram plugin UI** — message handlers, inline handlers and two-level plugin menus: `agent/plugins/base.py`, `agent/telegram_wiring.py`.
- **Notable external plugin integrations** — web search/content, YouTube transcripts, image search, TTS/image/video tools, WolframAlpha, GitHub analysis, SSH exec, file tools: `agent/plugins/*`.

## MCP
- **TCP MCP bridge** — token-protected local bridge to `BotApp.run_prompt_raw`: `app/services/mcp_bridge_service.py`.
- **Client-side MCP servers** — configured by `mcp_clients` with `stdio` or `http`: `config.py`, `app/config_runtime/models.py`, `config_example.yaml`.
- **MCP manager** — starts clients, lists tools, caches schemas and calls remote tools: `modes/sdk/runtime/mcp/manager.py`, `modes/sdk/runtime/mcp/stdio_client.py`, `modes/sdk/runtime/mcp/http_client.py`.
- **MCP tools as plugins** — remote tools are exposed through `MCPRemoteToolPlugin`: `modes/sdk/runtime/tooling/mcp_plugin.py`.

## LLM And Web Services
- **OpenAI-compatible chat completions** — default and per-call models: `modes/sdk/runtime/openai_client.py`.
- **Summary model** — `defaults.openai_big_model` / env fallback: `summary.py`.
- **Provider/API keys** — OpenAI, Z.ai, Tavily, Jina, GitHub keys live in `defaults.*`: `config.py`, `app/config_runtime/models.py`.
- **Web/content extraction** — `duckduckgo-search`, `trafilatura`, `beautifulsoup4`, `pdfminer.six`, `youtube-transcript-api`: `requirements.txt`, `agent/plugins/*`.

## SSH And Remote Control
- **SSH runtime** — `asyncssh` pooled connections, exec/stream/cancel/keygen: `app/services/ssh_service.py`.
- **Project SSH config** — `{workdir}/.cli-proxy/ssh.yaml` and `{workdir}/.cli-proxy/ssh.env`: `app/services/ssh_config_loader.py`.
- **Remote shell/file/git** — command execution and remote Git support: `app/services/remote_shell_service.py`, `desktop/services/desktop_git_service.py`, `miniapp/services/files_service.py`.
- **MiniApp SSH API** — host CRUD, test connection, keygen and secret save: `miniapp/routes.py`.

## Scheduler
- **Cron parser and loop** — built-in 5-field cron, timezone support and misfire handling: `app/services/scheduler_service.py`.
- **Persistence** — scheduled jobs/audit records stored through SQLite repository: `app/services/scheduled_job_repository.py`.
- **Launch integration** — emits `ScheduledJobEvent`, records `ModeLaunchCompletedEvent`: `app/services/scheduler_service.py`, `app/events/bus.py`.
- **UI surfaces** — MiniApp scheduler routes and Desktop scheduler panel: `miniapp/routes.py`, `desktop/widgets/scheduler_panel.py`.

## Security
- **Facade** — unified auth, validation, audit and rate limits: `app/security/facade.py`.
- **Auth strategies** — config allowlists, token, OAuth verifier hook and Telegram MiniApp initData: `app/security/auth.py`.
- **Rate limits** — in-memory or SQLite sliding window: `app/security/rate_limits.py`, `config.py`.
- **Audit** — event bus and SQLite audit log store: `app/security/audit.py`, `app/events/bus.py`.

## Limitations
- Runtime endpoints were not exercised; route and integration facts are from static source reads.
- External service availability, credentials and MCP server health were not checked.
