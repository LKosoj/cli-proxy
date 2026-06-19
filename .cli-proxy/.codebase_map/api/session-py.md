# API Spec: `session.py`

Generated: 2026-06-17T10:46:19Z

## Classes
### `class SessionCliSwitchResult` (line 222)

### `class CliState` (line 290)

### `class GitState` (line 299)

### `class ModeState` (line 309)

### `class OrchestratorState` (line 321)

### `class SddState` (line 329)

### `class Session` (line 353)
- `def scope()` (line 552)
- `def scope(value)` (line 556)
- `def resume_token()` (line 560)
- `def resume_token(value)` (line 567)
- `def set_active_cli(cli_name)` (line 575)
  - *Switch the active CLI for this session.*
- `def reset_all_resume_tokens()` (line 593)
  - *Clear resume tokens for all CLIs in this session (and the active token view).*
- `async def run_prompt(prompt, image_path, image_paths)` (line 618)
- `def interrupt()` (line 1377)
- `def close_headless_process()` (line 1498)
- `def close()` (line 1517)
- `def is_active_by_tick(now, window_sec)` (line 1895)

### `class SessionManager` (line 2113)
- `def __init__(config)` (line 2114)
- `def create(chat_id, tool_name, workdir)` (line 2236)
- `def sessions_for_chat(chat_id)` (line 2281)
- `def get(chat_id, session_id)` (line 2298)
- `def get_by_uid(session_uid)` (line 2318)
- `def get_by_scope(chat_id, message_thread_id)` (line 2365)
- `def close(chat_id, session_id)` (line 2384)
- `def close_by_uid(session_uid)` (line 2401)
- `def serialize_chat_entry_for_persist(chat_id, session_id)` (line 2519)
  - *Сериализует chat-entry на ВЫЗЫВАЮЩЕМ потоке (ожидается event loop).*
- `def write_chat_entry(chat_id, entry)` (line 2535)
  - *Пишет уже сериализованную chat-entry в state-repo. Безопасно из worker-потока.*
- `def persist_session(chat_id, session_id)` (line 2545)

## Symbols
- `def ensure_cli_proxy_gitignored(workdir)` (line 64)
  - *Ensure ``.cli-proxy/`` is listed in the project's ``.gitignore``.*
- `def session_active_cli_name(session)` (line 122)
  - *Return the session's current active CLI name from nested or legacy state.*
- `def pick_runtime_available_cli(config, preferred)` (line 131)
  - *Pick a CLI that is enabled and currently available for execution.*
- `def remember_session_cli_switch_notice(session, previous_cli, active_cli)` (line 152)
- `def consume_session_cli_switch_notice_text(session, lang)` (line 166)
- `def switch_session_active_cli_if_needed(session)` (line 230)
  - *Ensure the session points to an executable CLI before direct execution.*
- `def session_runtime_uid(session)` (line 273)
  - *Return the canonical runtime session UID for real and fake session objects.*
- `def run_tool_help(tool, workdir, idle_timeout_sec, lang)` (line 2760)
