# API Spec: `agent/manager_core.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class ManagerOrchestrator` (line 457)
*Manager mode: CLI does development, Agent (Executor) does review.*
- `def __init__(config)` (line 463)
- `async def run(session, user_text, bot, context, dest)` (line 1000)
- `def pause(session)` (line 3398)
- `def reset(session)` (line 3405)

## Symbols
- `def manager_run_phase_for_plan(plan)` (line 268)
- `def manager_legacy_phase_for_run_phase(phase)` (line 302)
- `def manager_legacy_plan_sync_payload(plan)` (line 312)
- `def manager_run_plan_payload(plan)` (line 336)
- `def manager_run_state_context_from_plan(plan)` (line 382)
- `def manager_apply_persisted_plan_metadata(target, persisted)` (line 419)
  - *Copy persistence-side metadata back onto the in-memory plan without*
