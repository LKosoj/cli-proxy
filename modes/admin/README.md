# Admin Mode

Режим администрирования текущей сессии. Обеспечивает мониторинг серверов,
аналитическое принятие решений, безопасное выполнение remediation-действий
и уведомление оператора.

## Архитектура

Admin работает как session-scoped плагин `modes/admin` и состоит из следующих
компонентов (см. соответствующие модули):

| Модуль                                           | Назначение                                                                                         |
|--------------------------------------------------|----------------------------------------------------------------------------------------------------|
| `mode.py` (`AdminMode`)                          | Entry point плагина: `on_enable`/`on_disable`, `handle_input`, `handle_callback`, меню и статус.    |
| `runner_service.py` (`AdminModeRunnerService`)   | Оркестрация pipeline `Monitor → Analyzer → Executor → Notifier`.                                    |
| `monitor.py` (`AdminMonitor`)                    | Снимки состояния серверов (нагрузка, диски, сервисы, кастомные чеки).                              |
| `analyzer.py` (`AdminAnalyzer`)                  | LLM-аналитика: инциденты, decision, подтверждения, severity.                                        |
| `executor.py` (`AdminExecutor`)                  | Выполнение remediation-действий через local/ssh transport. Проверяет allowlist и policies.         |
| `notifier.py` (`AdminNotifier`)                  | Отправка инцидентов/результатов в Telegram, учитывает mute/dry-run.                                 |
| `scanner.py` (`AdminEnvironmentScanner`)         | Initial environment scan + генерация `generated` блока в config.                                   |
| `state_store.py` (`AdminStateStore`)             | SQLite-хранилище session/chat состояния, incidents, actions, overrides, alert_state, acks, mute.   |
| `config_store.py` (`AdminConfigStore`)           | Чтение/запись per-session YAML-конфига в `<workdir>/.cli-proxy/.admin/config.yaml`.                 |
| `allowlist.py`                                   | Проверка ID команд и доступности в allowlist.                                                       |
| `transports/`                                    | Local/SSH subprocess транспорты для выполнения действий.                                            |
| `schemas.py`                                     | JSON-схемы payload'ов pipeline и конфигурации.                                                     |
| `ui.py`                                          | Сборка текстов для Telegram (MarkdownV2): меню, статус, экраны инцидентов/approvals/skills/runs.   |

## Lifecycle

- `on_enable(ctx)` — идемпотентная активация: ensure_schema, ensure_config,
  активация runtime, запуск initial environment scan (если требуется),
  запуск runner loop, `set_session_admin_enabled(True)`.
- `on_disable(ctx)` — деактивация: `set_session_admin_enabled(False)`,
  отмена всех фоновых admin-задач (`_cancel_admin_tasks`), `set_runtime_status("disabled")`.

Хуки вызываются из `app/services/mode_launch_adapter.py::_ensure_mode_enabled`
при переходах режимов. Они же вызываются из пользовательских команд
`/admin enable` и `/admin disable`, а также из callback-кнопок Enable/Disable.

## Pipeline

`AdminModeRunnerService.run_pipeline_once()` выполняет один цикл:

1. **Monitor** — опрос всех серверов из `admin.monitor.servers`,
   сбор `AdminMonitorSnapshot`.
2. **Analyzer** — LLM-decision на основе snapshot + current alert_state.
   Возвращает `decision` (action_id, confidence, triggers, reasoning).
3. **Executor** — если decision не требует ручного подтверждения,
   проверяет allowlist/policies и выполняет action через соответствующий transport.
4. **Notifier** — отправляет сводку (incident + action result) в Telegram
   с уважением mute и dry-run флагов.

Pipeline запускается в отдельной фоновой задаче (`_RUNNER_TASK_NAME =
"run_admin_pipeline_loop"`), интервал берётся из
`admin.monitor.interval_sec` (дефолт 30 сек).

## Конфигурация

### Файл `<workdir>/.cli-proxy/.admin/config.yaml`

Создаётся из шаблона `modes/admin/templates/config.yaml` при первом `on_enable`.

Верхний ключ — `admin:`. Основные подразделы:

```yaml
admin:
  allowlist:          # Разрешённые action_id для каждого транспорта
    local:
      clear_logs:
        argv: ["bash", "-lc", "find /var/log -type f -name '*.log' -mtime +7 -delete"]
        timeout_sec: 60
        risk_level: medium
    ssh: {}

  monitor:            # Настройки мониторинга
    enabled: true
    interval_sec: 30
    servers: []       # [{id: srv-1, target: local}, ...]

  notifications:
    level: info       # info | warning | error

  runtime:            # Заполняется автоматически после initial scan
    pinned_cli: {}
    pinned_executor_profile: null
    initialized_at: null
    last_scan_at: null
    scan_status: not_started
    scan_error: null

  environment:        # Описание окружения (ручные записи)
    services: {}
    server_roles: []
    stack_facts: {}

  diagnostics:
    checks: []        # Список checks для мониторинга

  incidents:
    rules: []         # Правила генерации incidents

  actions:            # Исполняемые действия
    local: {}
    ssh: {}
    remediation:
      clear_logs:
        action_id: clear_logs
        target: local
        risk_level: medium
        description: "Очистить старые логи"

  policies:           # Политики автоматизации
    version: v2
    analyzer:
      allow_secondary_cli: true
      allow_internet_secondary_cli: true
      require_secondary_confirmation_on_low_confidence: true
      require_secondary_confirmation_on_risky_action: true
      require_secondary_confirmation_on_signal_conflict: true
      require_secondary_confirmation_on_policy_conflict: true
      require_secondary_confirmation_before_remediation: true
    executor:
      mandatory_notify_actions: []
      auto_actions_per_hour: 6
      cooldown_sec: 120
      maintenance_window:
        enabled: false
        start_ts: 0
        end_ts: 0
    per_action: {}

  generated:          # Результаты initial scan, перезаписываются scanner'ом
    environment: {}
    diagnostics: { checks: [] }
    incidents:   { rules: [] }
    actions:
      remediation: {}
      targets: { local: {}, ssh: {} }
    policies: {}
    monitor: { servers: [] }
    manual: {}
    scan_meta: {}
    scan_summary: {}
```

### Секреты

`<workdir>/.cli-proxy/.admin/secrets.env` создаётся автоматически пустым.
Переменные из него подгружаются в окружение перед выполнением local/ssh команд.

## Policies

Policies определяют безопасность автоматизации:

- **analyzer.require_secondary_confirmation_on_***:
  если `decision.triggers` содержит соответствующий флаг,
  то Executor пропустит автоматическое выполнение и создаст `pending_approval`.
  Оператор подтверждает или отклоняет через inline-кнопки или `/admin approvals`.

- **executor.auto_actions_per_hour**: лимит автоматических действий за час.
  При превышении действие переводится в `pending_approval`.

- **executor.cooldown_sec**: минимальный интервал между двумя
  автоматическими действиями для одного server_id/action_id.

- **executor.maintenance_window**: если включено, действия выполняются
  только в пределах `[start_ts, end_ts]`.

- **executor.mandatory_notify_actions**: список action_id, после выполнения
  которых всегда отправляется notification.

## Autonomy

Блок `admin.autonomy` управляет, что режим может выполнять автоматически —
и monitor-loop (исправление drift'ов), и chat-autopilot (intents из LLM).

```yaml
admin:
  autonomy:
    enabled: true                       # общий kill-switch
    auto_apply_severities: [info]       # monitor-loop: какие severity можно авто-фиксить
    auto_exec_actions:                  # action_id, разрешённые к автоматическому запуску
      - systemd.restart_nginx
      - disk.cleanup_tmp
    auto_exec_adhoc_commands:           # chat-autopilot: argv[0] разрешённых ad-hoc команд
      - ls
      - df
      - uptime
      - systemctl
    max_actions_per_hour: 5             # monitor-loop rate-limit; к chat-autopilot не применяется
    cooldown_sec: 300                   # monitor-loop per-check cooldown
    per_server:
      web-01:
        auto_exec_actions: [systemd.restart_nginx]
        auto_exec_adhoc_commands: [journalctl]
```

### Chat autopilot

Если `autonomy.enabled=true` и intent из admin-chat (`propose_action`,
`propose_plan`, `propose_new_action`) проходит allowlist — он выполняется
автоматически без карточки approval. Правила:

- `propose_action` → действие разрешено, если `action_id ∈ auto_exec_actions`.
- `propose_new_action` (ad-hoc argv) → разрешено, если `argv[0] ∈
  auto_exec_adhoc_commands` (точное совпадение, без wildcards).
- `propose_plan` → all-or-nothing: каждый шаг должен пройти allowlist.
  Если хоть один шаг вне allowlist — весь план уходит в approval.
  Для auto-executed планов `stop_on_error=True` форсируется независимо
  от intent'а.
- Severity / `risk_level` из chat-intent'ов **не учитываются** (allowlist-ов
  достаточно). Rate-limit и cooldown к chat-autopilot не применяются.
- Blocked intent сохраняется в pending approval и помечается
  `autopilot_blocked: <reason>` — оператор видит его в Desktop/MiniApp/Telegram.
- События записываются в chat memory как `intent_autopilot_executed` /
  `intent_autopilot_blocked`.

- **per_action**: переопределение risk_level/timeout_sec для конкретного
  action_id.

## Команды пользователя

Доступны через `/admin <подкоманда>` в Telegram:

| Команда                                   | Действие                                                                 |
|-------------------------------------------|--------------------------------------------------------------------------|
| `/admin`                                  | Показать меню admin с inline-кнопками.                                    |
| `/admin enable`                           | Включить admin для текущей сессии.                                        |
| `/admin disable`                          | Выключить admin для текущей сессии.                                       |
| `/admin status`                           | Показать сводный статус pipeline, mute, pending.                          |
| `/admin rescan`                           | Перезапустить environment scan (сессия должна быть свободна).             |
| `/admin incidents [N]`                    | Последние инциденты.                                                      |
| `/admin actions [N]`                      | Последние выполненные admin-действия.                                     |
| `/admin approvals list`                   | Pending approvals для ручного подтверждения.                              |
| `/admin approvals revoke <override_id>`   | Отозвать сохранённый approval override.                                    |
| `/admin approvals clear`                  | Очистить approvals текущей сессии.                                        |
| `/admin skills list`                      | Pending skill-installs (agent-mode).                                      |
| `/admin skills approve <approval_id>`     | Подтвердить pending skill install.                                        |
| `/admin skills reject <approval_id>`      | Отклонить pending skill install.                                          |
| `/admin ack <incident_id>`                | Подтвердить incident.                                                     |
| `/admin mute <minutes>`                   | Временно выключить alerts (default 60 мин).                               |
| `/admin unmute`                           | Включить alerts обратно.                                                  |
| `/admin dry-run on|off`                   | Переключить dry-run (команды симулируются без выполнения).                |
| `/admin run <action_id> <server_id>`      | Выполнить whitelisted action для сервера.                                 |
| `/admin check <server_id>`                | Запустить check-action для сервера.                                       |

## Inline-меню

Меню (`build_menu`) собирается в `modes/admin/mode.py` и содержит кнопки:

- Enable / Disable — по состоянию сессии.
- Status / Rescan.
- Incidents / Actions — открывают inline-экраны с деталями.
- Approvals / Skills — открывают экраны с кнопками revoke/approve/reject.
- Runs / Mute — экран pipeline-runs и mute-меню.
- Dry-run — переключение dry-run режима.

## Внешние интерфейсы

### Telegram

Callback-данные формируются через `build_mode_action_callback_data(mode_id,
action, session, payload)`. Поддерживаемые actions:
`menu`, `enable`, `disable`, `status`, `rescan`, `incidents`, `actions_list`,
`approvals_list`, `skills_list`, `runs_list`, `ack`, `revoke`,
`approvals_clear`, `skill_approve`, `skill_reject`, `mute`, `unmute`,
`dryrun_toggle`.

Для длинных entity ID (incident_id, override_id, approval_id)
используется токенизация SHA1-16 с обратным маппингом в
`session._admin_*_tokens`, чтобы уложиться в 64-байтный лимит callback_data.

### MiniApp

- `GET /api/v1/admin/status?session_uid=...`
- `POST /api/v1/admin/action` — `{action, session_uid, ...}` поддерживает
  `enable`, `disable`, `rescan`, `approve_skill_install`, `reject_skill_install`,
  `ack_incident`, `revoke_approval`, `approvals_clear`, `mute`, `unmute`,
  `set_dry_run`, `dryrun_toggle`.
- `GET /api/v1/admin/runs?session_uid=...` — список pipeline runs.
- `GET /api/v1/admin/runs/{run_id}?session_uid=...&events_limit=50` —
  state + plan + checkpoints + tail событий.

### Desktop (PySide6)

`desktop/widgets/admin_panel.py` показывает status payload и позволяет
включать/выключать/перезапускать scan, approve/reject pending skill installs,
просматривать список pipeline runs и детали конкретного run (artifact store).

## State Store

`AdminStateStore` (SQLite):

- `get_session_state(session_id, chat_id)` — per-session флаги
  (`enabled`, `dry_run`, обновившие `updated_by`/`updated_at`).
- `list_incidents`, `get_incident`, `create_incident` — incidents.
- `list_actions`, `create_action` — admin-actions history.
- `list_approved_overrides`, `create_approved_override`,
  `revoke_approved_override`, `clear_approved_overrides` — approvals.
- `get_alert_state`, `create_alert_state`, `update_alert_state` —
  state per alert_id (для ack'ов).
- `create_acknowledgement` — подтверждение incident'а.
- `mute_session`, `unmute_session`, `get_mute_state` — mute механика.

Путь к БД: `AppConfig.defaults.state_path` (через SQLiteStore wrapper).

## Pipeline runs (artifacts)

`RunArtifactStore` сохраняет артефакты pipeline-итераций в
`runs/<session_uid>/admin/<run_id>/`. Каждый run хранит:

- `STATE.json` — статус, phase, started_at/finished_at.
- `PLAN.json` — operation plan от Analyzer.
- `CHECKPOINTS.json` — промежуточные отметки.
- `RECOVERY.json`, `METRICS.json` — служебные.
- `events.jsonl` — поток событий (monitor_snapshot, analyzer_decision,
  executor_result, notifier_result, errors).

Просмотр доступен через Telegram `/admin` → кнопка Runs, MiniApp
`/api/v1/admin/runs/...` и Desktop admin_panel.

## Тестирование

Модульные тесты:

- `tests/test_admin_mode_plugin.py` — интеграционные сценарии.
- `tests/test_admin_mode_lifecycle.py` — on_enable/on_disable.
- `tests/test_admin_runner_service.py` — pipeline orchestration.
- `tests/test_admin_analyzer.py`, `tests/test_admin_executor.py`,
  `tests/test_admin_monitor.py`, `tests/test_admin_notifier.py`.
- `tests/test_admin_state_store.py`, `tests/test_admin_config_store.py`.
- `tests/test_admin_allowlist.py`, `tests/test_admin_scanner.py`.
- `tests/test_admin_local_transport.py`, `tests/test_admin_ssh_transport.py`.
- `tests/test_admin_ui.py`, `tests/test_admin_mode_architecture.py`.
- `tests/test_desktop_admin_panel.py`.
