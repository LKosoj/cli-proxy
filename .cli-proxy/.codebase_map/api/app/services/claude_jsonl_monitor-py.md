# API Spec: `app/services/claude_jsonl_monitor.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class ClaudeTranscriptEvent` (line 290)
- `def from_jsonl_line(cls, line)` (line 299)

### `class ClaudeSessionMonitor` (line 320)
- `def read_new_events()` (line 360)

### `class ClaudeJsonlMonitor` (line 397)
*Track one active Claude transcript file for the current workdir.*
- `def __init__(workdir, callback, session_callback, poll_interval, username, session_id)` (line 406)
- `def start()` (line 636)
- `def stop()` (line 645)
- `def get_latest_session_id()` (line 652)

## Symbols
- `def extract_progress_texts(raw_event)` (line 211)
  - *Extract human-readable progress messages from a Claude transcript event.*
