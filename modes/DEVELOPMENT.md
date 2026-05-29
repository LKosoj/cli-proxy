# Разработка режимов (`modes/`)

Этот документ описывает, как добавлять и поддерживать режимы (Mode) в проекте.

## 1. Архитектура

- Базовый контракт режима: `modes/sdk/base.py` (`BaseMode`).
- Регистрация/загрузка режимов: `modes/registry.py`.
- Текущие режимы: `modes/agent/`, `modes/analyst/`, `modes/manager/`, `modes/webmaster/`.
- Общие модели/сервисы SDK: `modes/sdk/models.py`, `modes/sdk/services/*`.

Каждый режим — это модуль с классом, наследующим `BaseMode`, и обычно отдельным UI-слоем (`ui.py`), если есть inline-меню.

Режимы должны быть максимально независимыми от `BotApp`: общая инфраструктура предоставляется через SDK-сервисы, инжектируемые в `mode.initialize(...)`.

## 2. Минимальный контракт режима

Режим должен реализовать:

1. `mode_id`, `display_name`, `description`.
2. `handle_input(...)` — обработка входящего сообщения.
3. `handle_callback(...)` — обработка callback-кнопок.

Опционально:

1. `on_enable(...)` и `on_disable(...)` для lifecycle.
2. `build_menu(...)` для централизованной отрисовки меню.
3. `allows_agent_plugin_ui(...)` и другие feature-методы из `BaseMode`.

Рекомендуется также реализовать `build_menu(...)` в каждом режиме. Общий ререндер меню строится поверх этого метода.

## 3. Работа с состоянием сессии

Часто используемые поля:

- `session.modes.active_mode` — активный режим.
- `session.queue` — очередь входящих задач.
- `session.busy` — признак активной обработки.
- `session.orchestrator.enabled` — флаг session-scoped продвинутой оркестрации (по умолчанию `False`).
- `session.orchestrator.pending_input` — pending-ввод до подтверждения перехода (`✅/⛔`).
- `session.orchestrator.last_mode_output` — полный последний результат mode-пайплайна (для handoff).
- `session.orchestrator.last_mode_id` — `mode_id`, из которого получен последний результат.

Если режим ставит задачи в фон:

1. Используйте `BaseMode._start_mode_task(...)` как источник истины для running-состояния режима.
2. При выключении режима отменяйте его фоновые задачи через `mode_tasks.cancel_all(...)`.
3. Для долгих callback-действий (например, `run/refresh`) не блокируйте callback `await`-ом пайплайна:
   - запускайте задачу в фоне;
   - сразу закрывайте inline-меню (`reply_markup=None`) и показывайте «запущено/занята»;
   - на отмене прерывайте дочерние CLI-процессы режима;
   - если это одноразовый режим, выключайте его автоматически после завершения.

## 4. Межрежимная оркестрация (Hybrid)

В проекте используется session-scoped продвинутый оркестратор переходов между режимами:

1. LLM предлагает кандидата: `mode_id + reason + confidence`.
2. Policy/guardrails валидируют решение.
3. При низкой уверенности/невалидном выборе срабатывает deterministic fallback.

Ключевые ограничения:

- Переход в `agent` из оркестратора запрещён.
- При `Cancel` (`⛔`) текущий режим сохраняется.
- Оркестратор включается/выключается на уровне сессии и состояние сохраняется в `state`.

Handoff при подтверждённом переходе (`✅`):

- Если есть `session.orchestrator.last_mode_output`, в новый режим передаётся полный результат предыдущего режима без изменений.
- Если результата нет, передаётся исходный запрос пользователя без изменений.

Требования к mode-реализациям для корректного handoff:

1. Возвращайте содержательный `str` из `run_pipeline(...)` (или `ToolResult.output` в Desktop-path).
2. Не «прячьте» финальный результат только во внутренних структурах без возврата текста.
3. Для длинных ответов допускается дополнительная отправка через `send_output`, но возвращаемый результат должен оставаться пригодным для handoff.

## 5. Централизованные helper’ы BaseMode

Перед добавлением новой логики в `mode.py` проверьте, можно ли использовать уже существующие helper’ы:

- `_activate_mode(...)` / `_deactivate_mode(...)` — единый lifecycle enable/disable.
- `_persist_sessions(...)` — единый persist с логированием ошибок.
- `_normalize_dest(...)` — нормализация `dest` для Telegram/прочих transport-целей.
- `_enqueue_if_busy(...)` — общий паттерн busy -> queue -> notify.
- `_check_enable_requirements(...)` — типовые prechecks для включения режима (например, OpenAI/workdir).
- `_dispatch_callback_action(...)` — компактный dispatch callback-действий по карте handler’ов.
- `_rerender_menu_common(...)` — общий ререндер меню через `build_menu(...)`.
- `_messaging(...)` — единое создание `MessagingService`.

## 6. Сервисы и декуплинг от BotApp

При инициализации mode получает `services` (через `plugin.initialize(...)` в `BotApp`):

- `tasks` (`TaskService`) — фоновые задачи режима.
- `dialogs` (`DialogService`) — диалоги режима.
- `session_control` (`SessionControlService`) — persist/cancel операций по сессии.
- `messaging_factory` — фабрика `MessagingService` для текущего transport context.
- `pipeline` (`ModePipelineService`) — единый запуск `run_mode_pipeline` (mode-driven execution).
- `SharedOrchestratorRunner` (`modes/sdk/orchestration.py`) — общий SDK-уровень рантайма оркестратора для mode-owned runner services.
- `agent_runtime` (`AgentRuntimeService`) — runtime-операции agent-режима (interrupt/cleanup/cache/session lookup).
- `dirs_flow` (`DirsFlowService`) — централизованный запуск directory-picker flow.
- `manager_pending` (`DictStateService`) — централизованное хранение pending-состояния manager-mode.
- `tooling` (`ModeToolingService`) — единый доступ mode-кода к ToolRegistry (`execute`, `ask_user`, нормализация выбора).

Правило:

1. Для типовых операций используйте `BaseMode` helper’ы/SDK-сервисы.
2. Не добавляйте прямые вызовы `bot_app.manager._persist_sessions()`, `bot_app.mode_tasks.*`, `bot_app.mode_dialogs.*` в mode-код.
3. Не вызывайте из mode напрямую `bot_app.session_management.*` и не работайте напрямую с `bot_app.*`-dict state (например, `manager_resume_pending`) — используйте SDK-сервисы.
4. Для вызовов инструментов (`ask_user`, `use_cli`, `intent_plugin`) используйте `tooling`, не `bot_app._tool_registry`.
5. Прямой доступ к `bot_app` допустим только для mode-специфичных операций, которых нет в SDK (и это стоит явно документировать).

### 6.1. Архитектурные инварианты P1 (dispatcher / TaskService / intents)

Этот набор правил обязателен для `agent`, `analyst`, `manager`, `webmaster` и новых режимов.

#### A. Callback dispatcher (вместо if-elif монолита)

1. `handle_callback(...)` должен извлекать `action` и делегировать обработку через карту handler'ов.
2. Карта строится отдельным методом (например, `_build_callback_handlers(...)`), где `action -> _cb_*`.
3. Диспетчеризация выполняется через `BaseMode._dispatch_callback_action(...)`.
4. Каждый callback-action должен жить в отдельном методе (`_cb_enable`, `_cb_disable`, `_cb_reset`, ...).
5. Запрещено добавлять новый монолитный `if/elif`-блок на десятки действий в `handle_callback(...)`.

#### B. Фоновые задачи только через TaskService

1. В mode-коде запрещены прямые `asyncio.create_task(...)` для фоновых процессов режима.
2. Используйте `BaseMode._start_mode_task(...)` или `TaskService.create(...)`.
3. Отмена и cleanup выполняются через `BaseMode._cancel_mode_tasks(...)` / `SessionControlService.cancel_mode(...)`.
4. Любая фоновая задача режима должна быть видна в реестре (`tasks.list(...)`) и отменяема через session control.
5. При `disable/reset` режима необходимо отменять его фоновые задачи, не оставляя untracked task-ов.

#### C. Контракт intent: schema-first + graceful fallback

1. Output от `intent_plugin`/LLM сначала проходит `parse_normalize_validate(..., <ModeIntentOutputSchema>)`.
2. Схема intent-output хранится в `<mode>/schemas.py` и считается частью публичного контракта режима.
3. После валидации применяется нормализация payload (строки/списки/дефолты) в отдельном helper-методе.
4. При пустом/невалидном JSON режим обязан идти в graceful fallback (например, `_fallback_intent_payload(...)`), а не падать исключением верхнего уровня.
5. Любое изменение intent-схемы требует синхронного обновления нормализации и тестов на валидный и невалидный output.

## 7. Статусы и отрисовка (централизовано)

Для единообразного текста статуса используйте `session_status.py`:

- `build_mode_status_text(...)` — общий каркас статус-сообщения.
- `get_session_queue_len(session)` — длина очереди.
- `build_common_mode_stage(...)` — типовая логика стадии:
  - `выключен`
  - `обрабатывает задачу`
  - `ждет задачи в очереди`
  - `ожидает новый запрос`
- `build_manager_mode_stage(...)` — доменный stage helper для Manager.
- `build_webmaster_mode_stage(...)` — доменный stage helper для Webmaster.

Рекомендуемый подход:

1. В режиме вычислить только режим-специфичные признаки (`running`, доменные флаги).
2. Для базовой стадии использовать `build_common_mode_stage(...)`.
3. Если режиму нужны специальные стадии, применять доменный helper (`build_manager_mode_stage`/`build_webmaster_mode_stage`) и делегировать остальное в общий слой.

## 8. Правила внесения изменений

1. Не дублируйте общие утилиты статуса в mode-файлах.
2. Любую повторяющуюся логику выносите в общий слой (`session_status.py` или `modes/sdk/*` по назначению).
3. При добавлении новой опции конфигурации обновляйте оба файла:
   - `config.yaml`
   - `config_example.yaml`
4. Если добавляете новые ветки `except`, логируйте ошибки (`logger.exception(...)`).
5. Перед реализацией нового режима изучите существующие (`agent`, `analyst`, `manager`, `webmaster`) и переиспользуйте их как референс-паттерны.

## 9. Тестирование

Минимальный набор перед merge:

1. Точечные тесты изменённых компонентов.
2. Полный прогон: `pytest -q`.
3. Линтер: `flake8 .` (или `.venv/bin/python -m flake8 .`).

Для общих helper-функций пишите отдельные unit-тесты в `tests/` (без необходимости поднимать весь runtime-цикл режима).
