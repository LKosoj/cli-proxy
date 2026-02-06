# agent/plugins/reminders

Модуль предоставляет функциональность управления напоминаниями: создание, просмотр списка, удаление и отображение напоминаний через Telegram-интерфейс. Напоминания привязываются к пользователю и планируются во внутреннем хранилище задач с последующим уведомлением. Интегрирован с системой диалогов и обработки команд через callback-запросы.  
Ключевые структуры данных  
services — Словарь для хранения состояний: `user_tasks` (ID задач пользователя) и `scheduler_tasks` (детали напоминаний)  
scheduler_tasks — словарь всех активных задач планировщика, индексированных по ID напоминания  
user_tasks — словарь множеств ID напоминаний, привязанных к каждому пользователю

class RemindersTool
Класс для управления напоминаниями через Telegram-интерфейс
Поля
services — Словарь глобального состояния: user_tasks и scheduler_tasks
Методы
RemindersTool.get_source_name() -> str — Возвращает имя источника инструмента.
Возвращает
Имя источника — "Reminders"
---
RemindersTool.get_spec() -> ToolSpec — Возвращает спецификацию инструмента напоминаний с актуальным временем.
Возвращает
Объект ToolSpec с параметрами: action (set/list/delete), time, message, reminder_id
---
RemindersTool.get_menu_label() -> str — Возвращает метку для меню инструмента.
Возвращает
Строку "Напоминания"
---
RemindersTool.get_menu_actions() -> List[Dict[str, Any]] — Возвращает список действий, отображаемых в меню.
Возвращает
Список словарей с метками и действиями: "Список" → "list", "Создать" → "set"
---
RemindersTool.get_commands() -> List[Dict[str, Any]] — Возвращает команды, доступные через диалоговый интерфейс.
Возвращает
Список команд, сгенерированных через _dialog_callback_commands()
---
RemindersTool.dialog_steps() -> Dict[str, Callable] — Определяет шаги диалога для создания напоминания.
Возвращает
Словарь с шагом "wait_reminder_input", ведущим к _on_reminder_text
---
RemindersTool.callback_handlers() -> Dict[str, Callable] — Возвращает обработчики callback-действий.
Возвращает
Словарь действий: list, set, delete, view, close_menu → соответствующие методы
---
RemindersTool._get_user_chat(update: Update) -> Tuple[Optional[int], Optional[int]] — Извлекает user_id и chat_id из обновления.
Аргументы
update — объект обновления от Telegram
Возвращает
Кортеж (user_id, chat_id) или (None, None), если недоступны
---
RemindersTool._user_task_ids(user_id: int) -> set — Возвращает множество ID напоминаний пользователя.
Аргументы
user_id — уникальный идентификатор пользователя
Возвращает
Множество строк-идентификаторов напоминаний
---
RemindersTool._scheduler_tasks() -> Dict[str, Dict[str, Any]] — Возвращает глобальное хранилище задач планировщика.
Возвращает
Словарь напоминаний по ID: {id: {when: str, content: str, chat_id: int, task: asyncio.Task}}
---
RemindersTool._build_reminder_keyboard(user_id: int) -> List[list] — Формирует клавиатуру с активными напоминаниями пользователя.
Аргументы
user_id — идентификатор пользователя
Возвращает
Список кнопок в формате InlineKeyboardMarkup
---
RemindersTool._cb_list(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None — Обрабатывает запрос на отображение списка напоминаний.
Аргументы
update — входящее обновление от Telegram
context — контекст выполнения
payload — строка полезной нагрузки (игнорируется)
Исключения
Любые исключения подавляются при работе с сообщением
---
RemindersTool._cb_set(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None — Инициирует диалог создания напоминания.
Аргументы
update — входящее обновление
context — контекст выполнения
payload — полезная нагрузка (игнорируется)
Исключения
Любые исключения подавляются при отправке сообщения
---
RemindersTool._cb_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None — Удаляет напоминание по ID.
Аргументы
update — входящее обновление
context — контекст выполнения
payload — ID напоминания для удаления
Исключения
Любые исключения подавляются при редактировании сообщения
---
RemindersTool._cb_view(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None — Отображает содержимое напоминания во всплывающем уведомлении.
Аргументы
update — входящее обновление
context — контекст выполнения
payload — ID напоминания для просмотра
Исключения
Любые исключения подавляются при ответе на запрос
---
RemindersTool._cb_close_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None — Закрывает меню, удаляя сообщение.
Аргументы
update — входящее обновление
context — контекст выполнения
payload — полезная нагрузка (игнорируется)
Исключения
Любые исключения подавляются при удалении сообщения
---
RemindersTool._on_reminder_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None — Обрабатывает ввод текста напоминания в формате "YYYY-MM-DD HH:MM текст".
Аргументы
update — входящее сообщение с текстом напоминания
context — контекст выполнения
Исключения
Любые исключения подавляются при отправке ответа

---
async def handle_message(msg: Any, user_id: int, chat_id: int) -> None
Обрабатывает входящее сообщение для создания напоминания
Аргументы
msg — объект сообщения с методом reply_text
user_id — идентификатор пользователя
chat_id — идентификатор чата
Исключения
Любые исключения при отправке сообщения логируются, но не прерывают выполнение
---
async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]
Выполняет действие (set, list, delete) с напоминаниями через API
Аргументы
args — аргументы действия: action, time, message, reminder_id
ctx — контекст выполнения: session_id, chat_id, bot, context
Возвращает
Словарь с результатом операции: success, output или error
Исключения
Не выбрасывает исключения, все ошибки возвращаются в поле 'error'
