# API Spec: `modes/admin/runner_service.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class AdminModeRunnerServiceError(RuntimeError)` (line 30)
*Raised when admin runner request is invalid.*

### `class AdminMonitorAnalyzerStepResult` (line 35)

### `class AdminExecutorNotifierStepResult` (line 41)

### `class AdminPipelineStepResult` (line 55)

### `class AdminModeRunnerService` (line 66)
*Admin mode-owned monitor/analyzer runtime.*
- `def __init__(config)` (line 71)
- `def set_config(config)` (line 96)
- `def ensure_notifier()` (line 99)
- `def is_pipeline_ready()` (line 106)
- `async def run_monitor_analyzer_once()` (line 114)
- `async def run_monitor_analyzer_loop()` (line 192)
- `async def run_executor_notifier_once()` (line 241)
- `async def run_pipeline_once()` (line 338)
- `def supports_capability(capability)` (line 1033)
