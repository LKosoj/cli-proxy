# API Spec: `modes/admin/mode.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class AdminMode(BaseMode, RunArtifactsMixin)` (line 178)
- `def __init__(dependencies)` (line 196)
- `def build_runtime(config)` (line 209)
- `async def on_enable(ctx)` (line 212)
- `async def on_disable(ctx)` (line 255)
- `async def handle_input(message, ctx)` (line 412)
- `def build_status_payload()` (line 1372)
- `async def handle_callback(callback, ctx)` (line 3547)
- `def build_menu(session, back_callback, back_text)` (line 5358)
