# API Spec: `desktop/widgets/admin_panel.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class AdminPanel(QWidget)` (line 62)
*Минимальная desktop-панель Admin с локальным выбором сессии.*
- `def __init__(facade)` (line 69)
- `def active_session_uid()` (line 453)
- `def refresh_sessions()` (line 456)
- `def set_session(session_uid)` (line 490)
- `def refresh_status_payload()` (line 542)
- `def closeEvent(event)` (line 1305)

### `class AdminAutonomyPanel(QWidget)` (line 1347)
*Отдельная секция: inventory, baseline, drift, memory, runbooks.*
- `def __init__(facade)` (line 1350)
- `def set_session(session_uid)` (line 1429)
- `def refresh_servers()` (line 1433)

### `class AdminAutonomyDetailDialog(QDialog)` (line 1611)
*Detail-диалог для одного сервера: overview/baseline/drifts/memory/runbooks.*
- `def __init__(facade, session_uid, server_id)` (line 1614)

### `class RunbookPromoteDialog(QDialog)` (line 2694)
*Диалог promote runbook: allowlist серверов, опциональный confidence, run_validation.*
- `def __init__()` (line 2697)
- `def result_payload()` (line 2734)
