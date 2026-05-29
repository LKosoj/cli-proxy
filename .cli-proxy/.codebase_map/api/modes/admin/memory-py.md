# API Spec: `modes/admin/memory.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class ServerMemoryError(RuntimeError)` (line 30)
*Raised when server memory operation fails.*

### `class NoteEntry` (line 35)

### `class ServerMemory` (line 53)
*Memory-store для конкретного сервера:*
- `def __init__(workdir, server_id)` (line 60)
- `def facts_file()` (line 69)
- `def notes_file()` (line 73)
- `def get_facts()` (line 78)
- `def update_fact(key, value)` (line 89)
- `def delete_fact(key)` (line 112)
- `def get_notes()` (line 134)
- `def append_note(text)` (line 139)
- `def iter_note_entries()` (line 163)
- `def notes_stats()` (line 192)
- `def should_compact()` (line 199)
- `def compact_notes()` (line 207)

## Symbols
- `def memory_dir(workdir, server_id)` (line 41)
- `def facts_path(workdir, server_id)` (line 45)
- `def notes_path(workdir, server_id)` (line 49)
