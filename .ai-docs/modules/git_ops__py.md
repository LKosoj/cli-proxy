# git_ops

Модуль предоставляет инструменты для управления Git-операциями в Telegram-боте через асинхронные вызовы. Он интегрируется с сессиями пользователя, обеспечивает безопасный ввод данных через временные скрипты и формирует интерактивные клавиатуры для выполнения типичных Git-команд. Все операции выполняются с учётом состояния сессии и наличия активного репозитория. Поддерживается обработка конфликтов, многошаговые операции и аутентификация через GitHub-токен.

Ключевые структуры данных  
git_branch_menu — кэш веток для выбора пользователем, индексируется по chat_id  
git_pending_ref — хранит выбранную ветку для последующих операций (например, merge/rebase)  
git_pull_target — целевая ветка для операции pull  
pending_git_commit — сообщение коммита, накапливаемое при интерактивном вводе  
git_env — окружение с настройками для бесшовного выполнения Git-команд с токеном

GitOps.__init__(self, config, manager, send_message, send_document, short_label, handle_cli_input)
Инициализирует компонент управления Git-операциями
Аргументы
config — конфигурация приложения с токенами и настройками
manager — менеджер сессий для доступа к активной сессии
send_message — асинхронная функция отправки текстового сообщения
send_document — асинхронная функция отправки файла
short_label — функция сокращённого отображения путей или имён
handle_cli_input — функция передачи команды в CLI-сессию

---
GitOps._ensure_git_askpass(self)
Создаёт временный скрипт для аутентификации в Git через токен
Возвращает
Путь к скрипту, если токен задан; иначе — None

---
GitOps.git_env(self)
Формирует переменные окружения для выполнения Git-команд с аутентификацией
Возвращает
Словарь переменных окружения с настройками GIT_ASKPASS и токеном

---
GitOps.build_git_keyboard(self)
Создаёт клавиатуру с основными Git-командами
Возвращает
InlineKeyboardMarkup с кнопками для Git-операций

---
GitOps._build_git_branches_keyboard(self, chat_id, action)
Формирует клавиатуру выбора ветки из сохранённого списка
Аргументы
chat_id — идентификатор чата
action — действие (merge, rebase и т.д.), используемое в callback_data
Возвращает
InlineKeyboardMarkup с кнопками веток и отмены

---
GitOps._build_git_pull_keyboard(self, ref)
Создаёт клавиатуру выбора стратегии pull (merge/rebase)
Аргументы
ref — имя ветки, с которой выполняется pull
Возвращает
InlineKeyboardMarkup с кнопками merge, rebase и отмены

---
GitOps._build_git_confirm_keyboard(self, action, ref)
Формирует клавиатуру подтверждения операции merge или rebase
Аргументы
action — тип операции: "merge" или "rebase"
ref — целевая ветка
Возвращает
InlineKeyboardMarkup с кнопкой подтверждения и отмены

---
GitOps._build_git_conflict_keyboard(self)
Создаёт клавиатуру управления конфликтом при merge/rebase
Возвращает
InlineKeyboardMarkup с действиями: diff, abort, continue, вызов агента

---
GitOps._ensure_git_state(self, session)
Инициализирует атрибуты сессии, связанные с Git, если они отсутствуют
Аргументы
session — активная сессия пользователя

---
GitOps.ensure_git_session(self, chat_id, context)
Проверяет наличие активной сессии и инициализирует Git-состояние
Аргументы
chat_id — идентификатор чата
context — контекст выполнения Telegram-бота
Возвращает
Активную сессию при успехе, иначе — None

---
GitOps._session_label(self, session)
Формирует краткую строковую метку для сессии
Аргументы
session — сессия пользователя
Возвращает
Человекочитаемое описание сессии с ID и именем

---
GitOps._send_git_message(self, context, chat_id, session, text)
Отправляет сообщение с префиксом сессии
Аргументы
context — контекст выполнения
chat_id — идентификатор чата
session — активная сессия
text — текст сообщения

---
GitOps.ensure_git_repo(self, session, chat_id, context)
Проверяет, что рабочая директория сессии — это Git-репозиторий
Аргументы
session — активная сессия
chat_id — идентификатор чата
context — контекст выполнения
Возвращает
True, если это репозиторий; иначе — False

---
GitOps.ensure_git_not_busy(self, session, chat_id, context)
Проверяет, что сессия не занята другой операцией
Аргументы
session — активная сессия
chat_id — идентификатор чата
context — контекст выполнения
Возвращает
True, если сессия свободна; иначе — False

---
GitOps._run_git(self, session, args)
Асинхронно выполняет Git-команду в рабочей директории сессии
Аргументы
session — активная сессия
args — список аргументов команды git
Возвращает
Кортеж: (код возврата, вывод команды)

---
async def _run_git(session: Session, args: list[str]) -> tuple[int, str]
Выполняет git-команду с заданными аргументами и возвращает код возврата и вывод.
Аргументы
session — активная сессия, содержащая контекст выполнения
args — аргументы команды git
Возвращает
Кортеж из кода возврата (0 — успех) и строкового вывода команды
Исключения
Может выбрасывать исключения при ошибках выполнения процесса (обрабатываются внутри)

---
async def _git_current_branch(session: Session) -> Optional[str]
Возвращает текущую активную ветку Git.
Аргументы
session — сессия выполнения
Возвращает
Имя ветки или None, если определить не удалось

---
async def _git_upstream(session: Session) -> Optional[str]
Определяет upstream-ветку для текущей ветки.
Аргументы
session — сессия выполнения
Возвращает
Имя upstream-ветки (например, origin/main) или None

---
async def _git_ref_exists(session: Session, ref: str) -> bool
Проверяет, существует ли указанный Git-референс.
Аргументы
session — сессия выполнения
ref — имя референса (например, HEAD, origin/main)
Возвращает
True, если референс существует, иначе False

---
async def _git_default_remote(session: Session) -> Optional[str]
Определяет ветку по умолчанию в удаленном репозитории (origin/HEAD).
Аргументы
session — сессия выполнения
Возвращает
Имя ветки по умолчанию (например, origin/main) или None

---
async def _git_ahead_behind(session: Session, ref: str) -> Optional[tuple[int, int]]
Возвращает количество коммитов, на которые HEAD опережает и отстаёт от указанной ветки.
Аргументы
session — сессия выполнения
ref — референс для сравнения (например, origin/main)
Возвращает
Кортеж (ahead, behind) или None при ошибке

---
async def _git_in_progress(session: Session) -> Optional[str]
Определяет, выполняется ли в данный момент rebase или merge.
Аргументы
session — сессия выполнения
Возвращает
Строка "rebase" или "merge", если процесс активен; иначе None

---
def _git_set_conflict(session: Session, files: list[str], kind: Optional[str]) -> None
Помечает сессию как находящуюся в состоянии конфликта.
Аргументы
session — сессия выполнения
files — список конфликтующих файлов
kind — тип конфликта ("rebase" или "merge")

---
def _git_clear_conflict(session: Session) -> None
Снимает флаг конфликта в сессии.
Аргументы
session — сессия выполнения

---
async def _git_conflict_files(session: Session) -> list[str]
Получает список файлов с конфликтами слияния.
Аргументы
session — сессия выполнения
Возвращает
Список путей к конфликтующим файлам

---
async def _git_status_text(session: Session) -> str
Формирует человекочитаемое описание состояния Git-репозитория.
Аргументы
session — сессия выполнения
Возвращает
Форматированный текст с информацией о ветке, состоянии, upstream и конфликтах

---
async def _send_git_help(session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None
Отправляет пользователю справку по Git из файла git.md в виде документа.
Аргументы
session — сессия выполнения
chat_id — идентификатор чата для отправки
context — контекст бота Telegram
Исключения
Обрабатывает ошибки чтения файла и отправки документа

---
async def _git_commit_context(session: Session) -> Optional[str]
Собирает контекст изменений для формирования коммита (status, diff --stat, полный diff).
Аргументы
session — сессия выполнения
Возвращает
Текст с деталями изменений или None при ошибке

---
def _sanitize_commit_message(message: str, max_len: int = 100) -> str
Очищает и обрезает первую строку сообщения коммита до допустимой длины.
Аргументы
message — исходное сообщение
max_len — максимальная длина (по умолчанию 100)
Возвращает
Очищенное и усечённое сообщение

---
def _sanitize_commit_body(body: str, max_len: int = 2000) -> str
Очищает и обрезает тело сообщения коммита.
Аргументы
body — исходное тело сообщения
max_len — максимальная длина (по умолчанию 2000)
Возвращает
Очищенное и усечённое тело

---
async def _build_commit_body(session: Session) -> Optional[str]
Формирует тело коммита на основе diff --stat и git status.
Аргументы
session — сессия выполнения
Возвращает
Текст тела коммита или None, если изменений нет

---
async def _send_git_output(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: Session, output: str, header: Optional[str] = None) -> None
Отправляет текстовый вывод команды Git в чат.
Аргументы
context — контекст бота Telegram
chat_id — идентификатор получателя
session — сессия выполнения
output — содержимое для отправки
header — необязательный заголовок сообщения
Исключения
Обрабатывает ошибки отправки сообщения

---
async def _send_git_output(chat_id: int, session: Session, title: str, output: str) -> None
Отправляет отформатированный вывод Git-команды пользователю, обрезая длинный текст.
Аргументы
chat_id — идентификатор чата для отправки сообщения
session — сессия пользователя с контекстом репозитория
title — заголовок сообщения (например, "Git commit")
output — вывод команды, который будет обрезан при превышении длины
Возвращает
Ничего не возвращает
Исключения
Возможны исключения при отправке сообщения через Telegram API

---
async def _execute_git_commit(session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message: str, body: Optional[str] = None) -> None
Выполняет добавление изменений и коммит в Git с обработкой вывода и обновлением статуса.
Аргументы
session — активная сессия с репозиторием
chat_id — идентификатор чата для уведомлений
context — контекст выполнения Telegram-бота
message — основное сообщение коммита
body — дополнительное тело коммита (опционально)
Возвращает
Ничего не возвращает
Исключения
Возможны исключения при выполнении Git-команд или отправке сообщений

---
async def _handle_git_conflict(session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None
Уведомляет пользователя о наличии конфликтов слияния и предлагает действия через клавиатуру.
Аргументы
session — сессия с репозиторием, в которой обнаружены конфликты
chat_id — идентификатор чата для отправки уведомления
context — контекст выполнения Telegram-бота
Возвращает
Ничего не возвращает
Исключения
Возможны исключения при отправке сообщения

---
async def _git_merge_or_rebase(session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE, action: str, ref: str) -> None
Выполняет операцию слияния (merge) или перебазирования (rebase) с указанной веткой или ссылкой.
Аргументы
session — сессия с репозиторием
chat_id — идентификатор чата для уведомлений
context — контекст выполнения бота
action — действие: "merge" или "rebase"
ref — ссылка на ветку или коммит для слияния/перебазирования
Возвращает
Ничего не возвращает
Исключения
Возможны исключения при выполнении Git-команд или отправке сообщений

---
async def handle_pending_commit_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool
Обрабатывает ввод пользователя для сообщения коммита, запускает коммит или отменяет операцию.
Аргументы
chat_id — идентификатор чата, где вводится сообщение
text — введённый текст сообщения коммита
context — контекст выполнения Telegram-бота
Возвращает
True, если сообщение было обработано (даже при отмене), иначе False
Исключения
Возможны исключения при работе с сессией или отправке сообщений

---
async def handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool
Обрабатывает callback-запросы от inline-кнопок, связанных с Git-операциями.
Аргументы
query — объект callback-запроса от Telegram
chat_id — идентификатор чата
context — контекст выполнения бота
Возвращает
True, если запрос был распознан и обработан, иначе False
Исключения
Возможны исключения при выполнении Git-команд или отправке сообщений

---
async def _handle_git_pull_ff_only(context, chat_id, session, upstream)
Выполняет git pull --ff-only и обновляет статус при успехе
Аргументы
context — контекст выполнения Telegram-обработчика
chat_id — идентификатор чата
session — объект сессии пользователя
upstream — имя удалённой ветки для pull
Возвращает
True при успешной обработке действия
Исключения
Возможны исключения при выполнении Git-команд или отправке сообщений

---
async def _git_merge_or_rebase(session, chat_id, context, action, ref)
Выполняет операцию merge или rebase с указанной ссылкой
Аргументы
session — объект сессии пользователя
chat_id — идентификатор чата
context — контекст выполнения Telegram-обработчика
action — тип операции: "merge" или "rebase"
ref — ссылка (ветка) для применения операции
Возвращает
None
Исключения
Возможны исключения при выполнении Git-команд или отправке сообщений

---
async def _git_ahead_behind(session, ref)
Возвращает количество коммитов ahead и behind относительно указанной ветки
Аргументы
session — объект сессии пользователя
ref — имя ветки для сравнения
Возвращает
Кортеж (ahead, behind) при успехе, иначе None
Исключения
Возможны ошибки при выполнении Git-команды

---
async def _git_status_text(session)
Формирует краткое текстовое представление статуса Git
Аргументы
session — объект сессии пользователя
Возвращает
Строка с текстом статуса
Исключения
Возможны ошибки при выполнении Git-команды

---
async def _git_conflict_files(session)
Определяет наличие файлов с конфликтами слияния
Аргументы
session — объект сессии пользователя
Возвращает
Список имён файлов с конфликтами, или пустой список
Исключения
Возможны ошибки при выполнении Git-команды

---
async def _git_commit_context(session)
Собирает контекст изменений для генерации сообщения коммита
Аргументы
session — объект сессии пользователя
Возвращает
Словарь с данными изменений или None при ошибке
Исключения
Возможны ошибки при выполнении Git-команд

---
async def _build_commit_body(session)
Формирует тело коммита на основе diff
Аргументы
session — объект сессии пользователя
Возвращает
Текст тела коммита или None
Исключения
Возможны ошибки при выполнении Git-команд

---
async def _execute_git_commit(session, chat_id, context, message, body=None)
Выполняет коммит с указанным сообщением и опциональным телом
Аргументы
session — объект сессии пользователя
chat_id — идентификатор чата
context — контекст выполнения Telegram-обработчика
message — заголовок коммита
body — опциональное тело коммита
Возвращает
None
Исключения
Возможны ошибки при выполнении Git-команд или отправке сообщений

---
async def _handle_git_conflict(session, chat_id, context)
Обрабатывает состояние конфликтов при слиянии, уведомляя пользователя
Аргументы
session — объект сессии пользователя
chat_id — идентификатор чата
context — контекст выполнения Telegram-обработчика
Возвращает
None
Исключения
Возможны ошибки при отправке сообщений

---
async def _send_git_message(context, chat_id, session, text)
Отправляет сообщение с префиксом сессии через Telegram
Аргументы
context — контекст выполнения Telegram-обработчика
chat_id — идентификатор чата
session — объект сессии пользователя
text — текст сообщения
Возвращает
None
Исключения
Возможны ошибки при отправке сообщений

---
async def _send_git_output(context, chat_id, session, title, output)
Форматирует и отправляет вывод Git-команды как сообщение
Аргументы
context — контекст выполнения Telegram-обработчика
chat_id — идентификатор чата
session — объект сессии пользователя
title — заголовок вывода
output — вывод команды Git
Возвращает
None
Исключения
Возможны ошибки при отправке сообщений

---
async def _send_message(context, chat_id, text, reply_markup=None)
Универсальная отправка сообщения в чат
Аргументы
context — контекст выполнения Telegram-обработчика
chat_id — идентификатор чата
text — текст сообщения
reply_markup — опциональная клавиатура
Возвращает
None
Исключения
Возможны ошибки при отправке сообщений

---
async def _run_git(session, args)
Выполняет Git-команду в контексте сессии
Аргументы
session — объект сессии пользователя
args — список аргументов команды Git
Возвращает
Кортеж (код завершения, stdout)
Исключения
Возможны ошибки при запуске процесса

---
class GitCommandHandler
Обрабатывает входящие команды Git от пользователя через Telegram
Поля
git_pull_target — хранит целевую ветку для pull по chat_id
git_branch_menu — хранит список веток для выбора действия
git_pending_ref — хранит выбранную ветку перед подтверждением
pending_git_commit — хранит сессии ожидания ввода сообщения коммита
config — конфигурация приложения
Методы
_handle_git_pull_ff_only(context, chat_id, session, upstream) — выполняет fast-forward pull
_git_merge_or_rebase(session, chat_id, context, action, ref) — выполняет merge или rebase
_git_ahead_behind(session, ref) — определяет разницу в коммитах
_git_status_text(session) — возвращает текст статуса
_git_conflict_files(session) — возвращает список файлов с конфликтами
_git_commit_context(session) — собирает контекст для коммита
_build_commit_body(session) — формирует тело коммита
_execute_git_commit(session, chat_id, context, message, body) — выполняет коммит
_handle_git_conflict(session, chat_id, context) — обрабатывает конфликты
_send_git_message(context, chat_id, session, text) — отправляет сообщение с префиксом
_send_git_output(context, chat_id, session, title, output) — отправляет вывод команды
_send_message(context, chat_id, text, reply_markup) — отправляет сообщение
_run_git(session, args) — выполняет Git-команду
