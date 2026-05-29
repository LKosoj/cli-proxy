# API Spec: `modes/admin/executor.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class AdminExecutorError(RuntimeError)` (line 31)
*Raised when admin execution request is invalid.*

### `class AdminExecutionContext` (line 36)

### `class AdminExecutionResult` (line 49)

### `class _AdminActionLogStore(Protocol)` (line 59)
- `def create_action(action_id)` (line 60)
- `def create_incident(incident_id)` (line 70)
- `def create_alert_state(alert_id)` (line 80)
- `def list_actions(session_id)` (line 90)
- `def create_approved_override(override_id)` (line 93)
- `def get_approved_override(override_id)` (line 103)

### `class _SecurityPolicyDecision` (line 108)

### `class AdminExecutor` (line 115)
- `async def execute()` (line 116)
- `async def apply_analyzer_decision()` (line 538)
