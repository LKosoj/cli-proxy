# API Spec: `modes/admin/facade.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class ServerSummary` (line 81)
- `def status()` (line 94)
- `def to_dict()` (line 105)

### `class AdminAutonomyService` (line 162)
*Единая точка интеграции для UI (Telegram/MiniApp/Desktop) поверх новых компонентов:*
- `def __init__(workdir)` (line 169)
- `def list_server_specs()` (line 184)
- `def list_servers()` (line 193)
- `def get_server_summary(server_id)` (line 199)
- `def get_baseline(server_id)` (line 244)
- `def accept_baseline(server_id)` (line 259)
- `def discard_baseline_proposal(server_id)` (line 263)
- `async def rescan_server(server_id)` (line 269)
- `async def rescan_all()` (line 275)
- `def run_daily_maintenance()` (line 281)
- `def load_autonomy_policy()` (line 289)
- `def build_autonomy_loop()` (line 298)
- `async def run_autonomy_tick()` (line 315)
  - *Один тик автономии:*
- `def autonomy_status()` (line 400)
  - *Глобальный статус автономии: политика + кумулятивные счётчики.*
- `def get_memory(server_id)` (line 455)
- `def update_memory_fact(server_id)` (line 467)
- `def delete_memory_fact(server_id, key)` (line 476)
- `def append_memory_note(server_id, text)` (line 480)
- `def compact_memory(server_id)` (line 491)
- `def list_drifts(server_id)` (line 497)
- `def ack_drift(server_id, drift_id)` (line 513)
- `def get_snapshots(server_id, check_id)` (line 518)
- `def list_snapshot_checks(server_id)` (line 530)
- `def list_runbooks()` (line 536)
- `def list_runbook_summary()` (line 548)
- `def get_runbook(runbook_id)` (line 557)
- `def get_dossier(server_id)` (line 565)
- `def scan_script_sources(directory)` (line 592)
  - *Сканирует каталог под admin.runbook_sources, возвращает список .sh/.bash файлов.*
- `def read_script_from_source(path)` (line 598)
- `def create_runbook_from_scripts()` (line 603)
  - *Собирает runbook из спецификации.*
- `def check_server_prereqs(server_id)` (line 654)
  - *Вычисляет PrereqsReport по самым свежим наблюдениям сервера.*
- `def generate_bootstrap_runbook(server_id)` (line 702)
  - *Собирает bootstrap-runbook для установки недостающих admin-prereqs на сервере.*
- `async def validate_runbook(rb_id)` (line 783)
- `async def promote_runbook(rb_id)` (line 786)
- `async def run_runbook_step()` (line 802)
- `def global_summary()` (line 824)

## Symbols
- `def parse_server_specs(admin_cfg)` (line 122)
