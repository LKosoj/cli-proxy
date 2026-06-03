# API Spec: `session.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class SessionCliSwitchResult` (line 221)

### `class CliState` (line 289)

### `class GitState` (line 298)

### `class ModeState` (line 308)

### `class OrchestratorState` (line 320)

### `class SddState` (line 328)

### `class Session` (line 352)
- `def scope()` (line 551)
- `def scope(value)` (line 555)
- `def resume_token()` (line 559)
- `def resume_token(value)` (line 566)
- `def set_active_cli(cli_name)` (line 574)
  - *Switch the active CLI for this session.*
- `def reset_all_resume_tokens()` (line 592)
  - *Clear resume tokens for all CLIs in this session (and the active token view).*
- `async def run_prompt(prompt, image_path, image_paths)` (line 599)
- `def interrupt()` (line 1351)
- `def close_headless_process()` (line 1472)
- `def close()` (line 1491)
- `def is_active_by_tick(now, window_sec)` (line 1869)

### `class SessionManager` (line 2087)
- `def __init__(config)` (line 2088)
- `def create(chat_id, tool_name, workdir)` (line 2210)
- `def sessions_for_chat(chat_id)` (line 2255)
- `def get(chat_id, session_id)` (line 2272)
- `def get_by_uid(session_uid)` (line 2292)
- `def get_by_scope(chat_id, message_thread_id)` (line 2339)
- `def close(chat_id, session_id)` (line 2358)
- `def close_by_uid(session_uid)` (line 2375)
- `def serialize_chat_entry_for_persist(chat_id, session_id)` (line 2493)
  - *Сериализует chat-entry на ВЫЗЫВАЮЩЕМ потоке (ожидается event loop).*
- `def write_chat_entry(chat_id, entry)` (line 2509)
  - *Пишет уже сериализованную chat-entry в state-repo. Безопасно из worker-потока.*
- `def persist_session(chat_id, session_id)` (line 2519)

## Symbols
- `def ensure_cli_proxy_gitignored(workdir)` (line 64)
  - *Ensure ``.cli-proxy/`` is listed in the project's ``.gitignore``.*
- `def session_active_cli_name(session)` (line 122)
  - *Return the session's current active CLI name from nested or legacy state.*
- `def pick_runtime_available_cli(config, preferred)` (line 131)
  - *Pick a CLI that is enabled and currently available for execution.*
- `def remember_session_cli_switch_notice(session, previous_cli, active_cli)` (line 152)
- `def consume_session_cli_switch_notice_text(session)` (line 166)
- `def switch_session_active_cli_if_needed(session)` (line 229)
  - *Ensure the session points to an executable CLI before direct execution.*
- `def session_runtime_uid(session)` (line 272)
  - *Return the canonical runtime session UID for real and fake session objects.*
- `def run_tool_help(tool, workdir, idle_timeout_sec)` (line 2734)
