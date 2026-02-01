# bot

BotApp — основной класс приложения Telegram-бота для управления сессиями выполнения инструментов, взаимодействия с Git, MTProto, отображения состояний и файлов.  
Модуль координирует работу компонентов: сессий, интерфейсов, метрик, конфигурации и внешних систем (MCP, Git).  
Обрабатывает команды, входящие сообщения, колбэки и поддерживает буферизацию вывода для эффективной отправки в Telegram.

Ключевые структуры данных  
PendingInput — хранит временные данные о входящем вводе от пользователя (сессия, текст, назначение, путь к изображению)

---
BotApp.__init__(config: AppConfig)  
Инициализирует экземпляр бота с заданной конфигурацией, настраивает логирование и внутренние состояния.  
Аргументы  
config — конфигурация приложения типа AppConfig

---
BotApp.is_allowed(chat_id: int) -> bool  
Проверяет, разрешён ли доступ для указанного чата.  
Аргументы  
chat_id — идентификатор чата  
Возвращает  
True, если чат в белом списке; иначе False

---
BotApp._setup_logging()  
Настраивает ротацию логов по времени (ежедневно в 03:00 UTC), удаляет предыдущие архивы и форматирует вывод.

---
BotApp._format_ts(ts: float) -> str  
Форматирует временную метку в человекочитаемую строку.  
Аргументы  
ts — временная метка в формате Unix timestamp  
Возвращает  
Отформатированная строка вида "ГГГГ-ММ-ДД ЧЧ:ММ:СС" или "нет", если ts пуст

---
BotApp._short_label(text: str, max_len: int = 40) -> str  
Обрезает текст до указанной длины, добавляя многоточие при необходимости.  
Аргументы  
text — исходный текст  
max_len — максимальная длина (по умолчанию 40)  
Возвращает  
Обрезанная строка

---
BotApp._tool_exec(tool: ToolConfig) -> Optional[str]  
Возвращает исполняемый бинарный файл для инструмента (первый из cmd, headless_cmd, interactive_cmd).  
Аргументы  
tool — конфигурация инструмента типа ToolConfig  
Возвращает  
Имя исполняемого файла или None, если не найдено

---
BotApp._is_tool_available(name: str) -> bool  
Проверяет, доступен ли инструмент в системе (существует ли исполняемый файл).  
Аргументы  
name — имя инструмента  
Возвращает  
True, если инструмент доступен; иначе False

---
BotApp._available_tools() -> list[str]  
Возвращает список имён доступных в системе инструментов.  
Возвращает  
Список строк — имён доступных инструментов

---
BotApp._expected_tools() -> str  
Возвращает отсортированную строку с ожидаемыми именами инструментов из конфигурации.  
Возвращает  
Строка с перечислением имён инструментов

---
BotApp._send_message(context: ContextTypes.DEFAULT_TYPE, **kwargs)  
Отправляет сообщение в Telegram с повторными попытками при сетевых ошибках.  
Аргументы  
context — контекст Telegram-бота  
kwargs — параметры для send_message (chat_id, text и др.)  
Исключения  
Повторяет до 5 раз при NetworkError или TimedOut, затем выводит сообщение об ошибке

---
BotApp._send_document(context: ContextTypes.DEFAULT_TYPE, **kwargs)  
Отправляет документ в Telegram с повторными попытками при сетевых ошибках.  
Аргументы  
context — контекст Telegram-бота  
kwargs — параметры для send_document (chat_id, document и др.)  
Исключения  
Повторяет до 5 раз при NetworkError или TimedOut, затем выводит сообщение об ошибке

---
BotApp._build_state_keyboard(chat_id: int) -> InlineKeyboardMarkup  
Создаёт клавиатуру для выбора сохранённого состояния с пагинацией.  
Аргументы  
chat_id — идентификатор чата  
Возвращает  
Объект InlineKeyboardMarkup с кнопками состояний и навигацией

---
BotApp.send_output(session: Session, dest: dict, output: str, context: ContextTypes.DEFAULT_TYPE, preview: str = None, summary_source: str = None) -> None  
Отправляет вывод сессии в Telegram, с генерацией краткого резюме или превью.  
Аргументы  
session — активная сессия  
dest — словарь с параметрами назначения (chat_id, message_thread_id)  
output — текст вывода (возможно, с ANSI-разметкой)  
context — контекст Telegram-бота  
preview — опциональный текст предварительного анонса  
summary_source — источник информации для анонса (например, имя инструмента)  
Исключения  
Любые ошибки при генерации резюме обрабатываются внутри, не прерывают выполнение

---
async def send_output(self, session: Session, dest: dict, output: str, context: ContextTypes.DEFAULT_TYPE, preview: str = None, summary_source: str = None) -> None  
Отправляет результат выполнения сессии в указанный канал (чат Telegram или MTProto) с оформлением заголовка и файлом вывода  
Аргументы  
session — активная сессия, генерирующая вывод  
dest — словарь с параметрами назначения (тип, chat_id, peer и др.)  
output — текстовый вывод для отправки  
context — контекст выполнения Telegram-бота  
preview — опциональный текст предварительного анонса  
summary_source — источник информации для анонса (например, имя инструмента)  
Возвращает  
Ничего не возвращает  
Исключения  
Любые исключения при отправке сообщений или удалении временных файлов перехватываются и не прерывают выполнение

---
async def run_prompt(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None  
Выполняет пользовательский запрос в рамках сессии, обрабатывает вывод или ошибку, отправляет результат и обрабатывает очередь  
Аргументы  
session — сессия, в которой выполняется запрос  
prompt — текст запроса от пользователя  
dest — параметры назначения для отправки результата  
context — контекст выполнения Telegram-бота  
Возвращает  
Ничего не возвращает  
Исключения  
Перехватывает все исключения, логгирует их и отправляет сообщение об ошибке пользователю

---
async def ensure_active_session(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[Session]  
Проверяет наличие активной сессии, предлагает восстановить её при необходимости  
Аргументы  
chat_id — идентификатор чата пользователя  
context — контекст выполнения Telegram-бота  
Возвращает  
Активную сессию, если она есть; None, если сессия отсутствует и не может быть восстановлена  
Исключения  
Нет явных исключений; ошибки при загрузке состояния логгируются, но не прерывают выполнение

---
async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает входящее сообщение от пользователя: команды, создание каталогов, ввод путей, продолжение диалогов  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения Telegram-бота  
Возвращает  
Ничего не возвращает  
Исключения  
Нет явных исключений; все ошибки обрабатываются внутри метода с отправкой пользователю сообщения об ошибке

---
async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает текстовые сообщения от пользователя, включая создание сессий, git clone, команды и буферизацию ввода  
Аргументы  
update — объект обновления от Telegram с информацией о сообщении  
context — контекст выполнения бота, предоставляемый python-telegram-bot  
Исключения  
Не выбрасывает явно задокументированные исключения

---
async def on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Отвечает пользователю при вводе неизвестной команды  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения бота  
Исключения  
Не выбрасывает явно задокументированные исключения

---
async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает входящие документы: загружает, проверяет тип и размер, передаёт в сессию или обрабатывает как изображение  
Аргументы  
update — объект обновления с прикреплённым документом  
context — контекст выполнения бота  
Исключения  
Не выбрасывает явно задокументированные исключения

---
async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает входящие фотографии: выбирает наибольшее изображение, проверяет размер, загружает и передаёт в сессию  
Аргументы  
update — объект обновления с прикреплённым фото  
context — контекст выполнения бота  
Исключения  
Не выбрасывает явно задокументированные исключения

---
async def _handle_image_bytes(self, session: Session, data: bytearray, filename: str, caption: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает байтовое представление изображения: сохраняет, отправляет в инструмент или возвращает ошибку, если инструмент не поддерживает изображения  
Аргументы  
session — активная сессия, в которую передаётся изображение  
data — байтовые данные изображения  
filename — имя файла изображения  
caption — подпись к изображению (опционально)  
chat_id — идентификатор чата для отправки ответов  
context — контекст выполнения бота  
Исключения  
Не выбрасывает явно задокументированные исключения

---
async def _handle_image_input(session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE, filename: str, data: bytes, caption: str) -> None  
Обрабатывает входящее изображение: сохраняет во временной директории и передаёт в CLI с промптом.  
Аргументы  
session — активная сессия пользователя  
chat_id — идентификатор чата для ответа  
context — контекст выполнения Telegram-бота  
filename — исходное имя файла изображения  
data — бинарные данные изображения  
caption — текстовое описание изображения (подпись)  
Исключения  
Возбуждает исключения при ошибках записи файла, но перехватывает их внутренне

---
def _cleanup_image_dir(img_dir: str) -> None  
Удаляет файлы в указанной директории, старше 24 часов.  
Аргументы  
img_dir — путь к директории с временными изображениями

---
async def _dispatch_mtproto_task(chat_id: int, payload: dict, context: ContextTypes.DEFAULT_TYPE) -> None  
Инициирует выполнение задачи через MTProto: формирует путь к файлу и промпт, отправляет на обработку.  
Аргументы  
chat_id — идентификатор чата  
payload — данные задачи (сообщение, получатель и др.)  
context — контекст выполнения Telegram-бота

---
async def _handle_cli_input(session: Session, text: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, dest: Optional[dict] = None, image_path: Optional[str] = None) -> None  
Передаёт текстовый ввод в CLI-сессию, ставит в очередь при занятой сессии.  
Аргументы  
session — активная сессия  
text — текст команды или запроса  
chat_id — идентификатор чата  
context — контекст выполнения бота  
dest — назначение результата (по умолчанию — Telegram)  
image_path — путь к изображению, если оно прикреплено  
Исключения  
Не выбрасывает исключения напрямую

---
async def _buffer_or_send(session: Session, text: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None  
Буферизует сообщение, если оно слишком длинное или уже есть буфер; иначе отправляет сразу.  
Аргументы  
session — активная сессия  
text — текст для отправки  
chat_id — идентификатор чата  
context — контекст выполнения бота

---
async def _schedule_flush(chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE) -> None  
Планирует сброс буфера сообщений через задержку, отменяя предыдущую задачу.  
Аргументы  
chat_id — идентификатор чата  
session — активная сессия  
context — контекст выполнения бота

---
async def _flush_after_delay(chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE) -> None  
Выполняет задержку и сбрасывает буфер сообщений; прерывается при отмене задачи.  
Аргументы  
chat_id — идентификатор чата  
session — активная сессия  
context — контекст выполнения бота  
Исключения  
asyncio.CancelledError — игнорируется при отмене задачи

---
async def _flush_buffer(chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE) -> None  
Отправляет накопленные в буфере сообщения как единый блок.  
Аргументы  
chat_id — идентификатор чата  
session — активная сессия  
context — контекст выполнения бота

---
def _mtproto_output_path(workdir: str) -> str  
Формирует уникальный путь для файла результата MTProto-задачи, очищает старые файлы.  
Аргументы  
workdir — рабочая директория сессии  
Возвращает  
Полный путь к файлу результата в формате result_YYYYMMDD_HHMMSS.md

---
def _mtproto_prompt(text: str, file_path: str) -> str  
Формирует промпт для CLI с указанием сохранить результат в указанный файл.  
Аргументы  
text — исходный текст запроса  
file_path — путь, куда CLI должен сохранить результат  
Возвращает  
Текст промпта с инструкцией для CLI

---
async def _send_mtproto_result(session: Session, dest: dict, output: str, context: ContextTypes.DEFAULT_TYPE, error: Optional[str] = None) -> None  
Отправляет результат MTProto-задачи: читает файл и передаёт содержимое через MTProto.  
Аргументы  
session — сессия, в которой выполнялась задача  
dest — словарь с назначением (peer, file_path, chat_id)  
output — вывод CLI (игнорируется, если файл существует)  
context — контекст выполнения бота  
error — текст ошибки выполнения CLI, если была  
Исключения  
Логирует ошибки чтения файла, но не прерывает выполнение

---
def _cleanup_mtproto_dir(out_dir: str) -> None  
Очищает директорию с результатами MTProto, удаляя файлы старше заданного количества дней.  
Аргументы  
out_dir — путь к директории с файлами результатов

---
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает входящие callback-запросы от Telegram-пользователей  
Аргументы  
update — объект обновления от Telegram с данными callback  
context — контекст выполнения, предоставляемый python-telegram-bot  
Исключения  
Может подавлять исключения при работе с файловой системой и менеджером сессий

---
def _cleanup_old_files(out_dir: str) -> None  
Удаляет Markdown-файлы в указанной директории, старше заданного количества дней  
Аргументы  
out_dir — путь к директории для очистки  
Исключения  
Игнорирует ошибки при доступе к файлам и чтении атрибутов

---
def _format_ts(timestamp: float) -> str  
Форматирует Unix-время в человеко-читаемую строку  
Аргументы  
timestamp — временная метка в формате Unix time  
Возвращает  
Строка в формате "дд.мм.гггг чч:мм"

---
def _build_state_keyboard(chat_id: int) -> InlineKeyboardMarkup  
Генерирует клавиатуру для выбора состояния сессии с пагинацией  
Аргументы  
chat_id — идентификатор чата, для которого строится меню  
Возвращает  
Объект InlineKeyboardMarkup с кнопками выбора и навигации

---
def _send_dirs_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, path: str) -> None  
Отправляет сообщение с меню выбора каталога  
Аргументы  
chat_id — идентификатор чата  
context — контекст выполнения  
path — путь к каталогу, содержимое которого отображается  
Исключения  
Может подавлять ошибки при отправке сообщений

---
def _short_label(path: str) -> str  
Генерирует короткое имя каталога для отображения в кнопках  
Аргументы  
path — полный путь к каталогу  
Возвращает  
Короткое имя каталога (базовое имя пути)

---
def is_within_root(path: str, root: str) -> bool  
Проверяет, находится ли путь внутри корневого каталога  
Аргументы  
path — проверяемый путь  
root — корневой каталог  
Возвращает  
True, если путь находится внутри root, иначе False

---
def prepare_dirs(dirs_menu, dirs_base, dirs_page, dirs_root, chat_id: int, path: str) -> Optional[str]  
Подготавливает данные каталогов для отображения в меню  
Аргументы  
dirs_menu — словарь для хранения списка каталогов  
dirs_base — словарь текущего базового пути  
dirs_page — словарь текущей страницы пагинации  
dirs_root — словарь корневых каталогов  
chat_id — идентификатор чата  
path — путь, содержимое которого нужно подготовить  
Возвращает  
Сообщение об ошибке, если подготовка не удалась, иначе None

---
def build_dirs_keyboard(dirs_menu, dirs_base, dirs_page, label_fn, chat_id: int, base_path: str, page: int) -> InlineKeyboardMarkup  
Создаёт inline-клавиатуру для навигации по каталогам  
Аргументы  
dirs_menu — хранилище списков каталогов  
dirs_base — хранилище базовых путей  
dirs_page — хранилище страниц пагинации  
label_fn — функция для генерации меток кнопок  
chat_id — идентификатор чата  
base_path — текущий путь для отображения  
page — номер страницы пагинации  
Возвращает  
Объект InlineKeyboardMarkup с кнопками каталогов и навигации

---
async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает входящие callback-запросы от inline-кнопок.  
Аргументы  
update — Объект обновления Telegram, содержащий callback-данные.  
context — Контекст выполнения бота, предоставляющий доступ к состоянию и сервисам.  
Исключения  
Может выбрасывать исключения при ошибках чтения/записи файлов, вызове внешних инструментов или сетевых операциях.

---
async def _send_toolhelp_content(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str) -> None  
Отправляет пользователю содержимое справки по инструменту, разбивая его на части при необходимости.  
Аргументы  
chat_id — Идентификатор чата для отправки сообщения.  
context — Контекст выполнения бота.  
content — Текст справочной информации, который необходимо отправить.

---
async def _send_document(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, document: BinaryIO) -> None  
Отправляет бинарный документ в указанный чат.  
Аргументы  
context — Контекст выполнения бота.  
chat_id — Идентификатор получателя.  
document — Открываемый в режиме чтения бинарный файл для отправки.

---
async def _send_files_menu(self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE, edit_message: Optional[CallbackQuery] = None) -> None  
Формирует и отправляет inline-меню с файлами и директориями из текущей рабочей директории сессии.  
Аргументы  
chat_id — Идентификатор чата.  
session — Активная сессия, определяющая рабочую директорию.  
context — Контекст выполнения бота.  
edit_message — Опциональный запрос на редактирование существующего сообщения вместо отправки нового.

---
def load_active_state(state_path: str) -> Optional[ActiveState]  
Загружает сохранённое состояние активной сессии из файла.  
Аргументы  
state_path — Путь к файлу состояния.  
Возвращает  
Объект ActiveState при успешной загрузке, иначе None.

---
def clear_active_state(state_path: str) -> None  
Удаляет файл сохранённого состояния активной сессии.  
Аргументы  
state_path — Путь к файлу состояния.

---
def get_toolhelp(toolhelp_path: str, tool: str) -> Optional[ToolHelpEntry]  
Извлекает закэшированную справку по инструменту из локального хранилища.  
Аргументы  
toolhelp_path — Путь к файлу с кэшем справки.  
tool — Название инструмента.  
Возвращает  
Объект ToolHelpEntry, если справка найдена, иначе None.

---
def update_toolhelp(toolhelp_path: str, tool: str, content: str) -> None  
Сохраняет или обновляет справку по инструменту в локальном хранилище.  
Аргументы  
toolhelp_path — Путь к файлу с кэшем справки.  
tool — Название инструмента.  
content — Текст справки для сохранения.

---
def run_tool_help(tool_config: ToolConfig, workdir: str, timeout: int) -> str  
Синхронно запускает инструмент с флагом --help и возвращает его вывод.  
Аргументы  
tool_config — Конфигурация инструмента (путь, аргументы и т.д.).  
workdir — Рабочая директория для запуска инструмента.  
timeout — Максимальное время выполнения в секундах.  
Возвращает  
Вывод stdout инструмента при успешном выполнении.  
Исключения  
Выбрасывает исключение при таймауте, ошибке запуска или ненулевом коде возврата.

---
async def handle_callback(self, query: CallbackQuery, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает входящие callback-запросы от пользовательских интерфейсов  
Аргументы  
query — объект callback-запроса от Telegram  
chat_id — идентификатор чата  
context — контекст выполнения бота  
Исключения  
Может выбрасывать исключения при ошибках файловых операций или недоступности сессии

---
async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Отображает список доступных CLI-инструментов  
Аргументы  
update — входящее обновление от Telegram  
context — контекст выполнения бота

---
async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Создаёт новую сессию с указанным инструментом и рабочей директорией  
Аргументы  
update — входящее обновление от Telegram  
context — контекст выполнения бота

---
async def cmd_newpath(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Завершает создание сессии, устанавливая путь после выбора инструмента  
Аргументы  
update — входящее обновление от Telegram  
context — контекст выполнения бота

---
async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Отображает список активных сессий  
Аргументы  
update — входящее обновление от Telegram  
context — контекст выполнения бота

---
async def cmd_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Активирует указанную сессию или показывает меню выбора, если сессия не указана.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Закрывает указанную сессию или показывает меню выбора, если сессия не указана.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Выводит текущее состояние активной сессии, включая статус, время работы и очередь.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Отправляет сигнал прерывания активной сессии.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Показывает количество сообщений в очереди активной сессии.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_clearqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Очищает очередь активной сессии и сохраняет изменения.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Переименовывает указанную или активную сессию и сохраняет новое имя.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Инициирует режим просмотра директорий, начиная с указанного пути или рабочей директории по умолчанию.  
Аргументы  
update — объект обновления от Telegram  
context — контекст выполнения команды

---
async def cmd_cwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Устанавливает новую рабочую директорию для активной сессии и создает новую сессию с этим путём.  
Аргументы  
update — Объект обновления от Telegram, содержащий информацию о сообщении  
context — Контекст выполнения команды, включая аргументы и данные сессии  
Исключения  
Выбрасывает исключения при ошибках отправки сообщений или некорректных путях

---
async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Инициализирует Git-сессию и отображает клавиатуру с доступными Git-операциями.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды  
Исключения  
Может выбрасывать исключения при ошибках взаимодействия с Git или отправки сообщений

---
async def cmd_setprompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Устанавливает регулярное выражение для распознавания промпта указанного инструмента.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды, содержит аргументы команды  
Возвращает  
None  
Исключения  
Сохранение конфигурации может завершиться ошибкой, если файл недоступен для записи

---
async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Просматривает или устанавливает токен возобновления для активной сессии.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды  
Исключения  
Может возникнуть ошибка при обновлении состояния на диске

---
async def cmd_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Показывает состояние сессии: либо по указанным tool и workdir, либо через интерактивное меню.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды  
Исключения  
Возможны ошибки при чтении файла состояния или его парсинге

---
async def cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Отправляет текстовое сообщение в активную сессию как CLI-ввод.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды  
Исключения  
Может выбрасывать исключения при отсутствии сессии или ошибках обработки ввода

---
def _bot_commands(self) -> list[BotCommand]  
Формирует список команд бота на основе реестра, фильтруя по отображению в меню.  
Возвращает  
Список объектов BotCommand для регистрации в Telegram  
Исключения  
Не выбрасывает исключения

---
async def set_bot_commands(self, app: Application) -> None  
Устанавливает список команд бота в интерфейсе Telegram.  
Аргументы  
app — Экземпляр приложения Telegram Bot  
Исключения  
Может выбросить исключение при сетевой ошибке или недоступности API

---
async def cmd_toolhelp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Отображает меню выбора инструмента для просмотра его специфичных команд.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды  
Исключения  
Возможны ошибки при отправке сообщения с клавиатурой

---
async def cmd_mtproto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает команду MTProto: либо запрашивает задачу, либо отменяет её, либо сохраняет и показывает меню.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды  
Исключения  
Может возникнуть ошибка при взаимодействии с подсистемой MTProto

---
async def cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Инициирует отображение файлового меню для активной сессии.  
Аргументы  
update — Объект обновления от Telegram  
context — Контекст выполнения команды  
Исключения  
Выбрасывает исключения при отсутствии сессии или ошибках навигации по директориям

---
def _list_dir_entries(base: str) -> list[dict]  
Возвращает отсортированный список элементов каталога (файлов и подкаталогов).  
Аргументы  
base — путь к каталогу, содержимое которого нужно перечислить  
Возвращает  
Список словарей с ключами "name", "path", "is_dir", отсортированный по типу (папки первыми) и имени

---
async def _send_files_menu(self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE, edit_message: Optional[object]) -> None  
Отправляет или обновляет интерактивное меню содержимого каталога с постраничной навигацией.  
Аргументы  
chat_id — идентификатор чата пользователя  
session — активная сессия, содержащая рабочий каталог  
context — контекст выполнения Telegram-бота  
edit_message — опциональное сообщение для редактирования вместо отправки нового  
Возвращает  
None

---
def _preset_commands(self) -> Dict[str, str]  
Возвращает словарь предопределённых команд (шаблонов) для быстрого запуска задач.  
Возвращает  
Словарь, где ключ — имя команды, значение — текстовый промпт

---
def _guess_clone_path(self, url: str, base: str) -> Optional[str]  
Формирует путь для клонирования репозитория на основе URL и базового каталога.  
Аргументы  
url — URL репозитория (SSH или HTTPS)  
base — базовый каталог, в котором будет создана папка  
Возвращает  
Полный путь для клонирования или None, если имя не может быть извлечено

---
async def cmd_preset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Обрабатывает команду выбора шаблонной задачи, отображая меню с доступными пресетами.  
Аргументы  
update — входящее обновление от Telegram  
context — контекст выполнения бота  
Возвращает  
None

---
async def cmd_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  
Отправляет текущие метрики работы бота в ответ на команду.  
Аргументы  
update — входящее обновление от Telegram  
context — контекст выполнения бота  
Возвращает  
None

---
async def run_prompt_raw(self, prompt: str, session_id: Optional[str] = None) -> str  
Выполняет выполнение промпта в указанной или активной сессии с блокировкой.  
Аргументы  
prompt — текст промпта для выполнения  
session_id — идентификатор сессии (опционально)  
Возвращает  
Результат выполнения промпта в виде строки  
Исключения  
RuntimeError — если сессия не найдена или занята

---
async def _send_dirs_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, base: str) -> None  
Формирует и отправляет меню выбора каталога с поддержкой постраничного отображения.  
Аргументы  
chat_id — идентификатор чата пользователя  
context — контекст выполнения Telegram-бота  
base — базовый путь для отображения каталогов  
Возвращает  
None

---
async def _send_toolhelp_content(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str) -> None  
Отправляет пользователю содержимое справки по инструменту.  
Аргументы  
chat_id — идентификатор чата пользователя  
context — контекст выполнения Telegram-бота  
content — текст справки для отображения  
Возвращает  
None

---
async def strip_ansi(text: str) -> str  
Удаляет ANSI-коды форматирования из строки.  
Аргументы  
text — Входная строка с возможными ANSI-кодами  
Возвращает  
Строка без ANSI-кодов

---
async def has_ansi(text: str) -> bool  
Проверяет, содержит ли строка ANSI-коды форматирования.  
Аргументы  
text — Входная строка для проверки  
Возвращает  
True, если строка содержит ANSI-коды, иначе False

---
async def ansi_to_html(text: str) -> str  
Преобразует строку с ANSI-кодами в HTML-представление с поддержкой цветов и стилей.  
Аргументы  
text — Входная строка с ANSI-кодами  
Возвращает  
HTML-строка с эквивалентным форматированием

---
async def make_html_file(html_content: str, prefix: str = "output") -> str  
Создаёт временный HTML-файл с указанным содержимым.  
Аргументы  
html_content — Содержимое HTML-страницы  
prefix — Префикс имени временного файла (по умолчанию "output")  
Возвращает  
Путь к созданному временному файлу

---
def build_command_registry(bot_app: BotApp) -> list[dict]  
Формирует список команд бота с их обработчиками на основе зарегистрированных методов.  
Аргументы  
bot_app — Экземпляр приложения BotApp  
Возвращает  
Список словарей с ключами "name" (имя команды) и "handler" (асинхронная функция-обработчик)

---
def build_app(config: AppConfig) -> Application  
Создаёт и настраивает экземпляр Telegram Application с обработчиками команд и сообщений.  
Аргументы  
config — Объект конфигурации приложения  
Возвращает  
Настроенный экземпляр Application из python-telegram-bot

---
def main() -> None  
Точка входа приложения: загружает конфигурацию и запускает бота в режиме polling.

---
class BotApp  
Основной класс бота, управляющий командами, обработкой сообщений, вложениями и метриками.  
Поля  
config — Объект конфигурации приложения  
metrics — Система сбора метрик (например, количество вызовов команд)  
mcp — Экземпляр MCP-сервера для внешних вызовов  
Методы  
async set_bot_commands(application: Application) — Регистрирует команды бота в интерфейсе Telegram  
async on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) — Обрабатывает нажатия на inline-кнопки  
async on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) — Обрабатывает неизвестные команды  
async on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) — Обрабатывает полученные фотографии  
async on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) — Обрабатывает полученные документы  
async on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) — Обрабатывает текстовые сообщения без команд  
is_allowed(chat_id: int) — Проверяет, разрешён ли доступ для указанного чата
