# Зависимости

Проект требует установки следующих Python-пакетов:

```txt
python-telegram-bot==20.7
openai
duckduckgo-search
gTTS
youtube-transcript-api
pdfminer.six>=20230625
beautifulsoup4>=4.12.0
trafilatura>=1.9.0
md2tgmd
PyYAML
httpx>=0.25.0
requests
markdown-it-py[linkify,plugins]
pexpect
ansi2html
```

## Основные компоненты

- **`python-telegram-bot`** — основа для асинхронного взаимодействия с Telegram API.
- **`openai`** — интеграция с OpenAI API для генерации текста, резюмирования и анализа.
- **`httpx`** — асинхронные HTTP-запросы (включая поддержку прокси и таймаутов).
- **`PyYAML`** — загрузка и парсинг конфигурационного файла `config.yaml`.
- **`md2tgmd`** — корректное экранирование MarkdownV2 для отправки в Telegram.

## Дополнительные зависимости

- **`duckduckgo-search`** — веб-поиск для инструмента `search_web`.
- **`gTTS`** — генерация речи из текста (инструмент `gtts_text_to_speech`).
- **`youtube-transcript-api`** — извлечение субтитров с YouTube.
- **`pdfminer.six`** — парсинг текста из PDF-файлов.
- **`beautifulsoup4`, `trafilatura`** — извлечение и очистка текста с веб-страниц.
- **`pexpect`** — управление интерактивными CLI-сессиями (например, Codex, Gemini).
- **`ansi2html`** — преобразование ANSI-цветов в HTML для отображения в Telegram.

## Установка

```bash
pip install -r requirements.txt
```

Для работы с конкретными инструментами требуются дополнительные компоненты:

- **`plantuml.jar`** — для генерации диаграмм (`show_me_diagrams`), должен быть доступен в PATH.
- **`Java`** — требуется для запуска PlantUML.
- **`ffmpeg`** — опционально, для обработки видео (например, в `HaiperImageToVideoTool`).

## Выявленные зависимости

- PyYAML==6.0.2
- ansi2html==1.9.1
- beautifulsoup4>=4.12.0
- duckduckgo-search==7.5.2
- gTTS==2.5.4
- httpx>=0.25.0
- linkify-it-py==2.0.3
- markdown-it-py==3.0.0
- md2tgmd==0.3.10
- mdit-py-plugins==0.4.1
- openai>=1.0.0
- pdfminer.six>=20221105
- pexpect==4.9.0
- pytest==8.3.4
- python-telegram-bot==20.7
- requests==2.32.3
- trafilatura>=1.6.0
- youtube-transcript-api==1.2.2
