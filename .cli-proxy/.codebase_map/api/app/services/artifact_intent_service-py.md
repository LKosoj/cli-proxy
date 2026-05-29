# API Spec: `app/services/artifact_intent_service.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class ArtifactIntent` (line 53)

### `class ArtifactResult` (line 59)

### `class ArtifactIntentService` (line 64)
*Detects 'send me a file' intent via a lightweight LLM call.*
- `async def classify(text)` (line 67)
  - *Return ArtifactIntent if user explicitly asks to receive a file, else None.*
- `def resolve(intent, project_root)` (line 112)
  - *Resolve file_pattern to an absolute path with safety checks.*
