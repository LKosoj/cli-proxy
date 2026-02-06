# session_ui

Модуль предоставляет пользовательский интерфейс для управления сессиями через Telegram-бота. Он позволяет просматривать список сессий, выбирать активную, переименовывать, обновлять токен возобновления, а также получать статус и управлять очередью. Взаимодействие осуществляется через inline-кнопки и текстовые сообщения.

Ключевые структуры данных  
SessionUI — Класс для управления интерфейсом сессий в Telegram, включая построение меню и обработку действий

---
class SessionUI  
Интерфейс управления сессиями через Telegram с поддержкой inline-меню и обработки ввода.  
Поля  
config — Конфигурация приложения.  
manager — Менеджер сессий.  
_send_message — Функция отправки сообщений через Telegram.  
_format_ts — Функция форматирования временных меток.  
_short_label — Функция усечения текста до заданной длины.  
_on_close — Опциональный колбэк, вызываемый при закрытии сессии.  
_on_before_close — Опциональный асинхронный колбэк перед закрытием сессии.  
pending_session_rename — Словарь: chat_id → session_id для ожидания ввода нового имени сессии.  
pending_session_resume — Словарь: chat_id → session_id для ожидания ввода нового resume-токена.  
Методы  
__init__(config, manager, send_message, format_ts, short_label, on_close: Optional[Callable[[str], None]] = None, on_before_close: Optional[Callable[[str, int, ContextTypes.DEFAULT_TYPE], None]] = None) — Инициализирует интерфейс управления сессиями.  
build_sessions_menu() -> InlineKeyboardMarkup — Возвращает inline-клавиатуру со списком всех сессий.  
handle_pending_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool — Обрабатывает текстовые сообщения от пользователей, ожидающие ввода (переименование, resume).  
handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool — Обрабатывает нажатия на inline-кнопки, связанные с сессиями.

---
build_sessions_menu()  
Возвращает inline-клавиатуру с перечнем всех доступных сессий.  
Возвращает  
InlineKeyboardMarkup — Клавиатура с кнопками для выбора сессии.

---
handle_pending_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool  
Обрабатывает текстовые сообщения от пользователя в контексте ожидающих действий (переименование, обновление resume).  
Аргументы  
chat_id — Идентификатор чата.  
text — Введённый пользователем текст.  
context — Контекст выполнения Telegram-бота.  
Возвращает  
True, если сообщение было обработано как ожидающее действие; иначе False.

---
handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool  
Обрабатывает нажатие на inline-кнопки, связанные с управлением сессиями.  
Аргументы  
query — Объект callback-запроса от Telegram.  
chat_id — Идентификатор чата.  
context — Контекст выполнения Telegram-бота.  
Возвращает  
True, если callback был распознан и обработан; иначе False.

---
async def query.edit_message_text(text: str) -> bool  
Обновляет текст сообщения в интерфейсе Telegram.  
Аргументы  
text — отображаемый текст сообщения.  
Возвращает  
True при успешном обновлении.  
Исключения  
Возможны исключения Telegram API при недопустимых запросах.

---
async def query.answer() -> None  
Подтверждает получение callback-запроса без визуальных изменений.  
Возвращает  
None.  
Исключения  
Возможны исключения Telegram API при недопустимых запросах.

---
async def self._send_message(context: Context, chat_id: int, text: str) -> None  
Отправляет новое сообщение пользователю.  
Аргументы  
context — контекст выполнения Telegram-бота.  
chat_id — идентификатор чата получателя.  
text — текст сообщения.  
Возвращает  
None.  
Исключения  
Возможны исключения Telegram API при ошибках отправки.

---
def self.manager.get(session_id: str) -> Session | None  
Возвращает сессию по её идентификатору.  
Аргументы  
session_id — уникальный идентификатор сессии.  
Возвращает  
Объект сессии или None, если не найден.

---
def self.manager.close(session_id: str) -> bool  
Закрывает и удаляет сессию.  
Аргументы  
session_id — идентификатор сессии.  
Возвращает  
True при успешном закрытии, иначе False.

---
def self.manager._persist_sessions() -> None  
Сохраняет текущее состояние всех сессий на диск.  
Возвращает  
None.

---
def get_state(state_path: str, tool_name: str, workdir: str) -> State | None  
Загружает состояние выполнения инструмента из файловой системы.  
Аргументы  
state_path — путь к каталогу состояний.  
tool_name — имя инструмента.  
workdir — рабочий каталог сессии.  
Возвращает  
Объект состояния или None, если не найдено.

---
def self._format_ts(timestamp: float) -> str  
Форматирует временную метку в человекочитаемую строку.  
Аргументы  
timestamp — время в формате Unix timestamp.  
Возвращает  
Строка в формате "ДД.ММ.ГГГГ ЧЧ:ММ:СС".

---
class Session  
Хранит состояние активной сессии исполнения инструмента.  
Поля  
session_id — уникальный идентификатор сессии.  
tool — имя инструмента.  
workdir — рабочий каталог.  
resume_token — токен для возобновления выполнения.  
queue — очередь сообщений на обработку.  
updated_at — временная метка последнего обновления.  
Методы  
close() — завершает сессию и освобождает ресурсы.

---
class SessionManager  
Управляет жизненным циклом сессий: создание, получение, закрытие, сохранение.  
Методы  
get(session_id: str) -> Session | None — возвращает сессию по ID.  
close(session_id: str) -> bool — закрывает сессию.  
_persist_sessions() — сохраняет все сессии на диск.
