# API Spec: `desktop/widgets/admin_panel.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class AdminPanel(QWidget)` (line 63)
*Desktop-панель Admin с локальным выбором сессии и разделами состояния.*
- `def __init__(facade)` (line 70)
- `def retranslate_ui(lang)` (line 622)
  - *Re-set all static UI strings using i18n.t(key, lang).*
- `def active_session_uid()` (line 719)
- `def refresh_sessions()` (line 722)
- `def set_session(session_uid)` (line 756)
- `def refresh_status_payload()` (line 811)
- `def closeEvent(event)` (line 1792)

### `class AdminAutonomyPanel(QWidget)` (line 1834)
*Отдельная секция: inventory, baseline, drift, memory, runbooks.*
- `def __init__(facade)` (line 1837)
- `def retranslate_ui(lang)` (line 1928)
- `def set_session(session_uid)` (line 1939)
- `def refresh_servers()` (line 1943)

### `class AdminAutonomyDetailDialog(QDialog)` (line 2147)
*Detail-диалог для одного сервера: overview/baseline/drifts/memory/runbooks.*
- `def __init__(facade, session_uid, server_id)` (line 2150)

### `class RunbookPromoteDialog(QDialog)` (line 3316)
*Диалог promote runbook: allowlist серверов, опциональный confidence, run_validation.*
- `def __init__()` (line 3319)
- `def result_payload()` (line 3357)
