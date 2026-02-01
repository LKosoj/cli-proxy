# config_example.yaml

Конфигурационный файл задаёт параметры Telegram-бота, взаимодействующего с ИИ-инструментами через командную строку. Включает настройки подключения к Telegram, конфигурацию инструментов (codex, claude, gemini, qwen), пути по умолчанию, переменные окружения, а также опциональные модули — MTProto-клиент для пересылки сообщений и MCP-сервер для внешнего взаимодействия.

Секции и ключи

telegram — настройки интеграции с Telegram<br>
telegram.token — токен Telegram-бота для авторизации в API<br>
telegram.whitelist_chat_ids — список разрешённых chat_id, с которыми может взаимодействовать бот<br>
tools — конфигурация внешних ИИ-инструментов<br>
tools.codex — настройки инструмента codex<br>
tools.codex.mode — режим работы (например, headless)<br>
tools.codex.cmd — базовая команда запуска codex<br>
tools.codex.interactive_cmd — команда для интерактивного режима<br>
tools.codex.resume_cmd — команда с шаблонами для возобновления сессии<br>
tools.codex.image_cmd — аргументы команды, добавляемые при наличии изображения<br>
tools.codex.resume_regex — регулярное выражение для извлечения thread_id из вывода<br>
tools.codex.help_cmd — команда, отправляемая инструменту для получения справки<br>
tools.codex.auto_commands — команды, автоматически отправляемые при старте сессии<br>
tools.codex.env — переменные окружения, передаваемые инструменту<br>
tools.claude — настройки инструмента claude<br>
tools.claude.mode — режим работы<br>
tools.claude.cmd — команда запуска с шаблонами<br>
tools.claude.interactive_cmd — команда для интерактивного режима<br>
tools.claude.help_cmd — команда получения справки<br>
tools.claude.env — переменные окружения для claude<br>
tools.gemini — настройки инструмента gemini<br>
tools.gemini.mode — режим работы<br>
tools.gemini.cmd — команда запуска с параметрами<br>
tools.gemini.interactive_cmd — команда для интерактивного режима<br>
tools.gemini.help_cmd — команда получения справки<br>
tools.gemini.env — переменные окружения для gemini<br>
tools.qwen — настройки инструмента qwen<br>
tools.qwen.mode — режим работы<br>
tools.qwen.cmd — команда запуска с шаблонами<br>
tools.qwen.interactive_cmd — команда для интерактивного режима<br>
tools.qwen.help_cmd — команда получения справки<br>
tools.qwen.env — переменные окружения для qwen<br>
defaults — параметры по умолчанию для работы бота<br>
defaults.workdir — рабочая директория для выполнения команд<br>
defaults.idle_timeout_sec — таймаут бездействия (в секундах) перед завершением сессии<br>
defaults.summary_max_chars — максимальное количество символов в итоговом отчёте<br>
defaults.html_filename_prefix — префикс имени HTML-файла с выводом команд<br>
defaults.state_path — путь к файлу состояния сессии<br>
defaults.toolhelp_path — путь к файлу с описанием команд инструментов<br>
defaults.openai_api_key — ключ API OpenAI (если используется)<br>
defaults.openai_model — модель OpenAI по умолчанию<br>
defaults.openai_base_url — базовый URL для API OpenAI<br>
defaults.github_token — токен GitHub для доступа к репозиториям<br>
defaults.log_path — путь к файлу логов<br>
defaults.mtproto_output_dir — директория для временных файлов MTProto<br>
defaults.mtproto_cleanup_days — возраст (в днях), после которого файлы удаляются<br>
defaults.image_temp_dir — директория для временных изображений<br>
defaults.image_max_mb — максимальный размер изображения в мегабайтах<br>
mtproto — настройки MTProto-клиента для Telegram<br>
mtproto.enabled — включён ли MTProto-клиент<br>
mtproto.api_id — идентификатор приложения Telegram<br>
mtproto.api_hash — хеш приложения Telegram<br>
mtproto.session_string — строка сессии (альтернатива файлу)<br>
mtproto.session_path — путь к файлу сессии<br>
mtproto.targets — список целевых чатов для пересылки<br>
mtproto.targets.title — отображаемое имя цели<br>
mtproto.targets.peer — идентификатор чата (например, "me")<br>
mcp — настройки MCP-сервера (модуль взаимодействия)<br>
mcp.enabled — включён ли MCP-сервер<br>
mcp.host — хост для прослушивания<br>
mcp.port — порт сервера<br>
mcp.token — токен для аутентификации на сервере<br>
presets — предустановленные команды<br>
presets.name — имя пресета<br>
presets.prompt — текст запроса, отправляемый инструменту

Важные параметры

telegram.token — обязательный параметр; без него бот не сможет подключиться к Telegram<br>
telegram.whitelist_chat_ids — критически важен для безопасности, ограничивает доступ только доверенным пользователям<br>
tools.codex.resume_regex — должен точно соответствовать формату вывода codex для корректного извлечения thread_id<br>
defaults.workdir — определяет, где будут выполняться команды; должен указывать на актуальный рабочий каталог<br>
defaults.openai_api_key — необходим для инструментов, использующих OpenAI, например codex<br>
mtproto.enabled — включение может потребовать дополнительной настройки и повышает поверхность атаки<br>
mcp.enabled — включение открывает сетевой порт; требует аутентификации через token для безопасности
