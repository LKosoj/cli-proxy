# mtproto_ui

Модуль предоставляет интерфейс для взаимодействия с MTProto через Telegram-бота с использованием библиотеки Telethon. Позволяет выбирать целевые чаты из конфигурации, вводить сообщения и отправлять их через MTProto-сессию. Поддерживает отправку текстовых сообщений и файлов, а также обработку пользовательских действий через inline-кнопки. Работа с MTProto инкапсулирована, включая ленивую инициализацию клиента и проверку авторизации.

Ключевые структуры данных  
MTProtoUI — Основной класс для управления интерфейсом взаимодействия с MTProto, включая меню, обработку ввода и отправку сообщений

class MTProtoUI  
Основной класс для управления интерфейсом MTProto: меню, ввод заданий, отправка сообщений.  
Поля  
config — Конфигурация приложения, содержащая настройки mtproto  
_send_message — Асинхронная функция для отправки сообщений через бота  
pending_target — Словарь: chat_id → индекс выбранной цели  
pending_task — Словарь: chat_id → текст сообщения, вводимый в два этапа  
_client — Активный экземпляр TelegramClient (Telethon), инициализируется по требованию  
_client_lock — Асинхронная блокировка для потокобезопасной инициализации клиента  
Методы  
__init__(config, send_message) — Инициализирует UI с конфигурацией и функцией отправки сообщений  
build_menu() — Возвращает разметку inline-клавиатуры для выбора цели из списка  
request_task(chat_id: int, context: ContextTypes.DEFAULT_TYPE) — Запрашивает у пользователя ввод задания  
_get_client() -> tuple[Optional["TelegramClient"], Optional[str]] — Возвращает активный клиент Telethon или ошибку  
show_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE) — Отображает меню выбора цели  
handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool — Обрабатывает нажатия на inline-кнопки  
consume_pending(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict] — Обрабатывает введённый текст как часть или всё сообщение  
send_text(peer, text: str) -> Optional[str] — Отправляет текстовое сообщение указанному получателю  
send_file(peer, path: str) -> Optional[str] — Отправляет файл по пути указанному получателю

---
build_menu()  
Возвращает inline-клавиатуру с доступными целями из конфигурации и кнопками управления.  
Возвращает  
InlineKeyboardMarkup — Разметка клавиатуры с целями и кнопками "Отмена", "Закрыть меню"

---
request_task(chat_id: int, context: ContextTypes.DEFAULT_TYPE)  
Отправляет сообщение с запросом ввода задания и кнопкой отмены.  
Аргументы  
chat_id — Идентификатор чата для отправки  
context — Контекст выполнения Telegram-бота

---
_get_client() -> tuple[Optional["TelegramClient"], Optional[str]]  
Возвращает инициализированный клиент Telethon или сообщение об ошибке.  
Возвращает  
Кортеж: (активный клиент или None, сообщение об ошибке или None)  
Исключения  
Любые исключения при подключении перехватываются и возвращаются как строка ошибки

---
show_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE)  
Отображает меню выбора цели для отправки сообщения.  
Аргументы  
chat_id — Идентификатор чата  
context — Контекст выполнения Telegram-бота

---
handle_callback(query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool  
Обрабатывает callback-запросы от inline-кнопок (выбор цели, отмена, закрытие).  
Аргументы  
query — Объект callback-запроса  
chat_id — Идентификатор чата  
context — Контекст выполнения  
Возвращает  
True, если запрос был обработан, иначе False

---
consume_pending(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]  
Обрабатывает введённый пользователем текст как сообщение для отправки.  
Аргументы  
chat_id — Идентификатор чата  
text — Введённый текст  
context — Контекст выполнения  
Возвращает  
Словарь с полями peer, title, message при успешной обработке; None, если нет ожидаемого ввода

---
send_text(peer, text: str) -> Optional[str]  
Отправляет текстовое сообщение через MTProto-клиент.  
Аргументы  
peer — Идентификатор получателя (чат, пользователь и т.д.)  
text — Текст сообщения  
Возвращает  
None при успехе, строку с описанием ошибки при неудаче

---
send_file(peer, path: str) -> Optional[str]  
Отправляет файл через MTProto-клиент.  
Аргументы  
peer — Идентификатор получателя  
path — Путь к файлу на диске  
Возвращает  
None при успехе, строку с описанием ошибки при неудаче
