# API Spec: `tests/test_admin_autonomy_e2e.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class _StubScanner(AdminBaselineScanner)` (line 33)
*Скан-стаб: отдаёт заранее подготовленные профили по очереди.*
- `def __init__(profiles)` (line 36)
- `async def scan(server)` (line 42)

### `class _FakeToolResult` (line 56)
- `def __init__()` (line 57)
- `def to_dict()` (line 73)

### `class _ActionRecorder` (line 77)
- `def __init__()` (line 78)
- `async def __call__()` (line 81)

## Symbols
- `def test_e2e_autonomy_tick_executes_action_and_updates_counters(tmp_path)` (line 131)
  - *Полный цикл: baseline → drift → runbook match → execute → counters.*
- `def test_e2e_autonomy_tick_respects_allowlist_and_escalates(tmp_path)` (line 185)
  - *auto_exec_actions не содержит action_id runbook'а → escalate, runner не вызывается.*
- `def test_e2e_autonomy_disabled_returns_shape_and_does_nothing(tmp_path)` (line 221)
  - *enabled=false → loop не стартует, но tick возвращает разумный shape.*
- `def test_e2e_autonomy_disabled_updates_last_tick_ts_but_not_count(tmp_path)` (line 235)
  - *При policy.enabled=False — last_tick_ts обновляется (scan состоялся),*
- `def test_llm_decision_maker_parses_valid_response()` (line 258)
- `def test_llm_decision_maker_falls_back_on_timeout()` (line 274)
- `def test_llm_decision_maker_falls_back_on_non_json_response()` (line 287)
- `def test_llm_decision_maker_strips_code_fences()` (line 298)
- `def test_llm_decision_maker_coerces_execute_without_action_id_to_escalate()` (line 308)
  - *Defense-in-depth: execute без action_id сразу вырождается в escalate.*
- `def test_e2e_llm_execute_on_alarm_is_gated(tmp_path)` (line 322)
  - *Даже если LLM говорит execute на alarm, policy-gate обязан escalate'ить.*
