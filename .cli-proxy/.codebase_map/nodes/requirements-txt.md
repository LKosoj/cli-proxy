# Node: requirements.txt

Generated: 2026-06-03T02:24:29Z

## Purpose
Single source of pinned Python dependencies for the whole project (`requirements.txt` at repo root). One flat list, no extras/groups — the same file installs the bot, desktop client, MiniApp and the test/lint stack. Consumed by the installer (`setup_bot.sh`), by CI (`.github/workflows/ci.yml`), and bundled into the source artifact (`utils/source_artifact.py`).

## Scope
- Source glob: `requirements.txt` (matches only the repo-root file).
- Estimated files: 1.
- Current pins (`requirements.txt:1-32`), grouped by role:
  - Transport / Telegram: `python-telegram-bot==22.7`, `telegramify-markdown==1.0.0rc5`.
  - Markdown rendering: `markdown-it-py==3.0.0`, `mdit-py-plugins==0.4.1`, `linkify-it-py==2.0.3`.
  - HTTP / web: `httpx>=0.27.0,<0.29.0`, `aiohttp>=3.9.0`, `requests==2.32.3`, `beautifulsoup4>=4.12.0`, `trafilatura>=1.6.0`, `pdfminer.six>=20221105`, `duckduckgo-search==7.5.2`.
  - LLM / agents: `openai>=1.0.0`, `tiktoken>=0.7.0`.
  - CLI runtime: `pexpect==4.9.0`, `ansi2html==1.9.1`.
  - Config / validation: `PyYAML==6.0.2`, `pydantic>=2.0.0,<3.0.0`, `jsonschema>=4.21.0`, `pathspec>=0.11.0`.
  - Persistence: `SQLAlchemy>=2.0.0,<3.0.0`.
  - Remote ops: `asyncssh>=2.14.0,<3.0.0`.
  - Media: `gTTS==2.5.4`, `youtube-transcript-api==1.2.2`.
  - Desktop UI: `PySide6==6.8.2`, `qasync==0.27.1`.
  - Test / lint: `pytest==8.3.4`, `pytest-asyncio==0.25.3`, `pytest-xdist==3.8.0`, `pytest-qt==4.5.0`, `flake8`.
- Out of scope: provider API keys and runtime config (`.env`, `config.yaml`, `config_example.yaml`); the empty MiniApp npm surface (`miniapp/package.json`); per-dependency consumer mapping (see `STACK.md`).

## Instructions for agent
- Pin every new dependency explicitly here; the project uses exact/bounded pins (e.g. `==`, `>=x,<y`), so follow the existing style rather than adding unbounded `pkg`.
- `requirements.txt` is the dependency source of truth. Adding an `import` of a non-stdlib package without a matching pin here breaks `setup_bot.sh` and CI installs.
- When bumping pins, keep the bot, desktop and MiniApp consistent (they share this one file) and re-run the affected suites; this is the same stack documented in `STACK.md`.
- Test-stack pins (`pytest*`, `flake8`, `pytest-qt`) back `pytest.ini` and the lint gate — do not drop them; CI in `.github/workflows/ci.yml` installs from this file (`pip install -r requirements.txt`) and runs flake8 + pytest.
- This file is part of the distributable source artifact (`utils/source_artifact.py:32,48`) and is asserted present by the installer smoke test (`tests/smoke/test_setup_bot_script.py:23`) — keep the filename/location stable.
- After edits, validate with a real install and the suite: `pip install -r requirements.txt` then `pytest -q` and `flake8 .`.

## Source of truth
- `requirements.txt` (repo root) — the pinned dependency list itself.
- `setup_bot.sh:132` — installs the venv from this file (`pip install -r requirements.txt`).
- `.github/workflows/ci.yml:35,40` — pip cache key + install step keyed on this file.
- `utils/source_artifact.py:32,48` — includes `requirements.txt` in the exported source bundle.
- `tests/smoke/test_setup_bot_script.py:23` — asserts the file ships alongside `setup_bot.sh` / `config_example.yaml`.
- `STACK.md` — maps these pins to their runtime consumers (Telegram, aiohttp/MiniApp, OpenAI SDK, tiktoken, SQLAlchemy, asyncssh, PySide6, test stack).

## When to update
- Any commit touching `requirements.txt` (add/remove/bump a pin).
- New non-stdlib import added anywhere in the tree that needs a pin.
- Version bumps that change behavior of `pytest`/`pytest-asyncio`/`pytest-qt` (re-sync `pytest.ini`) or of config validation (`pydantic`, `jsonschema`).

## Related nodes
- `nodes/setup-bot-sh.md` — installer that creates the venv from this file.
- `nodes/pytest-ini.md` — test config backed by the `pytest*` pins here.
- `nodes/config-py.md` / `nodes/config-example-yaml.md` — config surface validated via `pydantic`/`PyYAML` pinned here.
- `nodes/bot-py.md` — entrypoint depending on `python-telegram-bot` + `aiohttp`.

## Owner
- project-maintainers

## Last reviewed
- 2026-06-03 (enriched: full pin inventory grouped by role, consumers in installer/CI/source-artifact/smoke-test, validation commands; verified against `requirements.txt`, `setup_bot.sh`, `.github/workflows/ci.yml`, `utils/source_artifact.py`, `tests/smoke/test_setup_bot_script.py`, `STACK.md`)
