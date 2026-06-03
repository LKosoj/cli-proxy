# API Spec: `modes/admin/facade.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class ServerSummary` (line 81)
- `def status()` (line 94)
- `def to_dict()` (line 105)

### `class AdminAutonomyService` (line 163)
*Единая точка интеграции для UI (Telegram/MiniApp/Desktop) поверх новых компонентов:*
- `def __init__(workdir)` (line 170)
- `def list_server_specs()` (line 185)
- `def list_servers()` (line 194)
- `def get_server_summary(server_id)` (line 200)
- `def get_baseline(server_id)` (line 245)
- `def accept_baseline(server_id)` (line 260)
- `def discard_baseline_proposal(server_id)` (line 264)
- `async def rescan_server(server_id)` (line 270)
- `async def rescan_all()` (line 276)
- `def run_daily_maintenance()` (line 282)
- `def load_autonomy_policy()` (line 290)
- `def build_autonomy_loop()` (line 299)
- `async def run_autonomy_tick()` (line 316)
  - *Один тик автономии:*
- `def autonomy_status()` (line 401)
  - *Глобальный статус автономии: политика + кумулятивные счётчики.*
- `def get_memory(server_id)` (line 460)
- `def update_memory_fact(server_id)` (line 472)
- `def delete_memory_fact(server_id, key)` (line 481)
- `def append_memory_note(server_id, text)` (line 485)
- `def compact_memory(server_id)` (line 496)
- `def list_drifts(server_id)` (line 502)
- `def ack_drift(server_id, drift_id)` (line 518)
- `def get_snapshots(server_id, check_id)` (line 523)
- `def list_snapshot_checks(server_id)` (line 535)
- `def list_runbooks()` (line 541)
- `def list_runbook_summary()` (line 553)
- `def get_runbook(runbook_id)` (line 562)
- `def get_dossier(server_id)` (line 570)
- `def scan_script_sources(directory)` (line 597)
  - *Сканирует каталог под admin.runbook_sources, возвращает список .sh/.bash файлов.*
- `def read_script_from_source(path)` (line 603)
- `def create_runbook_from_scripts()` (line 608)
  - *Собирает runbook из спецификации.*
- `def check_server_prereqs(server_id)` (line 659)
  - *Вычисляет PrereqsReport по самым свежим наблюдениям сервера.*
- `def generate_bootstrap_runbook(server_id)` (line 707)
  - *Собирает bootstrap-runbook для установки недостающих admin-prereqs на сервере.*
- `async def validate_runbook(rb_id)` (line 788)
- `async def promote_runbook(rb_id)` (line 791)
- `async def run_runbook_step()` (line 807)
- `def global_summary()` (line 829)

## Symbols
- `def parse_server_specs(admin_cfg)` (line 122)
