# API Spec: `app/services/cli_json_stream.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class CliJsonStreamEvent` (line 51)
- `def to_record()` (line 61)

### `class BaseCliJsonStreamAdapter` (line 74)
- `def feed_line(line)` (line 77)
- `def final_output_text()` (line 80)
- `def session_id()` (line 84)
- `def completed()` (line 88)

### `class CodexJsonStreamAdapter(BaseCliJsonStreamAdapter)` (line 92)
- `def __init__()` (line 95)
- `def session_id()` (line 101)
- `def completed()` (line 105)
- `def final_output_text()` (line 108)
- `def feed_line(line)` (line 111)

### `class GeminiJsonStreamAdapter(BaseCliJsonStreamAdapter)` (line 312)
- `def __init__()` (line 315)
- `def session_id()` (line 322)
- `def completed()` (line 326)
- `def final_output_text()` (line 329)
- `def feed_line(line)` (line 332)

### `class QwenJsonStreamAdapter(BaseCliJsonStreamAdapter)` (line 453)
- `def __init__()` (line 456)
- `def session_id()` (line 463)
- `def completed()` (line 467)
- `def final_output_text()` (line 470)
- `def feed_line(line)` (line 473)

### `class ClaudeJsonStreamAdapter(BaseCliJsonStreamAdapter)` (line 633)
- `def __init__()` (line 636)
- `def session_id()` (line 643)
- `def completed()` (line 647)
- `def final_output_text()` (line 650)
- `def feed_line(line)` (line 653)

### `class CliJsonStreamRecorder` (line 883)
- `def __init__()` (line 884)
- `def raw_path()` (line 903)
- `def normalized_path()` (line 907)
- `def record_raw_line(line)` (line 910)
- `def record_event(event)` (line 918)
- `def close()` (line 926)

## Symbols
- `def cli_json_stream_archive_enabled(config)` (line 25)
- `def build_cli_json_stream_adapter(cli_name)` (line 838)
- `def recover_cli_text_from_raw_stream(cli_name, raw_text)` (line 851)
- `def extract_cli_evidence_from_normalized_stream(path)` (line 977)
