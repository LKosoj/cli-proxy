# session_ui

Модуль предоставляет пользовательский интерфейс для управления сессиями через Telegram-бота. Он позволяет просматривать список сессий, выбирать активную, переименовывать, обновлять токен возобновления, а также получать статус и управлять очередью сессий. Взаимодействие осуществляется через inline-кнопки и обработку сообщений.

Ключевые структуры данных  
SessionUI — Класс для построения интерфейса управления сессиями в Telegram

---
class SessionUI  
Интерфейс управления сессиями через Telegram с поддержкой inline-меню и обработки ввода  
Поля  
config — Конфигурация приложения  
manager — Менеджер сессий  
_send_message — Асинхронная функция отправки сообщений в чат  
_format_ts — Функция форматирования временных меток  
_short_label — Функция усечения текста до заданной длины  
pending_session_rename — Словарь ожидания ввода нового имени сессии (chat_id → session_id)  
pending_session_resume — Словарь ожидания ввода нового resume-токена (chat_id → session_id)  
Методы  
build_sessions_menu() — Создаёт inline-клавиатуру со списком всех сессий  
handle_pending_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool — Обрабатывает текстовые сообщения от пользователя при ожидании ввода (переименование, resume)  
handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool — Обрабатывает нажатия на inline-кнопки меню сессий

---
build_sessions_menu()  
Создаёт клавиатуру с перечнем всех сессий для выбора  
Возвращает  
InlineKeyboardMarkup — Готовая разметка с кнопками сессий

---
handle_pending_message(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool  
Обрабатывает текстовые сообщения от пользователя в режиме ожидания ввода (переименование или resume)  
Аргументы  
chat_id — Идентификатор чата  
text — Введённый пользователем текст  
context — Контекст выполнения Telegram-бота  
Возвращает  
True, если сообщение было обработано как ожидающий ввод; иначе False

---
handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool  
Обрабатывает нажатия на inline-кнопки в меню сессий  
Аргументы  
query — Объект callback-запроса от Telegram  
chat_id — Идентификатор чата  
context — Контекст выполнения Telegram-бота  
Возвращает  
True, если callback был распознан и обработан; иначе False

---
async def get_state(state_path: str, tool_name: str, workdir: str) -> Optional[SessionState]  
Возвращает состояние сессии по пути, имени инструмента и рабочему каталогу  
Аргументы  
state_path — путь к файлу или директории хранения состояний  
tool_name — имя инструмента, связанного с сессией  
workdir — рабочий каталог сессии  
Возвращает  
Объект SessionState при успехе, иначе None

---
async def _send_message(self, context: Context, chat_id: int, text: str) -> None  
Отправляет текстовое сообщение пользователю через Telegram-бота  
Аргументы  
context — контекст выполнения бота  
chat_id — идентификатор чата получателя  
text — текст сообщения для отправки

---
async def _format_ts(self, timestamp: float) -> str  
Форматирует временную метку в читаемую строку  
Аргументы  
timestamp — временная метка в формате Unix time  
Возвращает  
Строка с датой и временем в локальном формате

---
class SessionManager  
Управляет жизненным циклом сессий: создание, хранение, закрытие и сохранение на диск  
Методы  
get(session_id: str) — Возвращает сессию по идентификатору или None, если не найдена  
close(session_id: str) — Закрывает сессию, удаляет её из менеджера и возвращает успех операции  
_persist_sessions() — Сохраняет текущее состояние всех сессий на диск

---
async def handle_callback_query(self, update: Update, context: Context) -> bool  
Обрабатывает входящие callback-запросы от пользовательского интерфейса  
Аргументы  
update — объект обновления от Telegram с данными callback  
context — контекст выполнения бота  
Возвращает  
True, если запрос был обработан, иначе False  
Исключения  
Может выбрасывать исключения при ошибках взаимодействия с Telegram API
