# git_ops

Модуль `GitOps` предоставляет инструменты для управления Git-репозиторием через Telegram-интерфейс. Интегрируется с активными сессиями, обеспечивает безопасное выполнение Git-команд и обработку конфликтов. Поддерживает аутентификацию через GitHub-токен с использованием временного скрипта `GIT_ASKPASS`. Все действия инициируются через callback-кнопки, предоставляя удобный интерактивный интерфейс.

Ключевые структуры данных  
git_branch_menu — словарь, сопоставляющий chat_id с доступными ветками для выбора  
git_pending_ref — хранит выбранную ветку для последующих операций (например, merge/rebase)  
git_pull_target — целевая ветка для операции pull  
pending_git_commit — сообщение для коммита, накапливаемое при интерактивном вводе  
git_env — окружение с настройками и токеном для бесшовного выполнения Git-команд

GitOps.__init__(self, config, manager: SessionManager, send_message, send_document, short_label, handle_cli_input) -> None  
Инициализирует компонент управления Git-операциями  
Аргументы  
config — конфигурация приложения с токенами и настройками  
manager — менеджер активных сессий  
send_message — асинхронная функция отправки текстовых сообщений  
send_document — асинхронная функция отправки файлов  
short_label — функция сокращённого отображения путей или имён  
handle_cli_input — функция для передачи команд в CLI-сессию

---
GitOps._ensure_git_askpass(self) -> Optional[str]  
Создаёт временный скрипт для аутентификации в Git через токен  
Возвращает  
Путь к скрипту, если токен задан; иначе — None  
Исключения  
Может выбросить исключение при ошибках создания файла

---
GitOps.git_env(self) -> dict  
Формирует окружение для выполнения Git-команд с безопасной аутентификацией  
Возвращает  
Словарь переменных окружения, включая GIT_ASKPASS и токен

---
GitOps.build_git_keyboard(self) -> InlineKeyboardMarkup  
Создаёт основную клавиатуру с Git-операциями  
Возвращает  
Объект InlineKeyboardMarkup с кнопками для статуса, пулла, коммита и других операций

---
GitOps._build_git_branches_keyboard(self, chat_id: int, action: str) -> InlineKeyboardMarkup  
Генерирует клавиатуру для выбора ветки из списка  
Аргументы  
chat_id — идентификатор чата для получения списка веток  
action — тип действия (например, "merge" или "rebase")  
Возвращает  
Клавиатура с кнопками для каждой ветки и кнопкой отмены

---
GitOps._build_git_pull_keyboard(self, ref: str) -> InlineKeyboardMarkup  
Создаёт клавиатуру выбора стратегии pull: merge или rebase  
Аргументы  
ref — имя ветки, с которой выполняется pull  
Возвращает  
Клавиатура с кнопками "Merge", "Rebase" и "Отмена"

---
GitOps._build_git_confirm_keyboard(self, action: str, ref: str) -> InlineKeyboardMarkup  
Формирует клавиатуру подтверждения операции merge или rebase  
Аргументы  
action — тип операции ("merge" или "rebase")  
ref — целевая ветка  
Возвращает  
Клавиатура с подтверждающей кнопкой и отменой

---
GitOps._build_git_conflict_keyboard(self) -> InlineKeyboardMarkup  
Создаёт клавиатуру для разрешения конфликтов при слиянии  
Возвращает  
Клавиатура с действиями: diff, abort, continue, вызов агента

---
GitOps._ensure_git_state(self, session: Session) -> None  
Инициализирует Git-состояние сессии, если оно отсутствует  
Аргументы  
session — активная сессия, в которой проверяется состояние

---
GitOps.ensure_git_session(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[Session]  
Проверяет наличие активной сессии и её Git-состояние  
Аргументы  
chat_id — идентификатор чата  
context — контекст выполнения Telegram-бота  
Возвращает  
Активную сессию, если доступна; иначе — None  
Исключения  
Отправляет сообщение об ошибке, если сессия неактивна

---
GitOps._session_label(self, session: Session) -> str  
Формирует краткую строковую метку для сессии  
Аргументы  
session — сессия для отображения  
Возвращает  
Человекочитаемая строка с информацией о сессии

---
GitOps._send_git_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: Session, text: str) -> None  
Отправляет сообщение с префиксом сессии  
Аргументы  
context — контекст бота  
chat_id — получатель сообщения  
session — активная сессия  
text — текст сообщения

---
GitOps.ensure_git_repo(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool  
Проверяет, является ли рабочая директория сессии Git-репозиторием  
Аргументы  
session — проверяемая сессия  
chat_id — идентификатор чата  
context — контекст бота  
Возвращает  
True, если это репозиторий; иначе — False

---
GitOps.ensure_git_not_busy(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool  
Проверяет, не выполняется ли в данный момент Git-операция  
Аргументы  
session — сессия для проверки  
chat_id — идентификатор чата  
context — контекст бота  
Возвращает  
True, если сессия свободна; иначе — False

---
GitOps._run_git(self, session: Session, args: list[str]) -> tuple[int, str]  
Асинхронно выполняет Git-команду в рабочей директории сессии  
Аргументы  
session — сессия с рабочей директорией  
args — аргументы команды git (например, ["status"])  
Возвращает  
Кортеж из кода возврата и вывода команды

---
async def _run_git(session: Session, args: list[str]) -> tuple[int, str]  
Выполняет Git-команду с указанными аргументами и возвращает код возврата и вывод.  
Аргументы  
session — сессия, содержащая контекст выполнения (например, рабочую директорию)  
args — аргументы команды Git  
Возвращает  
Кортеж из кода возврата (0 — успех) и декодированного вывода команды  
Исключения  
Может выбрасывать исключения при ошибках выполнения процесса (например, отсутствие git)

---
async def _git_current_branch(session: Session) -> Optional[str]  
Возвращает имя текущей ветки Git или None при ошибке.  
Аргументы  
session — активная сессия  
Возвращает  
Имя ветки (например, "main") или None, если не удалось определить

---
async def _git_upstream(session: Session) -> Optional[str]  
Определяет имя upstream-ветки для текущей ветки.  
Аргументы  
session — активная сессия  
Возвращает  
Имя upstream (например, "origin/main") или None, если не задан

---
async def _git_ref_exists(session: Session, ref: str) -> bool  
Проверяет, существует ли указанный Git-референс.  
Аргументы  
session — активная сессия  
ref — имя референса (например, "origin/main")  
Возвращает  
True, если референс существует, иначе False

---
async def _git_default_remote(session: Session) -> Optional[str]  
Определяет ветку по умолчанию в удалённом репозитории (origin/HEAD).  
Аргументы  
session — активная сессия  
Возвращает  
Имя ветки по умолчанию (например, "origin/main") или None

---
async def _git_ahead_behind(session: Session, ref: str) -> Optional[tuple[int, int]]  
Возвращает количество коммитов, на которые HEAD опережает и отстаёт от указанной ветки.  
Аргументы  
session — активная сессия  
ref — референс для сравнения (например, "origin/main")  
Возвращает  
Кортеж (ahead, behind) или None при ошибке

---
async def _git_in_progress(session: Session) -> Optional[str]  
Определяет, выполняется ли в данный момент rebase или merge.  
Аргументы  
session — активная сессия  
Возвращает  
Строка "rebase" или "merge", если процесс идёт; иначе None

---
def _git_set_conflict(session: Session, files: list[str], kind: Optional[str]) -> None  
Помечает сессию как находящуюся в состоянии конфликта.  
Аргументы  
session — сессия для обновления  
files — список конфликтующих файлов  
kind — тип конфликта ("rebase" или "merge")

---
def _git_clear_conflict(session: Session) -> None  
Снимает флаг конфликта в сессии.  
Аргументы  
session — сессия для сброса состояния конфликта

---
async def _git_conflict_files(session: Session) -> list[str]  
Получает список файлов с конфликтами слияния.  
Аргументы  
session — активная сессия  
Возвращает  
Список имён файлов с конфликтами; обновляет состояние сессии через _git_set_conflict или _git_clear_conflict

---
async def _git_status_text(session: Session) -> str  
Формирует человекочитаемое описание состояния Git-репозитория.  
Аргументы  
session — активная сессия  
Возвращает  
Многострочная строка с информацией о ветке, состоянии, upstream и конфликтах

---
async def _send_git_help(session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None  
Отправляет пользователю справку по Git из файла git.md в виде документа.  
Аргументы  
session — сессия пользователя  
chat_id — идентификатор чата для отправки  
context — контекст Telegram-бота  
Исключения  
Логирует исключения при работе с файлами, но не прерывает выполнение

---
async def _git_commit_context(session: Session) -> Optional[str]  
Собирает контекст изменений в репозитории для формирования коммита.  
Аргументы  
session — активная сессия  
Возвращает  
Строка с выводом status --porcelain, diff --stat и diff; None при ошибке

---
def _sanitize_commit_message(message: str, max_len: int = 100) -> str  
Очищает и обрезает первую строку сообщения коммита.  
Аргументы  
message — исходное сообщение  
max_len — максимальная длина (по умолчанию 100)  
Возвращает  
Очищенная и усечённая строка

---
def _sanitize_commit_body(body: str, max_len: int = 2000) -> str  
Очищает и обрезает тело коммита.  
Аргументы  
body — исходное тело сообщения  
max_len — максимальная длина (по умолчанию 2000)  
Возвращает  
Очищенная и усечённая строка

---
async def _build_commit_body(session: Session) -> Optional[str]  
Формирует тело коммита на основе diff --stat и status --porcelain.  
Аргументы  
session — активная сессия  
Возвращает  
Форматированная строка с описанием изменений или None, если изменений нет

---
GitOps._output(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: Session, title: str, output: str) -> None  
Отправляет отформатированное сообщение с результатом Git-операции в чат  
Аргументы  
context — контекст выполнения Telegram-бота  
chat_id — идентификатор чата для отправки сообщения  
session — активная сессия пользователя с информацией о репозитории  
title — заголовок сообщения (например, "Git commit")  
output — вывод команды, который нужно отправить  
Исключения  
Возможны исключения при отправке сообщения через Telegram API

---
GitOps._execute_git_commit(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message: str, body: Optional[str] = None) -> None  
Выполняет добавление всех изменений и коммит с указанным сообщением и опциональным телом  
Аргументы  
session — сессия с репозиторием  
chat_id — идентификатор чата для уведомлений  
context — контекст Telegram-бота  
message — основное сообщение коммита  
body — дополнительное тело коммита (опционально)  
Исключения  
Возможны исключения при выполнении Git-команд или отправке сообщений

---
GitOps._handle_git_conflict(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает обнаруженные конфликты слияния, отправляя уведомление с перечнем затронутых файлов  
Аргументы  
session — сессия с репозиторием  
chat_id — идентификатор чата  
context — контекст Telegram-бота  
Исключения  
Возможны исключения при отправке сообщения

---
GitOps._git_merge_or_rebase(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE, action: str, ref: str) -> None  
Выполняет операцию слияния (merge) или перебазирования (rebase) с указанной веткой или ссылкой  
Аргументы  
session — сессия с репозиторием  
chat_id — идентификатор чата  
context — контекст Telegram-бота  
action — действие: "merge" или "rebase"  
ref — ссылка на ветку или коммит  
Исключения  
Возможны исключения при выполнении Git-команд или отправке сообщений

---
GitOps.handle_pending_commit_message(self, chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool  
Обрабатывает ввод пользователя для сообщения коммита, запускает коммит или отменяет операцию  
Аргументы  
chat_id — идентификатор чата  
text — введённый пользователем текст  
context — контекст Telegram-бота  
Возвращает  
True, если сообщение было обработано, иначе False  
Исключения  
Возможны исключения при работе с сессиями или отправке сообщений

---
GitOps.handle_callback(self, query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool  
Обрабатывает callback-запросы от inline-кнопок, запуская соответствующие Git-операции  
Аргументы  
query — объект callback-запроса от Telegram  
chat_id — идентификатор чата  
context — контекст Telegram-бота  
Возвращает  
True, если запрос был обработан, иначе False  
Исключения  
Возможны исключения при выполнении Git-команд или отправке сообщений
