# CLI Proxy Telegram Bridge

[English version](README_EN.MD) | [Русская версия](README.md)

Telegram-бот для управления CLI-агентами (Codex / Gemini / Qwen / Claude / Grok / Kimi / opencode) с поддержкой нескольких сессий, очереди запросов и HTML-вывода.

## Возможности
- Несколько сессий CLI в разных каталогах.
- Очередь запросов и контроль занятости.
- Короткий вывод в пределах Telegram Rich Message лимита (до 32768 UTF-8 символов) отправляется Telegram-сообщением; длинный вывод или `force_html=True` отправляется HTML-файлом (Markdown-разметка, ANSI-цвета, mermaid-диаграммы в SVG; при ошибке рендера остаются как код-блоки).
- Summary/preview для длинного HTML-вывода отправляется отдельным сообщением, если включена отправка саммари.
- Автодетект prompt и resume-токена, сохранение в `state.json`.
- Восстановление сессий и thread/scope-привязок после перезапуска.
- Inline-меню для выбора сессий, каталогов и команд.
- Кнопка "Использовать этот каталог" при выборе директории.
- Кнопка "git clone" для клонирования репозитория в выбранный каталог.
- Inline-меню Git-операций (status/fetch/pull ff-only/merge/rebase/diff/log/stash/commit/push) для сессии текущего контекста.
- Обработка конфликтов merge/rebase с кнопками diff/abort/continue/позвать агента.
- Админ-команда `/selfupdate`: выполняет `git pull --ff-only` в каталоге бота и при успехе запускает перезапуск сервиса.
- Управление файлами в рабочей директории через `/files` (отправка, сохранение, переименование, удаление).
- Отчёты текущей сессии доступны в Telegram через `/reports`; `/reports snapshot` и кнопка "Отчёт сессии" в меню `/sessions` создают человеко-читаемый HTML-отчёт на языке пользователя из run artifacts, истории отчётов и доступного transcript CLI. Финальный отчёт Manager автоматически сохраняется в общую историю отчётов.
- Шаблоны задач (скрытая команда `/preset`).
- **Режим Agent** (через `/sessions` -> `Агент`) - ИИ-агент (ReAct) с набором инструментов (чтение/запись файлов, поиск, выполнение команд, web-поиск и др.), планирует и выполняет задачи поэтапно.
- **Режим Manager** (через `/sessions` -> `Менеджер`) - мульти-агентная оркестрация: автоматическая декомпозиция задачи на подзадачи, разработка через CLI, ревью через Agent, арбитраж по критериям приёмки и финальный отчёт.
- **Режим SDD** (через `/sessions` -> `SDD`) - Spec-Driven Development поток `specify -> plan -> tasks -> analyze` с гейтами подтверждения, EARS-критериями, интеллектуальной инициализацией проекта, артефактами `specs/<NNN>-<slug>/`, ADR-логом `.cli-proxy/decisions.md` и handoff готового `tasks.md` в Manager.
- **Режим Webmaster** (через `/sessions` -> `Вебмастер`) - цикл `intent -> confirm -> dev_cli -> validation_cli -> feedback` для задач веб-разработки. Успешный gate принимается только когда все пункты чеклиста имеют статус `PASS`.
- **Режим Admin** (через `/sessions` -> `Admin`) - фоновый конвейер `Monitor -> Analyzer -> Executor -> Notifier` для мониторинга инцидентов и remediation-действий, с политиками безопасности, интерактивным `ask_user` и переиспользованием подтверждений по hash override.
- **Режим Mapper** (через `/sessions` -> `Mapper`) - строит карту кодовой базы в `.cli-proxy/.codebase_map/` через 4 параллельных фокуса (tech/arch/quality/concerns), поддерживает md-граф инструкций (`INDEX.md`, `nodes/*.md`), умеет инициализацию/переинициализацию/детальную проверку графа и обновляет карту инкрементально по `git diff`.
  Артефакты карты автоматически подмешиваются в контекст режимов Analyst и Manager (если карта уже существует).
- Межрежимный handoff оркестратора: при подтвержденном переходе в следующий режим передается полный `session.orchestrator.last_mode_output`, а не сокращенный summary.

## Быстрый старт
1. Установите зависимости:
```bash
pip install -r requirements.txt
```

2. Установите ripgrep (рекомендуется для режима Mapper):
```bash
# Ubuntu/Debian
sudo apt-get install ripgrep

# macOS
brew install ripgrep

# Windows (Chocolatey)
choco install ripgrep

# Или через cargo
cargo install ripgrep
```

ripgrep используется для быстрого сканирования файлов с учётом `.gitignore`.
Без ripgrep режим Mapper будет работать медленнее, но tetap поддержит `.gitignore` через библиотеку `pathspec`.

2. Заполните `config.yaml`:
- `telegram.token`
- `telegram.whitelist_chat_ids`
- `defaults.workdir`
- при необходимости `openai_*` для автоматического создания summary ответов от CLI инструментов

`telegram.whitelist_chat_ids` - критичный параметр доступа:
- бот отвечает только чатам, чьи `chat_id` перечислены в этом списке;
- если список пустой, доступ не получит никто;
- для групп `chat_id` обычно отрицательный (например, `-100...`).

3. Запустите бота:
```bash
python bot.py
```

## Thread Mode, Webhooks и Scheduler

### Thread Mode как базовый режим
- Для супергрупп с forum topics базовым рабочим режимом считается `thread_mode.mode=group`.
- В этом режиме каждая Telegram-сессия получает собственный topic, а входящий и исходящий роутинг выполняется по `message_thread_id`. Background reports и mode reports возвращаются в тот же topic через `NotificationQueueService`.
- Канонический runtime-идентификатор сессии всегда задаётся как `session_uid`, вычисленный из `ConversationScope`: direct chat использует `chat:<chat_id>`, forum topic использует `thread:<chat_id>:<message_thread_id>`, Desktop использует `desktop:<session_id>`.
- Минимальная конфигурация:
```yaml
thread_mode:
  enabled: true
  mode: group
  topics_chat_id: -1001234567890
```
- Шаги включения в BotFather:
1. Откройте `@BotFather` и выполните `/mybots`.
2. Выберите нужного бота.
3. Откройте `Bot Settings`.
4. Откройте `Group Topics` и включите topics для бота.
- На startup бот выполняет capability-check `bot.get_me().has_topics_enabled`. Если topics отключены, процесс завершается с CRITICAL-логом и явной инструкцией вернуться в BotFather.

### Webhooks
- `webhooks.enabled=true` включает shared HTTP ingress на общем `aiohttp` listener.
- Webhooks продолжают работать даже если `miniapp.enabled=false`, потому что ingress общий для MiniApp и Telegram webhook route из `webhooks.path`.
- Для Telegram webhook path обычно используется `webhooks.path: /webhooks/telegram`; запросы проверяются по `webhooks.secret_token` через заголовок `X-Telegram-Bot-Api-Secret-Token` или `X-Webhook-Secret-Token`.
- Рекомендуемая конфигурация:
```yaml
webhooks:
  enabled: true
  public_base_url: https://bot.example.com
  path: /webhooks/telegram
  secret_token: change-me
```
- Принятый webhook публикуется как `WebhookReceivedEvent`, после чего `ModeLaunchAdapter` применяет allowlist `origin -> mode`, сохраняет `correlation_id`/`dry_run` и отправляет итоговые репорты через queue-based delivery.

### Scheduler
- `scheduler.enabled=true` включает persistent scheduler c хранением job в SQLite.
- Каждая job хранит `cron`, `target_mode`, владельца и явный `notification_target.telegram_session_uid`, чтобы репорты возвращались в нужный Telegram scope.
- Scheduler не запускает mode напрямую: при срабатывании создаётся `ScheduledJobEvent`, который затем проходит тот же policy-check и mode launch adapter, что и webhook events.
- CRUD для scheduler jobs уже доступен в MiniApp и Desktop. Все мутации `create/update/delete` пишутся в audit trail.
- Базовая конфигурация:
```yaml
scheduler:
  enabled: true
  timezone: Europe/Moscow
  tick_interval_sec: 1.0
```

### Установка через инсталлятор (`setup_bot.sh`)
Для первичной настройки на Ubuntu/Debian можно использовать скрипт:

```bash
./setup_bot.sh
```

Скрипт:
- установит системные зависимости и Python venv;
- установит CLI-инструменты (`codex`, `claude`, `gemini`, `qwen`, `grok`, `kimi`, `opencode`);
- создаст `config.yaml` и `.env`;
- поднимет `systemd`-сервис.

Без интерактива:
```bash
SETUP_BOT_TOKEN="123456:ABCDEF" \
SETUP_WHITELIST_RAW="123456789,-1009876543210" \
SETUP_ADMINLIST_RAW="123456789" \
# Опционально: проекты для обычных пользователей (CHATID:/abs/p1,/abs/p2;CHATID2:/abs/p3)
# SETUP_USER_WORKDIRS_RAW="111111111:/opt/projects/a,/opt/projects/b" \
SETUP_WORKDIR="/opt/cli-proxy/workdir" \
OPENAI_API_KEY="sk-..." \
OPENAI_MODEL="gpt-4.1-mini" \
OPENAI_BIG_MODEL="gpt-4.1" \
./setup_bot.sh --non-interactive --service-name cli-proxy-bot --service-user "$(whoami)" --chown no
```

В `--non-interactive` обязательны:
- `SETUP_BOT_TOKEN`
- `SETUP_WHITELIST_RAW`
- `SETUP_ADMINLIST_RAW`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_BIG_MODEL`

### Настройка `claude-bot` для Claude Code
Если планируете использовать `Claude Code` в режиме `--dangerously-skip-permissions`, сначала настройте отдельного непривилегированного пользователя:

```bash
sudo ./scripts/setup-claude-bot.sh
```

Опционально можно переопределить рабочую директорию, имя пользователя и версию CLI:

```bash
sudo ./scripts/setup-claude-bot.sh --workdir /srv/git_projects --username claude-bot --version latest
```

Что делает скрипт:
- создаёт пользователя `claude-bot` (или указанного через `--username`), если его ещё нет;
- создаёт общую группу `cli-proxy-workgroup` и добавляет в неё `root` и `claude-bot`;
- настраивает права на `--workdir` (по умолчанию `/srv/git_projects`): группа `cli-proxy-workgroup` и `g+rwxs`, чтобы новые файлы наследовали общую группу;
- устанавливает `claude` через официальный install script от имени `claude-bot`;
- добавляет `$HOME/.local/bin` в `~/.bashrc` пользователя;
- дописывает в `~/.bashrc` переменные Anthropic (`ANTHROPIC_MODEL=coder-model`, а также `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`, если они заданы в скрипте);
- выполняет итоговую проверку и печатает, что именно прошло и что нужно исправить.

После запуска скрипта проверьте:
- `su - claude-bot -c "claude --version"`: CLI отвечает и показывает версию;
- `su - claude-bot -c "test -w /srv/git_projects && echo OK"`: есть запись в рабочую директорию (подставьте свой `--workdir`, если он другой);
- `su - claude-bot -c "test -x ~/.local/bin/claude && echo OK"`: бинарник установлен в пользовательский `~/.local/bin`;
- `groups claude-bot`: пользователь состоит в группе `cli-proxy-workgroup`;
- для текущей root-сессии выполнен перелогин или `newgrp cli-proxy-workgroup`, иначе новое членство в группе может не примениться сразу.

После этого основной бот может продолжать работать от root: команды `claude` будут запускаться автоматически через `su - claude-bot -c "..."`.

## Desktop-приложение
Проект также включает desktop-клиент на `PySide6 + qasync`, который использует те же доменные сервисы (`app/services`, `sessions`, `modes`) через фасад.

Запуск:
```bash
python desktop/main.py
```

Ключевые точки архитектуры desktop:
- `desktop/main.py` - desktop composition root, bootstrap сервисов и окна.
- `desktop/main_window.py` - каркас окна и маршрутизация UI-событий. Визуальное отображение V2-событий реакций (retry, reroute, needs_input) и статусов валидации (not_run).
- `desktop/services/application_facade.py` - orchestration desktop-операций и интеграция mode/runtime, подписка на события ReactionEngine.
- `desktop/widgets/*` - UI-компоненты без дублирования доменной логики.
- Вкладка `Reports` использует тот же общий history отчётов, что и Telegram `/reports`; Desktop не создаёт отдельную копию отчёта, а только показывает и экспортирует выбранный Markdown в PDF локально.

Для деталей по слоям и правилам размещения кода см. `ARCHITECTURE.md`.

## Рабочий процесс (рекомендуемый)
1. **Старт и выбор инструмента**
   - Откройте меню бота и выберите `/tools`, чтобы увидеть доступные CLI.
   - Откройте `/sessions` -> `+ Новая сессия`, выберите инструмент и рабочую директорию.

2. **Отправка задачи**
   - Пишите задачу обычным сообщением - она уйдёт в сессию текущего topic/контекста.
   - Если сообщение начинается с `/`, используйте `/send /команда` или `> /команда`.
   - Длинные сообщения склеиваются в буфер в течение 2 секунд; короткие (<3000) отправляются сразу.
- Вложения `.txt`, `.md`, `.rst`, `.log`, `.html`, `.htm` (до 500 КБ) отправляются сразу и объединяются с описанием.

3. **Контроль выполнения**
   - Если сессия занята, бот предложит: отменить, поставить в очередь или отменить ввод.
   - Статус сессии текущего контекста смотрите через `/status` или в меню `/sessions`.
   - Очередь можно проверить `/queue` и очистить `/clearqueue`.
   - Кнопка `Resume` в меню `/sessions` показывает 4 последние сессии активного CLI для рабочей директории (дата и начало первого запроса). Выбор подставляет её id в `resume_token`, и следующее сообщение продолжит выбранный диалог; ручной ввод id доступен там же. В Desktop тот же список открывается пунктом `Resume Token`.

4. **Работа с результатом**
   - Короткий результат в пределах Telegram Rich Message лимита (до 32768 UTF-8 символов) приходит Telegram-сообщением.
   - Длинный результат приходит HTML-файлом; `force_html=True` принудительно использует HTML-файл даже для короткого результата.
   - Summary или preview для длинного HTML-вывода приходит отдельным сообщением, если включена отправка саммари.
   - Отчёты сессии смотрите через `/reports`; `/reports latest` отправляет последний отчёт, `/reports snapshot` создаёт человеко-читаемый HTML-отчёт на языке пользователя, `/reports generate` создаёт отчёт из текущего Manager-плана, `/reports <filename.md>` отправляет конкретный файл.
   - При необходимости откройте `/files` для работы с файлами (скачать/сохранить/переименовать/удалить).

5. **Сессии и продолжение**
   - Используйте `/sessions` для переключения, переименования или закрытия сессий.
   - Resume-токен сохраняется и может быть восстановлен автоматически.

6. **Режим Agent**
   - Включите через `/sessions` -> `Agent Агент` - бот будет использовать ИИ-агента (ReAct) с инструментами (файлы, поиск, команды) для выполнения задач.
   - Agent и Manager взаимоисключающие - при включении одного второй выключается.

7. **Режим Manager**
   - Включите через `/sessions` -> `Manager Менеджер` - система автоматически разбивает задачу на подзадачи, делегирует разработку CLI, ревью - агенту, и принимает решения по критериям приёмки.
   - Прогресс и статус задач отображаются в чате.
   - Управление планом: статус, приостановка, сброс - через inline-меню менеджера.
   - Динамическая декомпозиция использует `min_tasks_dynamic` из `agent/manager.py` (`_min_tasks_dynamic(analysis)`):
     `max(MIN_TASKS_FLOOR, len(requirements)*MIN_TASKS_PER_REQ, len(remaining_work)*MIN_TASKS_PER_REMAINING) + ceil(not_done_checklist/3)`.
   - В структурной валидации плана применяются коды:
     `TASK_COUNT_BELOW_MIN` (если задач меньше `min_tasks_dynamic`) и
     `TASK_TOO_BROAD_REQ_COVERAGE` (если задача покрывает слишком много требований).
   - Константы декомпозиции/атомарности (`MIN_TASKS_FLOOR`, `MIN_TASKS_PER_REQ`,
     `MIN_TASKS_PER_REMAINING`, `ATOMICITY_MAX_REQS_PER_TASK`) являются внутренними
     для Manager и не выносятся в `config.yaml`.
   - Требуется настроенный OpenAI API и сессия текущего контекста.

8. **Режим SDD**
   - Включите через `/sessions` -> `SDD` - режим ведет фичу через `specify`, `plan`, `tasks`, `analyze` с гейтами подтверждения и правками на каждом этапе.
   - Спецификация и план пишутся в `specs/<NNN>-<slug>/spec.md` и `plan.md`, задачи - в `tasks.md`, отчёт о покрытии требований планом - в `analyze.md`; на терминальном гейте `analyze` ключевые решения фиксируются в `.cli-proxy/decisions.md` (ADR), после чего режим передает план Manager через `MANAGER_CONTINUE_TOKEN`.
   - В меню SDD доступна кнопка `Инициализировать проект`: для существующей кодовой базы режим сначала актуализирует Codebase Mapper, затем пишет `specs/_project/project-profile.generated.md`, `pack_manifest.json` и связанные SDD-артефакты; для пустой директории создаёт шаблоны в `specs/_templates/`.
   - Инициализация выбирает расширяемые artifact packs по evidence из проекта (`Cargo.toml`, `sdkconfig`, `platformio.ini`, `.csproj`, `pyproject.toml`, `package.json`, OpenAPI/AsyncAPI и другие маркеры). Если встроенные packs не покрывают проект, создаётся proposed pack в `.cli-proxy/sdd/packs/proposed/`; он не выбирается автоматически повторно и должен быть вручную перенесён/продвинут в project pack перед использованием как подтверждённое правило.
   - Можно стартовать сразу из исходного описания или сначала отправить его в Аналитик, после чего SDD продолжит по уточненному ТЗ.
   - Требуется настроенный OpenAI API и рабочая директория.

9. **Режим Аналитик**
   - Включите через `/sessions` -> `Analyst Аналитик` - система формирует полноценное ТЗ из исходного запроса.
   - В процессе может использовать инструменты агентной платформы (brainstorming, sequential thinking, web research, CLI-анализ через команды).
   - При нехватке данных задаёт уточняющие вопросы и поддерживает ответ кнопкой или через `Custom Свой вариант`.
   - Agent, Manager и Аналитик взаимоисключающие режимы.

10. **Режим Вебмастер**
   - Включите через `/sessions` -> `Webmaster Вебмастер` - режим ведет задачу через цикл intent/confirm/dev/validation и собирает структурированный отчёт по дефектам и чеклисту.
   - При невалидном JSON от `intent_plugin` применяется graceful fallback без падения процесса.
   - В destructive-callback действиях (`reset`, `clean`, `disconnect`) действует busy-guard: во время активного выполнения действия отклоняются.

11. **Режим Admin**
   - Управление режимом только через `/admin ...` для сессии текущего контекста: `/admin`, `help`, `enable`, `disable`, `status`, `incidents`, `actions`, `check`, `run`, `dry-run on|off`, `ack`, `mute`, `unmute`, `approvals list|revoke|clear`.
   - Включение/выключение Admin хранится отдельно в `admin_session_state` для пары `(chat_id, session_id)` и не меняет `session.modes.active_mode`.
   - При `enable` запускается фоновый цикл `Monitor -> Analyzer -> Executor -> Notifier` через `TaskService`, при `disable` и закрытии сессии задачи Admin отменяются.
   - Rule Engine Analyzer имеет приоритет над LLM fallback и обрабатывает обязательные сценарии: `502 + php-fpm down -> restart_php_fpm`, `postgresql down -> restart_postgresql`, `cpu_high -> notify_admin`, `ssl expiring -> notify_admin`.
   - Executor применяет Security Policies (cooldown/rate-limit/maintenance window/dry-run/mandatory notify), пишет действия в `admin_actions`, инциденты в `admin_incidents`, состояние алертов в `admin_alerts_state` и считает SLA-метрику доставки уведомлений.
   - Рискованные действия и low-confidence решения требуют подтверждения через `ask_user`; подтверждённые решения кешируются в `admin_approved_overrides` по hash (`action_id + params`) и переиспользуются без повторного запроса.
   - Команды управления подтверждениями: `/admin approvals list|revoke|clear`.

12. **Дополнительно**
   - Git-операции доступны через `/git` (status, pull, diff, commit, summary и т.д.).
   - Для администраторов доступна `/selfupdate` (обновление кода + перезапуск сервиса).

## Межрежимный handoff (оркестратор)
- После завершения mode-run оркестратор может предложить переход в другой режим.
- При подтверждении (`✅`) входом следующего режима становится полный payload из `session.orchestrator.last_mode_output`.
- До подтверждения перехода pending-ввод хранится в `session.orchestrator.pending_input`.
- Если полного payload нет, в следующий режим передается исходный пользовательский запрос.
- Два последовательных запуска с разным intent изолированы: handoff получает данные только последнего завершенного запуска.

Пример:
1. Менеджер завершает run и возвращает полный JSON-отчёт.
2. Результат сохраняется в `session.orchestrator.last_mode_output`.
3. Оркестратор предлагает переход в другой режим.
4. После подтверждения новый режим получает именно этот полный JSON.

## Модель статусов режимов
- Базовые стадии строятся единообразно через `ModeStatusService.build_common_mode_stage` / `build_common_mode_stage`: `выключен`, `обрабатывает задачу`, `ждет задачи в очереди`, `ожидает новый запрос`.
- Для доменных режимов применяются специализированные стадии: `build_manager_mode_stage` и `build_webmaster_mode_stage`.
- Статус Agent и Analyst расширен operational-полями: `pending_questions`, `active_plugin_flow`, `template`, `queue_origin`.
- Поля `pending_questions`, `active_plugin_flow`, `template`, `queue_origin` выводятся в статусах Telegram и в miniapp payload, что позволяет быстро понять, где именно режим ожидает действие пользователя.

## Актуальные изменения поведения
- **Analyst pre-run reset (Telegram + Desktop):** перед запуском Analyst и при повторном включении режима выполняется узконаправленный mode-scoped reset вместо полного сброса сессии. Очищаются analyst-specific state, blocking clarification хвосты, stale pending questions, runtime cache и помечается завершенным незавершённый analyst run artifact.
- **Сохранение полей сессии при reset:** при Analyst pre-run reset сохраняются `session.project_root`, `session.agent_memory`, `session.modes.active_mode`, `session.manager_quiet_mode`.
- **Desktop Agent runtime cleanup:** действия очистки Agent (`clean_all` и `clean_session`) в Desktop функционально эквивалентны Telegram и используют реальные операции `clear_sandbox`, `clear_session_files`, `clear_session_cache`.
- **Ошибки mode в CLI без silent fallback:** при внутренних ошибках `mode.handle_input` роутинг больше не падает в обычный CLI flow. Ошибка логируется через `logger.exception` и явно пробрасывается.
- **Очередь pending-вводов:** pending-вводы хранятся как FIFO-очередь (`deque`) на чат с лимитом `5` элементов. Новые вводы добавляются в хвост, при переполнении пользователь получает уведомление.
- **Телеметрия mode-задач в Desktop:** terminal-события эмитятся без дублей и классифицируются так: `task:cancelled` (cancelled), `task:completed` (без exception), `task:failed` (с exception).
- **Manager resume/pending prompt при сбое ask_user:** при ошибке выбора через `ask_user` исходный pending prompt сохраняется, пользователю отправляется сообщение о необходимости повторить выбор, очистка pending выполняется только после успешного resume.
- **Busy-guard для destructive callbacks:** во время busy-сессии действия `reset`/`clean`/`disconnect` отклоняются во всех режимах через единый роутер callback-ов.
- **Mode-level аудит callback-событий:** каждый вход в callback-обработчик логируется в stdout с `session_id`, `mode`, `action`, `chat_id`, timestamp и уровнем `INFO`.
- **Handoff full payload:** в `session.orchestrator.last_mode_output` сохраняется полный финальный результат режима, который затем передается в следующий режим без урезания до `_plan_summary`.
- **Operational status details:** Agent/Analyst статусы содержат `pending_questions`, `active_plugin_flow`, `template`, `queue_origin`.
- **Логирование ошибок:** во все новые `except`-ветки добавлено логирование через `logger.exception`.
- **Admin pipeline в фоне:** реализован полный конвейер `Monitor -> Analyzer -> Executor -> Notifier` с запуском через `TaskService` и отменой при `/admin disable`, закрытии или переключении сессии.
- **Admin E2E сценарии:** подтверждены цепочки `502 + php-fpm down -> restart -> notify` и `postgresql down -> ask_user -> restart`, включая устойчивость при локальном отказе одного сервера (обработка остальных продолжается).
- **Admin approvals reuse:** подтверждения рискованных действий переиспользуются через hash override (`admin_approved_overrides`), доступны команды `/admin approvals list|revoke|clear`.
- **Admin skill install approvals:** при `defaults.skill_install_policy=admin_approve` auto-discovery не пишет skill сразу, а создаёт pending approval в project-local ledger `.cli-proxy/skills/.skill_install_approval_ledger.json`; администратор подтверждает или отклоняет его через `/admin skills list|approve <approval_id>|reject <approval_id>`, Desktop Admin panel или MiniApp Admin tab.
- **Admin destructive recovery guard:** для native local/ssh remediation skill selector обходит transport path, а doctor не auto-replay'ит destructive execution; разрешены только rebuilt-state/manual confirmation варианты (`manual_review_required`, safe restart from boundary).

## Проектные prompts Manager/Webmaster
- **Где лежат файлы:** проектные prompt-файлы хранятся в рабочем каталоге сессии:
  - `.cli-proxy/.manager/prompt/prompts.yaml`
  - `.cli-proxy/.manager/prompt/learning.yaml`
  - `.cli-proxy/.webmaster/prompt/prompts.yaml`
  - `.cli-proxy/.webmaster/prompt/learning.yaml`
- **Автосоздание:** файлы создаются автоматически при создании сессии (`SessionManager.create`) и дополнительно проверяются при включении режима (Manager/Webmaster). Для Manager `prompts.yaml` инициализируется полным встроенным шаблоном (UI + оркестраторные prompts), `learning.yaml` создаётся как базовый YAML.
- **Невалидный YAML:** если `prompts.yaml` или `learning.yaml` повреждён, не проходит схему (включая обязательные ключи prompts), режим выдаёт явную ошибку в UI, пишет traceback через `logger.exception` и не запускается.
- **Runtime-источник:** Manager/Webmaster читают prompts только из project YAML текущего `session.workdir`. Промпты перечитываются при каждом включении режима, поэтому изменения в YAML применяются без рестарта процесса после повторного `enable`.

## Перенос сессий между CLI
- При переключении активного CLI бот предлагает перенести контекст предыдущего CLI, если у него есть `resume_token`.
- Перенос по умолчанию компактный: полный transcript читается из native-файла исходного CLI, сохраняется как evidence pack в `.cli-proxy/session-transfer/<transfer-id>/`, а в целевой CLI записывается короткая session capsule вместо всей истории.
- Evidence pack содержит `manifest.json`, `canonical.jsonl` и `capsule.md`. Целевой агент получает путь к этим файлам и должен читать их только при необходимости старого контекста.
- Низкоуровневые native writers остаются для совместимости и тестов, но основной пользовательский поток не вставляет весь transcript в контекст целевого CLI.

## Execution backends сессии
Для каждого CLI можно включить backend выполнения из настроек: `headless` или `tmux`. Это не заменяет `tools.<name>.mode`: `mode: headless` остаётся совместимостью текущего headless-контура, а `execution_backends` задаёт, какие backend доступны для CLI.

Пример:
```yaml
defaults:
  default_execution_backend: headless

tools:
  qwen:
    mode: headless
    cmd: ["qwen", "--yolo", "--prompt", "{prompt}", "--resume", "{resume}"]
    interactive_cmd: ["qwen", "--session-id", "{session_id}"]
    interactive_resume_cmd: ["qwen", "--resume", "{resume}"]
    execution_backends: ["headless", "tmux"]
    default_execution_backend: headless
```

- `headless` использует текущий per-request запуск CLI и сохраняет прежнее поведение.
- `tmux` запускает одну долгоживущую интерактивную сессию в `tmux`, пишет runtime-состояние в `.cli-proxy/runtime/tmux/` внутри рабочего каталога и отправляет запросы через pane/buffer без headless-команды.
- Для Claude, Codex, Grok, Qwen, Gemini и Kimi промежуточный статус, финальный ответ и завершение хода читаются прежде всего из native JSONL transcript. `pane.log` и маркер `DONE` остаются fallback при недоступном или неизвестном формате transcript. Отсутствие нового вывода не прерывает живую tmux-сессию; `Ctrl-C` отправляется только при явной остановке.
- При первом старте tmux backend может закрепить `resume_token` через `{session_id}` в `interactive_cmd` или через CLI-specific session flag, если CLI его поддерживает. Если интерактивный CLI печатает id сессии, можно задать `resume_regex`, и token будет сохранён из pane log.
- При штатном перезапуске бота или Desktop существующие tmux-сессии не закрываются. На следующем старте runtime проверяет их наличие, восстанавливает мониторинг активного запроса, доставляет завершившийся во время перезапуска ответ в исходный chat/thread или Desktop-сессию и затем продолжает сохранённую очередь. Явное закрытие сессии, interrupt, смена backend, смена активного CLI и `force fresh` по-прежнему останавливают соответствующую tmux-сессию. При смене CLI закрывается только tmux предыдущего CLI текущей bot-сессии; tmux других сессий не затрагиваются.
- Для восстановления после перезагрузки backend перед каждым не-первым запросом проверяет наличие tmux-сессии. Если tmux-сессия пропала и `resume_token` известен, запускается `interactive_resume_cmd` с `{resume}` или встроенная resume-команда для поддерживаемого CLI, а не headless-путь.
- Для известных CLI есть дефолтные resume-команды: `claude --resume <token>`, `gemini --resume <token>`, `qwen --resume <token>`, `grok --resume <token>` и `codex resume <token>`. Явный `interactive_resume_cmd` имеет приоритет.
- `tools.<name>.tmux_user` опционально запускает tmux-команды через `su - <user>`. Для root systemd-service его нужно задавать и для CLI, работающих от root (например, `root` для Codex/Grok): login-session выносит tmux-server из cgroup `bot.service`, поэтому systemd-рестарт не убивает его вместе с headless-процессами. Для Claude Code обычно используется отдельный `claude-bot`.
- Выбор backend задаётся только в настройках: `tools.<name>.default_execution_backend` имеет приоритет для конкретного CLI, `defaults.default_execution_backend` используется как общий fallback.
- Изменение `default_execution_backend` при runtime reload применяется к уже созданным сессиям, если их CLI поддерживает выбранный backend; session-level override больше не используется, а UI показывает backend только как read-only состояние.
- Пока tmux-запрос активен, меню занятой сессии позволяет отправить новое текстовое сообщение прямо в текущую pane либо оставить его отдельным элементом очереди; при непустой очереди его также можно объединить с последним элементом.
- В `tmux` v1 изображения не поддерживаются: image-запрос явно отклоняется, silent fallback в headless не выполняется.
- Для отката выставьте `defaults.default_execution_backend: headless` или `tools.<name>.default_execution_backend: headless` и выполните runtime reload. Idle tmux-сессии закрываются при смене backend; busy-сессии требуют дождаться завершения или закрыть/пересоздать сессию.
- CI покрывает tmux-контур guard-тестом: backend не должен использовать `headless_cmd` или headless-запуск CLI.

## Конфигурация
`config.yaml` поддерживает:
- `telegram.whitelist_chat_ids`: список разрешённых Telegram chat id (обязательный контроль доступа)
- `telegram.user_modes`: per-user allowlist режимов; поддерживает зарегистрированные режимы (`agent`, `analyst`, `manager`, `sdd`, `webmaster`, `codebase_mapper`), виртуальные `direct_cli` и `orchestrator`, а значение `"all"` включает и зарегистрированные mode, и эти виртуальные токены
- `tools.*`: команды запуска, режим, prompt/resume/help (включая `resume_cmd` для отдельных headless-команд возобновления), `image_cmd` (добавляется к базовой команде/resume_cmd для обработки изображений), а также `interactive_cmd`, `interactive_resume_cmd`, `execution_backends` и `default_execution_backend` для выбора backend `headless`/`tmux` из настроек
- `defaults.*`: базовый каталог, таймауты, пути к state, OpenAI настройки, `zai_api_key`/`tavily_api_key`/`jina_api_key` для web-поиска/reader, `github_token` для git по HTTPS, `gemini_oauth_client_secret` для обновления Gemini quota credentials, `default_execution_backend` для новых сессий
- `defaults.log_path`: путь к файлу логов бота (основной лог). Ошибки пишутся отдельно в файл `*_error.log` с теми же правилами ротации.
- `defaults.image_temp_dir`: каталог для временных изображений (относительно workdir или абсолютный).
- `defaults.image_max_mb`: лимит размера одного изображения (по умолчанию 10 МБ).
- `defaults.manager_*`: настройки режима Manager - лимит задач, попыток, таймауты декомпозиции/разработки/ревью, обрезка отчётов, автовозобновление плана, автокоммит (`manager_auto_commit`) после каждого одобренного шага, архив ответов в `.cli-proxy/.manager/response/`, prompt-learning в `.cli-proxy/.manager/prompt/`
- `defaults.cli_json_stream_archive_enabled`: включает архив raw/normalized JSONL событий для JSON-stream CLI в `.cli-proxy/cli-json-stream/`; полезно для отладки schema drift и проблем завершения CLI
- `defaults.assistant_preview_enabled`: если включено, Telegram и Desktop во время run показывают один live preview из последнего `assistant_text` CLI, обновляют его in-place и убирают после финального ответа
- Константы динамической декомпозиции и атомарности Manager остаются внутренними
  в `agent/manager.py` и не настраиваются через `config.yaml`.
- `defaults.analyst_use_cli_timeout_sec` / `defaults.webmaster_use_cli_timeout_sec`: отдельные таймауты `use_cli` для Analyst/Webmaster
- `defaults.webmaster_validation_max_fix_iterations`: максимум внутренних итераций доработки после провала валидации
- `defaults.codebase_mapper_usage`: режим работы Mapper (`auto|enabled|disabled`; сейчас `auto` эквивалентен `enabled`)
- `defaults.run_artifacts_*`: durable run traces и retention cleanup (`STATE/PLAN/CHECKPOINTS/METRICS/RECOVERY/EVENTS`) для doctor/recover/resume и UI run panels
- `defaults.run_doctor_enabled` / `defaults.run_boundary_validation_enabled` / `defaults.run_metrics_enabled`: включают doctor backend, phase gates и per-run token/cost/duration aggregates в `METRICS.json`
- `defaults.skill_discovery_mode`: `off` отключает selection, `suggest` подмешивает только уже доступные skills, `auto` разрешает discover/install allowlisted skills в project-local registry
- `defaults.skill_install_policy`: `manual` только предлагает skill, `admin_approve` создаёт pending approval в `.cli-proxy/skills/.skill_install_approval_ledger.json` и требует explicit admin approve/reject, `allowlisted_auto` разрешает автоустановку allowlisted discovery в project-local registry
- `defaults.skill_registry_paths` / `defaults.skill_allowlisted_sources`: project/global registry roots и allowlist источников, из которых skill runtime имеет право выбирать или устанавливать skills
- `mcp.*`: TCP-bridge для внешних клиентов (host/port/token)
- `mcp_clients`: канонический список MCP-клиентов/HTTP endpoints для сериализации и diff
- `miniapp.*`: встроенный HTTP MiniApp для админов (`enabled`, `bind_host`, `bind_port`, `base_path`, `public_url`, `max_edit_file_size_kb`, `enable_delete`)
- `thread_mode.*`: runtime-настройки тредов/топиков (`enabled`, `mode`, `topics_chat_id`, `topic_title_prefix`, `inactivity_ttl_sec`)
- `webhooks.*`: webhook-режим Telegram на shared ingress (`enabled`, `path`, `public_base_url`, `secret_token`, `request_timeout_sec`, `max_payload_bytes`)
- `scheduler.*`: встроенный планировщик (`enabled`, `timezone`, `tick_interval_sec`, `max_concurrent_jobs`, `job_timeout_sec`, `misfire_grace_sec`)
- `presets`: список шаблонов задач (name + prompt)

### Typed config: `AppConfig` / `AppConfigModel`
- Typed loader сначала валидирует YAML/.env/env overlay через Pydantic-модель `app/config_runtime/models.py:AppConfigModel`, затем адаптирует результат в runtime dataclass `AppConfig`.
- Каноническая runtime-схема `AppConfig`:
```yaml
telegram: {}
tools: {}
defaults: {}
mcp: {}
miniapp: {}
thread_mode: {}
webhooks: {}
scheduler: {}
mcp_clients: []
presets: []
```
- Новые секции `thread_mode`, `webhooks`, `scheduler` валидируются fail-fast. Например, `thread_mode.mode=group` требует `thread_mode.topics_chat_id`, а `webhooks.path` и `miniapp.base_path` обязаны начинаться с `/`.

### Env overrides для typed config
- Порядок наложения: `config.yaml` -> legacy env aliases из `.env`/process env -> `CLI_PROXY__*` overrides. Внутри env-слоя process env побеждает `.env` для одной и той же переменной.
- Общий префикс override: `CLI_PROXY__`. Путь собирается через `__`, регистр не важен, значения парсятся через YAML (`true`, `30`, `[]`, `{}` и т.д.).
- Для `defaults.*` также поддерживаются legacy env-имена: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BIG_MODEL`, `OPENAI_BASE_URL`, `ZAI_API_KEY`, `TAVILY_API_KEY`, `JINA_API_KEY`, `GITHUB_TOKEN`. Они тоже override-ят значения из YAML, но остаются менее явным механизмом, чем `CLI_PROXY__DEFAULTS__...`.
- Примеры для новых секций:
```bash
export CLI_PROXY__THREAD_MODE__ENABLED=true
export CLI_PROXY__THREAD_MODE__MODE=group
export CLI_PROXY__THREAD_MODE__TOPICS_CHAT_ID=-1001234567890

export CLI_PROXY__WEBHOOKS__ENABLED=true
export CLI_PROXY__WEBHOOKS__PUBLIC_BASE_URL=https://example.com
export CLI_PROXY__WEBHOOKS__SECRET_TOKEN=super-secret

export CLI_PROXY__SCHEDULER__ENABLED=true
export CLI_PROXY__SCHEDULER__TIMEZONE=Europe/Moscow
export CLI_PROXY__SCHEDULER__MAX_CONCURRENT_JOBS=2
```

### Каноническая сериализация и semantic diff
- Канонический output использует только `mcp_clients`.
- Для HTTP MCP endpoints используется тот же список `mcp_clients` с `transport=http`, `url`, `headers`, `timeout_ms`.
- Diff в `ConfigService` семантический: он сравнивает канонически сериализованное представление текущего файла и нового `AppConfig`, поэтому отсутствие реальных изменений не создаёт ложный diff, не переписывает файл и не создаёт `.bak`.

### Контракты схем (режимы)
- Канонические схемы режимов находятся в:
  - `modes/manager/schemas.py`
  - `modes/webmaster/schemas.py`
  - `modes/agent/schemas.py`
  - `modes/analyst/schemas.py`
  - `modes/sdd/schemas.py`
- Feature-gating для схем удалён: валидация работает через единый pipeline.
- Legacy parsing fallback удалён: structured parsing работает только через V2 normalizer (`parse_normalize_validate`/`loads_safe`).

### MiniApp
- Команда `/miniapp` открывает Telegram MiniApp для разрешённых пользователей (`telegram.admlist_chat_ids` и whitelist+ACL).
- Shared ingress bind адрес берётся из `miniapp.bind_host`/`miniapp.bind_port` и используется и для MiniApp, и для webhook ingress.
- Базовый путь MiniApp: `miniapp.base_path` (по умолчанию `/cli-proxy`), API под префиксом `/cli-proxy/api/*`.
- Для кнопки `/miniapp` задайте `miniapp.public_url` абсолютным URL (например, `https://example.com/cli-proxy`).
- Авторизация API: заголовок `X-Telegram-Init-Data` с валидацией подписи Telegram WebApp.
- Контур `Логи`:
  - 5 типов логов: `main`, `error`, `agent`, `cli_dialog`, `miniapp`;
  - realtime-поток через WebSocket;
  - режимы истории: `только новые`, `100/200/500/1000 предыдущих`;
  - фильтр по каноническому `session_uid`, связанному с `ConversationScope`, с меткой имени сессии;
  - скачивание строк, отображаемых в MiniApp.
- Роли доступа в логах:
  - админ видит все записи и все `session_uid`;
  - обычный пользователь видит только свои записи/сессии.
- Контур `Config`: редактирование `config.yaml` через формы + preview diff + сохранение + автоматический `reload_runtime_config()`.
- Контур `Файлы`: только `workdir`, определённый явным `project_slug` + `session_uid`.
  - При отсутствии явного контекста все `files/*` эндпоинты (включая `files/tree`) возвращают `409 Session context required`.
  - `config.yaml` через файловый API запрещён (доступен только через `Config UI`).
  - Бинарные/слишком большие файлы на редактирование блокируются.

### Hot-reload матрица
- После сохранения Config UI всегда вызывает `reload_runtime_config()`, но применяются только поля,
  которые `ConfigApplyPolicy` классифицирует как `hot_reload`. Остальные попадают в
  `restart_required` и остаются изменением YAML до рестарта процесса или отдельного
  операторского действия.
- Применяется без рестарта:
  - `defaults.cli_json_stream_archive_enabled`, `defaults.assistant_preview_enabled`,
    `defaults.pending_input_confirmation_enabled`;
  - `tools.*` (для новых запусков и текущих сессий где возможно);
  - `presets`, `security.*`, `lint_evolution.*`;
  - `telegram.whitelist_chat_ids`, `telegram.admlist_chat_ids`, `telegram.user_workdirs`, `telegram.user_modes`;
  - `miniapp.public_url`, `miniapp.enable_delete`;
  - `mcp.token`, `webhooks.secret_token`;
  - secret-поля `defaults.openai_api_key`, `defaults.zai_api_key`, `defaults.github_token`,
    `defaults.tavily_api_key`, `defaults.jina_api_key`.
- Требует рестарт процесса или отдельное операторское действие:
  - `telegram.token`;
  - Telegram transport/polling timeouts (`connection_pool_size`, `*_timeout_sec`, `polling_timeout_sec`, `poll_interval_sec`);
  - runtime/skills-флаги `defaults.run_artifacts_enabled`, `defaults.run_artifacts_retention_days`,
    memory-learning флаги `defaults.memory_events_enabled`,
    `defaults.memory_native_cli_hooks_enabled`, `defaults.memory_outcomes_enabled`,
    `defaults.memory_dreaming_enabled`, `defaults.memory_events_retention_days`,
    `defaults.memory_events_max_payload_chars`, `defaults.memory_events_redaction_enabled`,
    `defaults.memory_dreaming_batch_size`, `defaults.run_doctor_enabled`,
    `defaults.run_boundary_validation_enabled`,
    `defaults.run_metrics_enabled`, `defaults.skill_discovery_mode`,
    `defaults.skill_install_policy`, `defaults.skill_registry_paths`,
    `defaults.skill_allowlisted_sources`, `defaults.gemini_oauth_client_secret`;
  - `miniapp.enabled`, `miniapp.bind_host`, `miniapp.bind_port`, `miniapp.base_path`, `miniapp.max_edit_file_size_kb`;
  - `mcp.enabled`, `mcp.host`, `mcp.port`, `mcp_clients`;
  - `thread_mode.*`, `scheduler.*`;
  - `webhooks.enabled`, `webhooks.path`, `webhooks.public_base_url`,
    `webhooks.request_timeout_sec`, `webhooks.max_payload_bytes`.
- MiniApp и Desktop используют ту же policy: diff показывает `applied` только для hot-reload
  полей, а `scheduler`, `thread_mode`, `mcp` transport/client settings и webhook endpoint
  settings не обещают hot-apply.

### Runtime reload и события
- Runtime reload использует тот же strict typed loader, что и startup: конфиг заново читается, накладывает env overrides, валидируется как `AppConfigModel` и только потом адаптируется в `AppConfig`.
- Если валидация не проходит, старый runtime-конфиг остаётся активным. Ошибка пишется в лог loader'а, а reload возвращает статус ошибки без частичного применения поломанного YAML.
- При успешном reload конфиг безопасно подменяется в `BotApp`, session runtime и mode runtime; результат дополнительно публикуется через `SystemEventBus`.
- События reload:
  - `runtime.config.reloaded` - reload успешен;
  - `runtime.config.reload_failed` - reload отклонён из-за ошибки валидации или чтения.
- Payload события содержит `path`, `status`, `applied`, `restart_required`, `warnings`. Это тот же набор данных, который использует MiniApp `Config` UI для preview/save/reload цикла.

Для каждого инструмента можно задать переменные окружения:
```yaml
tools:
  codex:
    env:
      OPENAI_API_KEY: "..."
      OPENAI_MODEL: "..."
```
Эти переменные будут добавлены к окружению процесса CLI.

Поддерживаются подстановки переменных окружения вида `${VAR}`.
Значения `null` игнорируются и не добавляются в окружение.

Grok Build CLI добавляется как `tools.grok`; headless-режим использует официальный
`grok --output-format streaming-json -p "{prompt}"`, а ключ xAI можно передать через
`XAI_API_KEY` в `.env` или системном окружении. Для `/limits` Grok показывает
локальный usage последней сессии проекта из `~/.grok/sessions`; персональные RPM/TPM
квоты xAI доступны в Console и не выдаются стабильным CLI/API. Перенос сессий
Grok использует тот же локальный session store и компактный transfer capsule.

Kimi Code CLI добавляется как `tools.kimi`; headless-режим использует официальный
prompt mode `kimi --output-format stream-json --prompt "{prompt}"`. Флаг
`--prompt` сам включает неинтерактивный режим, а `--yolo`, `--auto` и `--plan`
kimi запрещает вместе с ним, поэтому в headless их нет; в tmux-режиме `--yolo`
задаётся в `interactive_cmd`. Продолжение сессии идёт по токену: kimi печатает его
последней строкой хода (`{"role":"meta","type":"session.resume_hint",...}`), мост
сохраняет токен и следующий запрос отправляет с `--resume <token>` (алиас
`--session`). Флага `--continue` в команде нет намеренно: он подхватил бы
последнюю сессию каталога вместо новой, а вместе с `--session` kimi его и не
принимает (если добавить его в `config.yaml`, мост уберёт его при resume).
Ключ передаётся через
`KIMI_API_KEY` в `.env` или системном окружении, авторизация по OAuth выполняется
командой `kimi login`. `/limits` для Kimi показывает пометку о недоступности квот:
CLI не публикует usage. Перенос сессий, чтение native transcript в tmux-режиме и
подстановка недавних сессий в пикер resume работают через журнал
`~/.kimi-code/sessions/<ключ каталога>/<id сессии>/agents/main/wire.jsonl`; ключ
каталога повторяет `encodeWorkDirKey` kimi (`wd_<слаг имени каталога>_<12 hex
sha256 от абсолютного пути>`). В перенесённую сессию мост дописывает запись
`profile.bind`: headless `--resume` сам профиль не привязывает и без неё падает с
`model.not_configured`. Привязка берётся из сессии, которую kimi уже отрисовал для
этого каталога, иначе из `default_model` в `~/.kimi-code/config.toml`. Перед
первым запуском в новом каталоге kimi спрашивает про доверие к папке; в tmux-режиме
мост распознаёт этот экран и просит открыть kimi в рабочем каталоге вручную.

opencode добавляется как `tools.opencode`; headless-режим использует
`opencode run --format json --session "{resume}" "{prompt}"` - промпт здесь
позиционный аргумент, а не флаг. `--format json` включает построчный поток
событий (`text`, `tool_use`, `step_start`, `step_finish`, `reasoning`, `error`),
в каждой строке приходит `sessionID`, поэтому `resume_regex` не нужен. Флага
`--resume` у opencode нет: токен подставляется в `--session`, а при пустом
токене пара `--session {resume}` из команды выпадает целиком. Модель в
`config.yaml` не фиксируется - её выбирает сам opencode (`opencode providers`,
`~/.config/opencode/opencode.json`), авторизация выполняется командой
`opencode auth login`. tmux-режим не заявлен (`execution_backends: ["headless"]`):
TUI opencode работает только в полноэкранном режиме, парсер экрана под него не
написан. `/limits` для opencode показывает пометку о недоступности квот: лимиты
держит выбранный провайдер, а не сам CLI. Перенос сессий и подстановка недавних
сессий в пикер resume работают через единую базу
`~/.local/share/opencode/opencode.db` (или `$XDG_DATA_HOME/opencode/opencode.db`).
Перенесённой сессии мост проставляет провайдера и модель из самого свежего
сообщения в этой базе: при `--session` opencode берёт модель из последнего
пользовательского сообщения и с чужим именем падает с
`ProviderModelNotFoundError`. Если opencode в этой системе ещё ни разу не
запускался, переносить некуда - мост пропускает перенос с предупреждением.

Для обновления Gemini OAuth credentials при сборе CLI-лимитов задайте в `config.yaml`:
```yaml
defaults:
  gemini_oauth_client_secret: "..."
```

## YOLO/auto-approve режим
В `config.yaml` включены авто-одобрения:
- **Gemini CLI**: `--approval-mode yolo`
- **Qwen Code**: `--yolo`
- **Claude Code**: `--dangerously-skip-permissions`
- **Codex**: в интерактивном режиме отправляется `/approvals full`

OpenAI может быть задан как через `config.yaml`, так и через env:
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

Команда `/selfupdate` использует имя systemd-сервиса из env-переменной `CLI_PROXY_SERVICE_NAME`
(по умолчанию `cli-proxy-bot`).
- `OPENAI_BASE_URL`

Переменные окружения имеют приоритет.

Z.AI для web-поиска/reader:
- `defaults.zai_api_key` в `config.yaml`
- или env `ZAI_API_KEY` (имеет приоритет)

Tavily для web-поиска/reader:
- `defaults.tavily_api_key` в `config.yaml`
- или env `TAVILY_API_KEY` (имеет приоритет)
Tavily даёт бесплатные месячные лимиты - их можно использовать.

Jina.ai для web-поиска/reader:
- `defaults.jina_api_key` в `config.yaml`
- или env `JINA_API_KEY` (имеет приоритет)

GitHub токен (PAT) для git по HTTPS:
- `defaults.github_token` в `config.yaml` (используется для clone/fetch/pull/push, без интерактивных запросов)

## Вложения сообщений
- Текстовые вложения: `.txt`, `.md`, `.rst`, `.log`, `.html`, `.htm` (до 500 КБ). Бот объединяет подпись и содержимое файла и отправляет в CLI.
- Изображения: `.png`, `.jpg`, `.jpeg` (photo/document). Для обработки нужен `image_cmd` у выбранного CLI.
  Если у текущего инструмента нет `image_cmd`, бот сообщит, что изображения не поддерживаются.
  В качестве текста запроса используется подпись к изображению.

## Команды бота
### Видимые в меню Telegram
- `/sessions` - меню управления сессиями (новая/список/status/rename/close/resume/state/queue/clearqueue/reset + агент/менеджер/SDD/аналитик/вебмастер/mapper)
- `/interrupt` - прервать генерацию
- `/git` - Git-операции по сессии текущего контекста (inline-меню: status/fetch/pull/merge/rebase/diff/log/stash/commit/push/summary/help)
- `/files` - управление файлами рабочей директории (отправка/сохранение/переименование/удаление)
- `/miniapp` - открыть MiniApp администрирования
- `/reports` - показать, создать или отправить отчёты текущей сессии (`latest`, `snapshot`, `generate`, `<filename.md>`)
- `/tools` - список инструментов
### Доступны, но скрыты из меню
- `/dirs` - просмотр каталогов (menu)
- `/newpath <path>` - задать путь после выбора инструмента
- `/cwd <path>` - создать сессию в каталоге
- `/setprompt <tool> <regex>` - установить prompt_regex для инструмента
- `/send <текст>` - отправить текст напрямую в CLI
- `/resume [token]` - показать/установить resume
- `/close` - закрыть сессию и удалить её из сохранённого состояния (menu)
- `/status` - статус сессии текущего контекста (busy, последняя активность/тик секундомера)
- `/rename <id> <name>` или `/rename <name>` - переименовать сессию
- `/state [tool path]` - состояние по tool+path или меню
- `/clearqueue` - очистить очередь сессии текущего контекста
- `/queue` - показать очередь
- `/preset` - шаблоны задач для CLI
- Шаблоны берутся из `config.yaml` (секция `presets`).
- `/metrics` - метрики бота
- `/admin` - открыть меню и краткий статус Admin для сессии текущего контекста
- `/admin help|enable|disable|status` - базовое управление режимом Admin
- `/admin incidents [N] | /admin actions [N]` - журнал инцидентов/действий текущей сессии
- `/admin check <server_id> | /admin run <action_id> <server_id>` - запуск check/run только для server/action из сессионного admin-конфига
- `/admin dry-run on|off` - переключение dry-run для текущей сессии
- `/admin ack <incident_id> | /admin mute <minutes> | /admin unmute` - подтверждение инцидентов и mute-управление
- `/admin approvals list|revoke <id>|clear` - управление подтверждёнными override
- `/admin skills list|approve <approval_id>|reject <approval_id>` - просмотр и разбор pending skill install approvals для `skill_install_policy=admin_approve`

## Run operations: `doctor` / `recover` / `resume`
- В inline-панелях Analyst, Agent, Manager и Webmaster доступны явные run-операции `doctor`, `recover`, `resume`.
- Те же действия доступны в Desktop через панель `Runs` и в MiniApp через `Status -> Runs`.
- `doctor` читает текущие `STATE.json`, `PLAN.json`, `CHECKPOINTS.json`, `METRICS.json`, `EVENTS.jsonl` и legacy-store режима, затем пишет структурированный результат в `RECOVERY.json`.
- `recover` фиксирует безопасное recovery-намерение по рекомендации doctor и не выполняет произвольный mid-step replay.
- `resume` разрешается только с безопасной границы, определённой doctor/recover; конкурентный запуск блокируется busy/lock/tick guard'ами.
- Эти run-операции отличаются от команды `/resume [token]`: slash-команда управляет CLI resume-токеном, а кнопки `doctor/recover/resume` работают с runtime recovery текущего run.

## Состояния
- `state.json` - хранит состояние по сессиям: `resume_token`, `summary/updated_at`, очередь, активный режим (`agent`/`manager`/`sdd`/`analyst`/`webmaster`/`codebase_mapper`), память агента, `project_root`.
- `state.json` - хранит поля межрежимного handoff и оркестрации: `advanced_orchestrator_enabled`, `orchestrator_pending_input`, `orchestrator_last_mode_output`, `orchestrator_last_mode_id`.
- `state.json` - хранит список сессий и scope-bound привязки для восстановления после перезапуска.

## Тесты
```bash
pytest -q
```

## Прямой ввод в CLI
Если нужно отправить строку, начинающуюся с `/`, используйте один из способов:
- `/send /help`
- Или префикс `>` в обычном сообщении: `> /help`
- `/state [tool path]` - состояние по tool+path или меню
