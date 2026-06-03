# API Spec: `app/services/artifact_intent_service.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class ArtifactIntent` (line 56)

### `class ArtifactResult` (line 62)

### `class ArtifactIntentService` (line 67)
*Detects 'send me a file' intent via a lightweight LLM call.*
- `async def classify(text)` (line 70)
  - *Return ArtifactIntent if user explicitly asks to receive a file, else None.*
- `def resolve(intent, project_root)` (line 115)
  - *Resolve file_pattern to an absolute path with safety checks.*
