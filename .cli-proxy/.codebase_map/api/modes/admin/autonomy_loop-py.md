# API Spec: `modes/admin/autonomy_loop.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class AutonomyDecision` (line 48)
*Что агент решил сделать с конкретным drift.*
- `def to_dict()` (line 58)

### `class DecisionContext` (line 70)

### `class DecisionMaker(Protocol)` (line 77)
- `async def decide(ctx)` (line 78)

### `class RuleBasedDecisionMaker` (line 81)
*Базовый decision maker без LLM:*
- `def __init__()` (line 92)
- `async def decide(ctx)` (line 95)

### `class LLMDecisionMaker` (line 152)
*Decision maker, использующий внешний LLM.*
- `def __init__(llm_caller)` (line 162)
- `async def decide(ctx)` (line 177)

### `class AdminAutonomyLoop` (line 255)
*Замыкает цикл: drifts → decision → policy gate → execute/escalate → audit note.*
- `def __init__(workdir, policy)` (line 261)
- `async def process_server_drifts(server_id, drifts)` (line 282)
- `def record_tick_start()` (line 557)
- `def maybe_auto_accept_baseline(server_id)` (line 567)
  - *Возвращает имя сервера, у которого baseline был авто-принят, либо None.*
