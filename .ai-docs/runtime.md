# Запуск и окружение

## Запуск бота

Бот запускается в режиме polling через `python-telegram-bot`. Точка входа — функция `build_app`, которая:

1.  Загружает конфигурацию из `config.yaml`.
2.  Инициализирует основные компоненты: `SessionManager`, `ToolRegistry`, `Metrics`.
3.  Регистрирует обработчики команд, сообщений и callback-запросов.
4.  Интегрирует плагины.
5.  Запускает приложение с проверкой доступа по `whitelist_chat_ids`.

**Команда запуска:**
```bash
python bot.py
```

## Переменные окружения

Переменные окружения имеют приоритет над настройками в `config.yaml`.

| Переменная | Назначение | Обязательная |
| :--- | :--- | :--- |
| `TELEGRAM_TOKEN` | Токен бота от @BotFather | Да |
| `TAVILY_API_KEY` | Ключ для поиска через Tavily | Нет |
| `JINA_API_KEY` | Ключ для веб-поиска и чтения страниц (Jina.ai) | Нет |
| `GITHUB_TOKEN` | Токен для аутентификации в Git (HTTPS) | Для приватных репозиториев |
| `OPENAI_API_KEY` | Ключ для доступа к OpenAI API | Для функций, требующих LLM |
| `ZAI_API_KEY` | Ключ для доступа к Z.AI API | Для функций, требующих LLM |
| `WOLFRAM_APP_ID` | ID приложения для WolframAlpha | Для инструмента `wolfram_alpha` |
| `TMDB_API_KEY` | Ключ для доступа к The Movie Database | Для инструмента `movie_info` |
| `EDAMAM_APP_ID`, `EDAMAM_APP_KEY` | Ключи для доступа к Edamam API | Для инструмента `chief` |
| `STABLE_DIFFUSION_TOKEN` | Токен для HuggingFace Inference API | Для инструмента `image_generation` |
| `AGENT_SANDBOX_ROOT` | Корневая директория для песочниц агентов | Нет (по умолчанию `workdir`) |

## Конфигурационный файл (`config.yaml`)

Основной файл конфигурации. Пример структуры:

```yaml
telegram:
  token: "YOUR_TELEGRAM_TOKEN"
  whitelist_chat_ids: [123456789, 987654321]

defaults:
  workdir: "/path/to/workdir"
  log_path: "/path/to/bot.log"
  state_path: "/path/to/state.json"
  image_temp_dir: "/tmp/images"
  image_max_mb: 10
  idle_timeout_sec: 3600
  memory_max_kb: 1024
  memory_compact_target_kb: 512
  output_max_chars: 4000
  clarification_enabled: true
  clarification_keywords: ["уточни", "почему", "где"]

tools:
  codex:
    cmd: "codex --headless --prompt '{prompt}'"
    interactive_cmd: "codex"
    resume_cmd: "codex --resume '{resume}'"
    help_cmd: "codex --help"
    prompt_regex: ".*\\$ $"
    resume_regex: "thread_id: ([a-zA-Z0-9]+)"
    env:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
  # ... другие инструменты (claude, gemini, qwen)

mcp:
  enabled: true
  host: "127.0.0.1"
  port: 8765
  token: "your_mcp_token"

mcp_servers:
  - name: "context7"
    transport: "http"
    url: "http://context7-server:8888"
    headers:
      Authorization: "Bearer ${MCP_CONTEXT7_TOKEN}"
  - name: "notebooklm"
    transport: "stdio"
    cmd: "python -m notebooklm_mcp_server"

presets:
  tests: "Запусти тесты и сообщи результат."
  lint: "Проверь код линтером."
  build: "Собери проект."
```
