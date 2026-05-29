# Аудит кодовой базы: cli-proxy (Session Transfer)

## 1. Структура проекта
В основе архитектуры лежит Telegram-бот с UI (MiniApp/Desktop), оркестрирующий сессии CLI-агентов (Codex, Qwen, Gemini, Claude). Основные директории:
- `sessions/`: Управление жизненным циклом сессий, scope, запуск команд, обработка вывода и UI (9 файлов).
- `modes/`: Реализация режимов работы (`admin`, `codebase_mapper`, `sdk` и др., всего 126 файлов).
- `app/services/`: Базовые сервисы, включая мониторинг файлов агентов (JSONL-мониторы) и трансфер контекста.
- `.cli-proxy/.codebase_map/`: Граф зависимостей и маппинг документации (`INDEX.md`, `ARCHITECTURE.md`, `STRUCTURE.md`, `STACK.md`).
- Стек (из `STACK.md`): Python 3.12+, `aiohttp`, `python-telegram-bot`, `PySide6` для десктопа, `SQLAlchemy` + SQLite для хранения состояния. 

## 2. Текущая реализация переноса сессий (Gemini/Claude/Qwen)
На текущий момент **прямой конвертации файлов сессий** (нативной перезаписи) в проекте нет. 
Перенос сессий работает как **Prompt Injection** (текстовая инъекция контекста):
- При переключении CLI (или начале новой сессии с трансфером) данные из предыдущего CLI парсятся в универсальный объект `CanonicalSession`.
- Метод `_apply_pending_transfer` (из `sessions/session_run_service.py` и `session_management.py`) читает отложенный трансфер.
- Функция `build_injection_prompt` (в `injector.py`) берет до 20 последних сообщений и форматирует их в текст (вида `- User: ...\n- Assistant: ...`), который добавляется к пользовательскому запросу до отправки его целевому CLI.

## 3. Модули, ответственные за Session Transfer
Основной код вынесен в директорию `app/services/session_transfer/`:
- `canonical.py`: Модели данных `CanonicalSession` и `CanonicalMessage`.
- `service.py`: Оркестратор трансфера. Содержит маппинг `_READERS` для `claude`, `qwen`, `gemini` и функции жизненного цикла (`extract_session`, `check_transfer_available`, `store_pending_transfer`, `consume_pending_transfer`).
- `injector.py`: Формирование текста для prompt injection. 
- `reader_claude.py`, `reader_gemini.py`, `reader_qwen.py`: Адаптеры-ридеры для соответствующих CLI. 

## 4. API контракты для чтения/записи сессий
- **Чтение:** Контракт чтения задан как: 
  `Callable[[str, str], Optional[CanonicalSession]]`
  Функция (например, в `reader_claude.read_session(session_id, workspace)`) принимает ID сессии и рабочую директорию, возвращая заполненную структуру `CanonicalSession` или `None`.
- **Запись:** **Контракт записи отсутствует**. Система полагается только на изменение текстового промпта, генерируемого для старта агента, а не на генерацию нативных файлов сессий агентов на диске.

## 5. Единый формат сессии
Для передачи контекста используется структура из `canonical.py`:
```python
@dataclass
class CanonicalMessage:
    role: str  # "user" | "assistant" | "tool" | "system"
    content: str
    timestamp: Optional[float] = None

@dataclass
class CanonicalSession:
    source_cli: str
    session_id: str
    workspace: str
    messages: List[CanonicalMessage]
    summary: Optional[str] = None
    extracted_at: float
```
**Риск для Codex:** Этот формат является "сплющенным" (`lossy translation`), он преобразует сложные структуры (например, вызовы тулов) в обычный текст (`content: "[tool: name]"`).

---

## 6. Implementation Guidance Layer (Референс CodeDash)

**Source:** [vakovalskii/codedash](https://github.com/vakovalskii/codedash)

**Extracted Pattern:**
- CodeDash реализует **нативную конвертацию файлов сессий**, а не prompt injection. 
- Команда `convert` читает JSONL исходного агента (например, `~/.claude/projects/.../xxx.jsonl`), маппит полную внутреннюю структуру (с сохранением сырых вызовов инструментов `tool_calls` и `tool_results`) и физически записывает валидный JSONL файл в директорию целевого агента (например, `~/.codex/sessions/...`).

**Local Mapping (Адаптация в `cli-proxy`):**
Для полноценного двустороннего переноса (особенно _записи в Codex_) по аналогии с CodeDash нужно:
1. Расширить `CanonicalMessage` (в `canonical.py`), добавив опциональные поля `tool_calls` и `tool_results` (или `raw_data: dict`), чтобы не терять сложные метаданные при чтении.
2. Создать `reader_codex.py`: реализовать функцию `read_session`, парсящую файлы из `~/.codex/sessions/*.jsonl` (пути можно взять из `cli_limits_service.py`).
3. Создать `writer_codex.py`: определить API контракт записи. Написать функцию `write_session(canonical: CanonicalSession, target_session_id: str, target_workspace: str)`, которая генерирует JSONL файл по правилам Codex.
4. Обновить `session_run_service.py` и `session_management.py`: если целевой CLI — `codex`, перед запуском создавать файл сессии через `write_session` (подкладывая его в нужную директорию) вместо инъекции текста в `prompt`.

**Статус адаптации:** 
Референс высокорелевантен. Требуется архитектурное смещение: добавление слоя "Native File Writer" параллельно или вместо текущего слоя "Prompt Injector" для CLI, поддерживающих формат Codex/JSONL.