# API Spec: `agent/manager_core.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class ManagerOrchestrator` (line 477)
*Manager mode: CLI does development, Agent (Executor) does review.*
- `def __init__(config)` (line 485)
- `async def run(session, user_text, bot, context, dest)` (line 1142)
- `def pause(session)` (line 3542)
- `def reset(session)` (line 3549)

## Symbols
- `def manager_run_phase_for_plan(plan)` (line 286)
- `def manager_legacy_phase_for_run_phase(phase)` (line 320)
- `def manager_legacy_plan_sync_payload(plan)` (line 330)
- `def manager_run_plan_payload(plan)` (line 356)
- `def manager_run_state_context_from_plan(plan)` (line 402)
- `def manager_apply_persisted_plan_metadata(target, persisted)` (line 439)
  - *Copy persistence-side metadata back onto the in-memory plan without*
