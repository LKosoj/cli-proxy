# agent/plugins/task_management

/**
 * @brief Модуль управления задачами для Telegram-бота с поддержкой создания, просмотра, обновления, удаления и отслеживания задач.
 *
 * Задачи хранятся в локальном JSON-файле, привязаны к пользователю и чату, поддерживают приоритеты, статусы, дедлайны и теги.
 * Реализован фоновый процесс проверки дедлайнов с уведомлениями о приближающихся и просроченных задачах.
 * Интеграция с диалоговой системой позволяет интерактивно добавлять задачи через шаги диалога.
 *
 * Ключевые структуры данных
 * _NotifyPolicy — Параметры периодической проверки дедлайнов и отправки уведомлений
 */

float _now_ts()
Возвращает текущую метку времени в секундах.
Возвращает
Текущее время как float (секунды с эпохи)

---
std::string _shared_root()
Определяет корневую директорию для общих данных агента.
Возвращает
Путь к директории _shared, основанной на переменной окружения или текущей директории

---
std::string _tasks_path()
Формирует путь к файлу хранения задач.
Возвращает
Полный путь к tasks.json

---
void _ensure_storage()
Создаёт директорию для хранения, если она не существует.

---
std::map<std::string, std::map<std::string, Any>> _load_all_tasks()
Загружает все задачи из JSON-файла.
Возвращает
Словарь всех задач, индексированный по user_id

---
void _save_all_tasks(const std::map<std::string, std::map<std::string, Any>>& data)
Сохраняет все задачи в JSON-файл с атомарной записью.
Аргументы
data — Данные задач для сохранения

---
std::pair<std::optional<int>, std::optional<std::string>> _parse_deadline(const std::optional<std::string>& deadline)
Парсит строку дедлайна в timestamp или возвращает ошибку.
Аргументы
deadline — Строка формата "YYYY-MM-DD HH:MM"
Возвращает
Кортеж из (timestamp, ошибка: str или None)

---
std::string _format_task_line(const std::map<std::string, Any>& task)
Форматирует задачу в краткую текстовую строку для отображения.
Аргументы
task — Объект задачи
Возвращает
Форматированная строка с заголовком, статусом, приоритетом и ID

---
std::string _human_status(const std::string& status)
Преобразует внутренний статус задачи в человекочитаемый вид.
Аргументы
status — Внутренний статус (pending, in_progress и т.д.)
Возвращает
Локализованное название статуса

---
std::string _next_status(const std::string& current)
Определяет следующий статус задачи по циклическому порядку.
Аргументы
current — Текущий статус задачи
Возвращает
Следующий статус в порядке workflow

---
class TaskManagementTool
Инструмент управления задачами с поддержкой диалогов и интерактивного меню в Telegram.
Поля
_policy — Политика уведомлений для отслеживания дедлайнов
Методы
dialog_steps() — Возвращает шаги диалога для добавления задачи
get_source_name() — Возвращает имя источника инструмента
get_spec() — Возвращает спецификацию инструмента для вызова ИИ
get_menu_label() — Возвращает метку для меню
get_menu_actions() — Возвращает действия, доступные в меню
get_commands() — Возвращает команды, поддерживаемые инструментом
callback_handlers() — Возвращает обработчики callback-запросов
execute(args: Dict[str, Any], ctx: Dict[str, Any]) — Выполняет действие с задачей (создание, список и т.д.)
_cb_start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) — Начинает диалог добавления задачи
_cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) — Обновляет список задач
_cb_view(update: Update, context: ContextTypes.DEFAULT_TYPE) — Отображает детали задачи
_cb_view_help(update: Update, context: ContextTypes.DEFAULT_TYPE) — Показывает справку по использованию
_cb_del(update: Update, context: ContextTypes.DEFAULT_TYPE) — Удаляет задачу
_cb_next(update: Update, context: ContextTypes.DEFAULT_TYPE) — Переходит к следующему статусу задачи
_on_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE) — Обрабатывает ввод текста при добавлении задачи

---
std::map<std::string, Any> add_task(const std::string& user_id, const std::string& title, const std::string& priority = "low", const std::optional<std::string>& description = std::nullopt, const std::optional<std::string>& deadline = std::nullopt)
Добавляет новую задачу для пользователя.
Аргументы
user_id — уникальный идентификатор пользователя.
title — заголовок задачи.
priority — приоритет: "high", "medium", "low".
description — необязательное описание задачи.
deadline — строка с дедлайном в формате "YYYY-MM-DD HH:MM", опционально.
Возвращает
Словарь с ключами "success" (bool) и "output" (str) или "error" (str) при неудаче.

---
std::map<std::string, Any> list_tasks(const std::string& user_id)
Возвращает отформатированный список задач пользователя, отсортированный по статусу, дедлайну и приоритету.
Аргументы
user_id — уникальный идентификатор пользователя.
Возвращает
Словарь с ключами "success" (bool) и "output" (str). При отсутствии задач — сообщение "Задач нет.".

---
std::map<std::string, Any> update_task(const std::string& user_id, const std::string& task_id, const std::map<std::string, Any>& args)
Обновляет поля указанной задачи: статус, приоритет, описание, дедлайн.
Аргументы
user_id — уникальный идентификатор пользователя.
task_id — идентификатор задачи.
args — словарь с полями для обновления (status, priority, description, deadline).
Возвращает
Словарь с ключами "success" (bool) и "output" (str) или "error" при неудаче.
Исключения
Может вернуть ошибку, если задача не найдена или дедлайн указан в неверном формате.

---
std::map<std::string, Any> delete_task(const std::string& user_id, const std::string& task_id)
Удаляет задачу по идентификатору.
Аргументы
user_id — уникальный идентификатор пользователя.
task_id — идентификатор задачи.
Возвращает
Словарь с ключами "success" (bool) и "output" (str) об успешном удалении или "error", если задача не найдена.

---
std::pair<std::string, std::optional<InlineKeyboardMarkup>> _build_tasks_menu(const std::string& user_id)
Формирует текст и inline-клавиатуру для отображения задач в интерфейсе.
Аргументы
user_id — уникальный идентификатор пользователя.
Возвращает
Кортеж из строки (текст меню) и объекта InlineKeyboardMarkup (или None, если клавиатура не нужна).

---
std::tuple<std::optional<std::string>, std::optional<std::string>, std::optional<std::string>, std::optional<std::string>> _parse_add_input(const std::string& text)
Парсит текстовое сообщение пользователя для извлечения данных новой задачи.
Аргументы
text — ввод пользователя (например, "high 2025-01-01 12:00 Созвон").
Возвращает
Кортеж: (priority, deadline, title, error), где error — строка ошибки или None при успехе.

---
void _cb_start_add(const Update& update, const ContextTypes::DEFAULT_TYPE& context, const std::optional<std::string>& payload = std::nullopt)
Обработчик колбэка для начала добавления задачи через кнопку.
Аргументы
update — объект обновления от Telegram.
context — контекст выполнения.
payload — строка полезной нагрузки (не используется).
Исключения
Не возвращает значения, но может выполнять отправку сообщений через bot API.

---
void _on_add_text(const Update& update, const ContextTypes::DEFAULT_TYPE& context)
Обработчик текстового ввода при добавлении задачи в диалоговом режиме.
Аргументы
update — объект обновления от Telegram.
context — контекст выполнения.
Исключения
Отправляет сообщение об ошибке при невалидном вводе, иначе сохраняет задачу и завершает диалог.

---
void run_task_deadline_checker(const Any& application, const Any& is_allowed_cb)
Асинхронный цикл проверки дедлайнов задач и отправки уведомлений.
Аргументы
application — Экземпляр приложения Telegram Bot
is_allowed_cb — Функция-коллбэк для проверки, разрешено ли отправлять сообщение в чат
Возвращает
None
Исключения
Любые исключения логируются, но не прерывают выполнение цикла
