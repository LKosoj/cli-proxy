# API Spec: `session.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class SessionCliSwitchResult` (line 217)

### `class CliState` (line 285)

### `class GitState` (line 294)

### `class ModeState` (line 302)

### `class OrchestratorState` (line 314)

### `class Session` (line 327)
- `def scope()` (line 523)
- `def scope(value)` (line 527)
- `def resume_token()` (line 531)
- `def resume_token(value)` (line 538)
- `def set_active_cli(cli_name)` (line 546)
  - *Switch the active CLI for this session.*
- `def reset_all_resume_tokens()` (line 564)
  - *Clear resume tokens for all CLIs in this session (and the active token view).*
- `async def run_prompt(prompt, image_path, image_paths)` (line 571)
- `def interrupt()` (line 1337)
- `def close_headless_process()` (line 1458)
- `def close()` (line 1477)
- `def is_active_by_tick(now, window_sec)` (line 1840)

### `class SessionManager` (line 1956)
- `def __init__(config)` (line 1957)
- `def create(chat_id, tool_name, workdir)` (line 2062)
- `def sessions_for_chat(chat_id)` (line 2106)
- `def get(chat_id, session_id)` (line 2123)
- `def get_by_uid(session_uid)` (line 2143)
- `def get_by_scope(chat_id, message_thread_id)` (line 2180)
- `def close(chat_id, session_id)` (line 2199)
- `def close_by_uid(session_uid)` (line 2215)
- `def persist_session(chat_id, session_id)` (line 2306)

## Symbols
- `def ensure_cli_proxy_gitignored(workdir)` (line 60)
  - *Ensure ``.cli-proxy/`` is listed in the project's ``.gitignore``.*
- `def session_active_cli_name(session)` (line 118)
  - *Return the session's current active CLI name from nested or legacy state.*
- `def pick_runtime_available_cli(config, preferred)` (line 127)
  - *Pick a CLI that is enabled and currently available for execution.*
- `def remember_session_cli_switch_notice(session, previous_cli, active_cli)` (line 148)
- `def consume_session_cli_switch_notice_text(session)` (line 162)
- `def switch_session_active_cli_if_needed(session)` (line 225)
  - *Ensure the session points to an executable CLI before direct execution.*
- `def session_runtime_uid(session)` (line 268)
  - *Return the canonical runtime session UID for real and fake session objects.*
- `def run_tool_help(tool, workdir, idle_timeout_sec)` (line 2510)
