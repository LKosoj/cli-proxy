# API Spec: `agent/cli_routing.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class RoutedCallError(Exception)` (line 126)

## Symbols
- `def get_priority_list(config, work_type)` (line 85)
- `def pick_candidates(config, work_type)` (line 112)
- `def temporary_session_cli(session, cli_name)` (line 137)
  - *Temporarily switch session.tool/active_cli for a single call and restore afterwards.*
- `async def run_prompt_routed(session, config, work_type, prompt)` (line 161)
  - *Run a prompt via the first available CLI by priority for the given work type.*
- `async def run_prompt_routed_meta(session, config, work_type, prompt)` (line 193)
  - *Same as run_prompt_routed(), but returns the CLI name that succeeded.*
