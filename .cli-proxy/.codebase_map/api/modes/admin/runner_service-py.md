# API Spec: `modes/admin/runner_service.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class AdminModeRunnerServiceError(RuntimeError)` (line 29)
*Raised when admin runner request is invalid.*

### `class AdminMonitorAnalyzerStepResult` (line 34)

### `class AdminExecutorNotifierStepResult` (line 40)

### `class AdminPipelineStepResult` (line 54)

### `class AdminModeRunnerService` (line 65)
*Admin mode-owned monitor/analyzer runtime.*
- `def __init__(config)` (line 70)
- `def set_config(config)` (line 95)
- `def ensure_notifier()` (line 98)
- `def is_pipeline_ready()` (line 105)
- `async def run_monitor_analyzer_once()` (line 113)
- `async def run_monitor_analyzer_loop()` (line 154)
- `async def run_executor_notifier_once()` (line 203)
- `async def run_pipeline_once()` (line 300)
- `def supports_capability(capability)` (line 919)
