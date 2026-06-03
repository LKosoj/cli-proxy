# API Spec: `tests/test_admin_local_transport.py`

Generated: 2026-06-03T02:24:28Z

## Symbols
- `def test_local_subprocess_transport_executes_command_and_captures_stdout(tmp_path)` (line 22)
- `def test_local_subprocess_transport_times_out_long_running_command(tmp_path)` (line 41)
- `def test_local_subprocess_transport_rejects_empty_argv()` (line 59)
- `def test_local_subprocess_transport_rejects_non_positive_timeout(tmp_path)` (line 73)
- `def test_local_subprocess_transport_closes_pipe_transports(monkeypatch)` (line 92)
- `def test_local_subprocess_transport_logs_pipe_close_cleanup_failure(caplog)` (line 132)
- `def test_local_subprocess_transport_logs_kill_cleanup_failure(monkeypatch, caplog)` (line 152)
