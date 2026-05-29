# API Spec: `desktop/services/application_facade.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class AppNotification` (line 112)

### `class PreparedAttachments` (line 118)

### `class DesktopModeLaunchPolicy` (line 124)

### `class DesktopRuntimePayload(BaseModel)` (line 130)

### `class ApplicationFacade` (line 142)
*Координатор инициализации и шина уведомлений/прогресса.*
- `def __init__()` (line 145)
- `async def describe_active_cli_limits()` (line 245)
- `def subscribe(callback)` (line 331)
- `def list_modes()` (line 340)
  - *Возвращает список доступных режимов.*
- `def set_session_mode(session_uid, mode_id)` (line 353)
  - *Устанавливает активный mode для сессии.*
- `async def set_session_mode_via_callback(session_uid, mode_id)` (line 369)
  - *Переключает режим через тот же callback-маршрут, что и в Telegram:*
- `def rename_session(session_uid, new_name)` (line 404)
- `def reset_session(session_uid)` (line 416)
- `async def update_session_setting(session_uid, key, value)` (line 433)
  - *Update a specific session setting and persist changes.*
- `def get_remote_control_settings(session_uid)` (line 512)
  - *Return remote control settings for a session (Desktop parity with MiniApp GET).*
- `async def update_remote_control(session_uid)` (line 557)
  - *Validate, normalize, and optionally preflight a remote control settings change.*
- `async def recheck_remote_control(session_uid)` (line 692)
  - *Re-run preflight for the current remote control host.*
- `def force_save_file(session_uid, user_id, path, content)` (line 734)
  - *Force-save a file (skip revision check). Logs audit event.*
- `async def test_ssh_connection(workdir, alias)` (line 764)
  - *Verify SSH connectivity to a host.*
- `async def generate_ssh_key(workdir, alias)` (line 768)
  - *Generate a new SSH key pair for a host.*
- `def set_active_cli(session_uid, cli_name)` (line 772)
- `def confirm_session_transfer(session_uid, source_cli)` (line 802)
  - *Called by Desktop UI when user confirms session transfer.*
- `def get_metrics_snapshot()` (line 841)
- `def get_session_mode(session_uid)` (line 852)
- `def get_admin_status_payload(session_uid)` (line 861)
- `async def run_admin_session_action(session_uid)` (line 891)
- `def get_admin_config_yaml(session_uid)` (line 942)
- `def save_admin_config_yaml(session_uid)` (line 977)
- `def get_admin_hosts(session_uid)` (line 1015)
- `def get_admin_monitor_servers(session_uid)` (line 1041)
- `def save_admin_monitor_servers(session_uid)` (line 1096)
- `def get_admin_actions_ssh(session_uid)` (line 1164)
- `def save_admin_actions_ssh(session_uid)` (line 1206)
- `def get_admin_chat_messages(session_uid)` (line 1307)
- `def get_admin_chat_pending(session_uid)` (line 1323)
- `def get_admin_chat_memory_md(session_uid)` (line 1339)
- `def save_admin_chat_memory_md(session_uid)` (line 1355)
- `def reject_admin_chat_pending(session_uid)` (line 1373)
- `async def post_admin_chat_message(session_uid)` (line 1384)
- `async def approve_admin_chat_pending(session_uid)` (line 1405)
- `def list_admin_runs(session_uid)` (line 1424)
- `def get_admin_run_detail(session_uid)` (line 1465)
- `def list_scheduler_projects()` (line 1514)
- `def resolve_scheduler_project_slug(session_uid)` (line 1527)
- `def list_scheduler_notification_targets()` (line 1531)
- `def list_scheduler_jobs()` (line 1544)
- `def get_scheduler_job()` (line 1555)
- `async def publish_mode_launch_request()` (line 1568)
- `def create_scheduler_job()` (line 1697)
- `def update_scheduler_job()` (line 1723)
- `def delete_scheduler_job()` (line 1766)
- `def pause_scheduler_job()` (line 1779)
- `def resume_scheduler_job()` (line 1793)
- `async def run_scheduler_job_now()` (line 1807)
- `def set_theme(theme_name)` (line 1831)
  - *Меняет тему приложения и уведомляет подписчиков.*
- `def notify(event)` (line 1840)
- `def list_active_tasks()` (line 1853)
  - *Desktop-friendly snapshot of active background tasks.*
- `def list_runs(session_uid)` (line 1861)
- `async def doctor_run(session_uid)` (line 1878)
- `async def recover_run(session_uid)` (line 1892)
- `async def resume_run(session_uid)` (line 1906)
- `async def apply_recommendation_run(session_uid)` (line 1920)
- `async def promote_run_skills(session_uid)` (line 1934)
- `def list_pending_skill_installs(session_uid)` (line 2002)
- `async def approve_pending_skill_install(session_uid)` (line 2033)
- `async def reject_pending_skill_install(session_uid)` (line 2045)
- `def set_task_priority(task_id, priority)` (line 2519)
- `def update_task_progress(task_id)` (line 2525)
- `async def cancel_task(task_id)` (line 2531)
- `def get_manager_plan(session_uid)` (line 2544)
  - *Загрузить план проекта для сессии.*
- `async def export_data(session_uid)` (line 2556)
  - *Инициирует экспорт данных через CLI-обработчик.*
- `def resolve_analyst_question(question_id, answer)` (line 2585)
  - *Резолвит вопрос от аналитика/агента, отвечая на него.*
- `async def start()` (line 2709)
- `async def handle_mode_callback(session_uid)` (line 3830)
  - *Desktop entrypoint for mode callbacks (Telegram-style callback_data).*
- `async def handle_dirs_flow_event(session_uid)` (line 4180)
- `async def handle_dialog_message(session_uid)` (line 4197)
  - *If a mode dialog is active for (chat_id, session_uid, active_mode),*
- `async def show_mode_menu()` (line 4407)
  - *Ask the active mode plugin to build its menu and deliver it to Desktop UI via ui:mode_menu.*
- `def register_mode_runtime(mode_id, runtime)` (line 4436)
- `def iter_mode_runtimes()` (line 4481)
- `def get_runtime_by_capability(capability)` (line 4484)
- `async def prepare_attachments(session_uid, attachments)` (line 4501)
  - *Validate and copy attachments into session temp_dir (defaults.image_temp_dir under session.workdir).*
- `async def try_queue_busy_input(session_uid, text)` (line 4699)
  - *Ставит busy-ввод в очередь или показывает queue-choice, если сессия занята.*
- `async def stage_session_input(session_uid, text)` (line 4736)
  - *Ставит desktop-ввод в общий confirm/queue flow вместо немедленного запуска.*
- `async def run_session_input()` (line 4763)
  - *Основной метод выполнения ввода пользователя.*
- `async def run_background()` (line 4980)
- `def get_plugin_ui(allowed_tools)` (line 5008)
  - *Получает информацию о плагинах для UI.*
- `async def reload()` (line 5020)
  - *Перезагружает конфигурацию и переинициализирует компоненты.*
- `async def shutdown()` (line 5028)
- `def admin_autonomy_list_servers(session_uid)` (line 5290)
- `def admin_autonomy_server_summary(session_uid, server_id)` (line 5302)
- `def admin_autonomy_global_summary(session_uid)` (line 5318)
- `def admin_autonomy_get_baseline(session_uid, server_id)` (line 5330)
- `def admin_autonomy_accept_baseline(session_uid, server_id)` (line 5345)
- `def admin_autonomy_discard_baseline(session_uid, server_id)` (line 5361)
- `def admin_autonomy_list_drifts(session_uid, server_id)` (line 5376)
- `def admin_autonomy_ack_drift(session_uid, server_id, drift_id)` (line 5396)
- `def admin_autonomy_get_memory(session_uid, server_id)` (line 5411)
- `def admin_autonomy_update_fact(session_uid, server_id)` (line 5426)
- `def admin_autonomy_delete_fact(session_uid, server_id, key)` (line 5447)
- `def admin_autonomy_append_note(session_uid, server_id, text)` (line 5462)
- `def admin_autonomy_compact_memory(session_uid, server_id)` (line 5483)
- `def admin_autonomy_list_runbooks(session_uid, server_id, tags)` (line 5499)
- `def admin_autonomy_get_runbook(session_uid, runbook_id)` (line 5519)
- `def admin_autonomy_rescan_server(session_uid, server_id)` (line 5539)
- `def admin_autonomy_rescan_all(session_uid)` (line 5569)
- `def admin_autonomy_run_daily_maintenance(session_uid)` (line 5596)
- `def admin_autonomy_validate_runbook(session_uid, runbook_id)` (line 5612)
- `def admin_autonomy_promote_runbook(session_uid, runbook_id)` (line 5639)
- `def admin_autonomy_run_step(session_uid, runbook_id)` (line 5678)
- `def admin_autonomy_scan_scripts(session_uid, directory)` (line 5721)
- `def admin_autonomy_build_runbook(session_uid)` (line 5745)
- `def admin_autonomy_list_snapshot_checks(session_uid, server_id)` (line 5787)
- `def admin_autonomy_get_snapshots(session_uid, server_id, check_id)` (line 5802)
- `def admin_autonomy_check_prereqs(session_uid, server_id)` (line 5825)
- `def admin_autonomy_build_prereqs_bootstrap(session_uid, server_id)` (line 5843)
