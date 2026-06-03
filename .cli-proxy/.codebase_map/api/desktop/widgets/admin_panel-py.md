# API Spec: `desktop/widgets/admin_panel.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class AdminPanel(QWidget)` (line 62)
*Desktop-панель Admin с локальным выбором сессии и разделами состояния.*
- `def __init__(facade)` (line 69)
- `def active_session_uid()` (line 492)
- `def refresh_sessions()` (line 495)
- `def set_session(session_uid)` (line 529)
- `def refresh_status_payload()` (line 581)
- `def closeEvent(event)` (line 1396)

### `class AdminAutonomyPanel(QWidget)` (line 1438)
*Отдельная секция: inventory, baseline, drift, memory, runbooks.*
- `def __init__(facade)` (line 1441)
- `def set_session(session_uid)` (line 1520)
- `def refresh_servers()` (line 1524)

### `class AdminAutonomyDetailDialog(QDialog)` (line 1702)
*Detail-диалог для одного сервера: overview/baseline/drifts/memory/runbooks.*
- `def __init__(facade, session_uid, server_id)` (line 1705)

### `class RunbookPromoteDialog(QDialog)` (line 2785)
*Диалог promote runbook: allowlist серверов, опциональный confidence, run_validation.*
- `def __init__()` (line 2788)
- `def result_payload()` (line 2825)
