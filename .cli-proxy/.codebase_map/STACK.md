# Tech Stack

Обновлено по полному `rg --files` и выборочному чтению ключевых файлов фокуса `tech`.

## Runtime
- **Python 3.12** — CI matrix: `.github/workflows/ci.yml`; зависимости: `requirements.txt`; запуск: `bot.py`, `desktop/main.py`, `start_miniapp.py`.
- **asyncio** — основной async runtime для бота, MiniApp, scheduler, mode pipeline и desktop facade: `bot.py`, `desktop/services/application_facade.py`.
- **aiohttp** — общий HTTP listener и MiniApp/webhook surfaces: `app/services/shared_http_ingress.py`, `miniapp/server.py`, `app/services/webhook_ingress_service.py`.
- **python-telegram-bot==22.6** — Telegram polling, команды, callbacks, WebApp-кнопка: `bot.py`, `tg/wiring.py`, `tg/command_registry.py`.

## UI
- **Desktop: PySide6==6.8.2 + qasync==0.27.1** — Qt UI и asyncio loop: `desktop/main.py`, `desktop/main_window.py`, `desktop/widgets/*`.
- **MiniApp: static HTML/CSS/JS + aiohttp** — отдельного JS build stack нет: `miniapp/package.json` пустой; UI: `miniapp/static/index.html`, `miniapp/static/app.js`, `miniapp/static/styles.css`.
- **Browser-side assets** — Telegram WebApp SDK и Ace Editor подключаются через CDN в `miniapp/static/index.html`.

## Configuration
- **YAML config** — legacy dataclass surface: `config.py`; пример: `config_example.yaml`.
- **Pydantic 2 validation** — strict config models: `app/config_runtime/models.py`; loader/env overrides: `app/config_runtime/loader.py`; adapter back to dataclasses: `app/config_runtime/adapter.py`.
- **Env overrides** — `.env`, legacy `OPENAI_*`/provider keys и `CLI_PROXY__*`: `app/services/dotenv_loader.py`, `app/config_runtime/loader.py`.
- **Runtime reload** — reloadable/restart-required fields are centralized in `app/services/app_runtime_service.py`.

## Persistence
- **SQLite + SQLAlchemy 2.x** — shared runtime state repository with WAL settings: `app/services/state_repository.py`.
- **SQLite direct stores** — security audit/rate limits and job repositories: `app/security/audit.py`, `app/security/rate_limits.py`, `app/services/scheduled_job_repository.py`, `app/services/webhook_delivery_repository.py`.
- **JSON/YAML local artifacts** — run artifacts and project-local metadata under `.cli-proxy/*`: `app/services/run_artifact_store.py`, `app/services/ssh_config_loader.py`.

## Modes And Agents
- **Mode plugin loader** — scans `modes/*/__init__.py` for `PLUGIN`: `modes/registry.py`.
- **Mode SDK** — `BaseMode`, services and runtime contracts: `modes/sdk/base.py`, `modes/sdk/services/*`, `modes/sdk/runtime/contracts.py`.
- **Implemented modes** — `modes/agent`, `modes/analyst`, `modes/manager`, `modes/webmaster`, `modes/admin`, `modes/codebase_mapper`.
- **Tool plugin registry** — loads `agent/plugins/*.py`, validates schemas, supports parallel tool calls: `modes/sdk/runtime/tooling/registry.py`, `modes/sdk/runtime/tooling/loader.py`, `agent/plugins/base.py`.

## CLI And LLM Runtime
- **Configured CLIs** — Codex, Claude, Gemini, Qwen are defined under `tools.*` in `config_example.yaml`.
- **CLI execution** — sessions use subprocess/pexpect, resume tokens and CLI availability switching: `session.py`, `utils/cli.py`, `agent/cli_routing.py`.
- **CLI JSON streams** — adapters for Codex/Gemini/Qwen/Claude/Grok/Kimi/opencode: `app/services/cli_json_stream.py`; tmux transcript reader: `app/services/cli_backends/transcript_reader.py`; codex rollout tail: `app/services/cli_backends/codex_rollout_tail.py`.
- **OpenAI SDK** — async chat completions and summaries: `modes/sdk/runtime/openai_client.py`, `summary.py`.
- **Token counting** — `tiktoken` with fallback: `modes/sdk/runtime/token_counter.py`.

## Validation And Serialization
- **Central JSON normalizer** — JSON extraction, repair and jsonschema validation: `modes/sdk/runtime/json_normalizer.py`.
- **JSON Schema** — runtime/tooling/lint evolution validation: `modes/sdk/runtime/json_normalizer.py`, `app/services/lint_evolution/cli_classifier.py`.
- **Telegram Markdown entities** — `telegramify-markdown` wrapper and chunking: `tg/markdown.py`, `app/services/telegram_transport.py`.

## Network And Remote Ops
- **HTTP clients** — `httpx` for async HTTP and `requests` for sync plugin paths; dependencies in `requirements.txt`.
- **SSH** — `asyncssh` connection pool, command streaming and keygen: `app/services/ssh_service.py`; config/secrets: `app/services/ssh_config_loader.py`.
- **Remote shell/git** — remote command helpers and desktop Git remote fallback: `app/services/remote_shell_service.py`, `desktop/services/desktop_git_service.py`.
- **MCP** — server bridge and client-side stdio/http MCP manager: `app/services/mcp_bridge_service.py`, `modes/sdk/runtime/mcp/*`, `modes/sdk/runtime/tooling/mcp_plugin.py`.

## Testing And Quality
- **pytest==8.3.4**, **pytest-asyncio**, **pytest-xdist**, **pytest-qt** — test stack: `requirements.txt`, `pytest.ini`, `tests/**`.
- **flake8** — required lint gate: `requirements.txt`, `.github/workflows/ci.yml`.
- **CI** — Ubuntu/Windows/macOS, Python 3.12, lint, pytest+coverage, source artifact build and smoke tests: `.github/workflows/ci.yml`.

## Limitations
- Runtime was not started; facts are based on static scan and selective source reads.
- `miniapp/package-lock.json` exists, but `miniapp/package.json` is empty, so no npm build/toolchain is documented as active.
