# telegram_io

Thin wrapper around python-telegram-bot I/O with built-in retry logic for transient network errors.  
Handles message sending, document delivery, editing, and deletion with resilience to network issues.  
Business logic is delegated to higher-level components; this module focuses on transport reliability.

Ключевые структуры данных  
TelegramIO — Класс для надёжного взаимодействия с Telegram API с автоматическими повторными попытками

---
RecordMessageFn  
Тип функции для записи идентификаторов отправленных сообщений  
Аргументы  
chat_id — Идентификатор чата  
message_id — Идентификатор отправленного сообщения  
Возвращает  
None — Ничего не возвращает

---
async def send_message(self, context: Any, **kwargs)  
Отправляет сообщение в Telegram с повторными попытками при сетевых ошибках  
Аргументы  
context — Контекст выполнения (обычно из обработчика python-telegram-bot)  
kwargs — Параметры для send_message (например, chat_id, text)  
Возвращает  
Объект сообщения при успехе, None при неудаче  
Исключения  
Перехватывает NetworkError и TimedOut, логирует финальную ошибку

---
async def send_document(self, context: Any, **kwargs) -> bool  
Отправляет документ в Telegram с повторными попытками  
Аргументы  
context — Контекст выполнения  
kwargs — Параметры для send_document (например, chat_id, document)  
Возвращает  
True при успехе, False при неудаче  
Исключения  
Перехватывает NetworkError, TimedOut и другие исключения, логирует ошибки

---
async def delete_message(self, context: Any, chat_id: int, message_id: int) -> bool  
Удаляет сообщение в указанном чате  
Аргументы  
context — Контекст выполнения  
chat_id — Идентификатор чата  
message_id — Идентификатор удаляемого сообщения  
Возвращает  
True при успехе, False при неудаче  
Исключения  
Перехватывает все исключения, не прерывает выполнение

---
async def edit_message(self, context: Any, chat_id: int, message_id: int, text: str) -> bool  
Редактирует текст существующего сообщения  
Аргументы  
context — Контекст выполнения  
chat_id — Идентификатор чата  
message_id — Идентификатор редактируемого сообщения  
text — Новый текст сообщения  
Возвращает  
True при успехе, False при неудаче  
Исключения  
Перехватывает все исключения, не прерывает выполнение

---
class TelegramIO  
Тонкая обёртка для работы с Telegram API с поддержкой повторных попыток при сетевых ошибках  
Поля  
record_message — Опциональная функция для записи идентификаторов отправленных сообщений  
Методы  
async send_message(context: Any, **kwargs) — Отправка сообщения с повторными попытками  
async send_document(context: Any, **kwargs) — Отправка документа с повторными попытками  
async delete_message(context: Any, chat_id: int, message_id: int) — Удаление сообщения  
async edit_message(context: Any, chat_id: int, message_id: int, text: str) — Редактирование текста сообщения
