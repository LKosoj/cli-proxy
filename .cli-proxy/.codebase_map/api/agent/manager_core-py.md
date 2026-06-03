# API Spec: `agent/manager_core.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class ManagerOrchestrator` (line 468)
*Manager mode: CLI does development, Agent (Executor) does review.*
- `def __init__(config)` (line 476)
- `async def run(session, user_text, bot, context, dest)` (line 1111)
- `def pause(session)` (line 3497)
- `def reset(session)` (line 3504)

## Symbols
- `def manager_run_phase_for_plan(plan)` (line 277)
- `def manager_legacy_phase_for_run_phase(phase)` (line 311)
- `def manager_legacy_plan_sync_payload(plan)` (line 321)
- `def manager_run_plan_payload(plan)` (line 347)
- `def manager_run_state_context_from_plan(plan)` (line 393)
- `def manager_apply_persisted_plan_metadata(target, persisted)` (line 430)
  - *Copy persistence-side metadata back onto the in-memory plan without*
