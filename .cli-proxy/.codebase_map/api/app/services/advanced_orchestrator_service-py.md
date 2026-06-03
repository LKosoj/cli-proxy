# API Spec: `app/services/advanced_orchestrator_service.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class OrchestratorProposal` (line 23)

### `class ModePolicy` (line 32)

### `class AdvancedOrchestratorService` (line 41)
*Session-level intelligent mode router with deterministic guardrails.*
- `def propose_transition()` (line 185)
- `async def propose_transition_hybrid()` (line 229)
  - *Hybrid routing:*
- `def build_handoff_input()` (line 323)
  - *For accepted transition:*
- `def apply_mode()` (line 334)
- `def build_confirm_text()` (line 344)
- `def current_mode_label()` (line 355)
- `def mode_policies()` (line 368)
