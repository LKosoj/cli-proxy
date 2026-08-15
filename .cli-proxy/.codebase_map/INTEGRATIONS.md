# Integrations

Обновлено по полному `rg --files` и выборочному чтению ключевых файлов фокуса `tech`.

## Telegram
- **Bot API / polling** — `python-telegram-bot==22.6`; bootstrap and handlers: `bot.py`, `tg/wiring.py`.
- **Core commands** — registry in `tg/command_registry.py`; includes session control, git/files, MiniApp, limits, modes and lint evolution admin commands.
- **Callbacks** — centralized callback router and action modules: `tg/callbacks.py`, `tg/callback_actions/*`.
- **Thread mode** — `chat:*` / `thread:*` scopes and forum-topic routing: `sessions/conversation_scope.py`, `app/services/session_thread_manager.py`, `tg/command_policy.py`.
- **Outbound formatting** — Markdown/entities transport and chunking: `tg/markdown.py`, `app/services/telegram_transport.py`.

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
