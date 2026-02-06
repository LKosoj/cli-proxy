# Руководство по разработке плагинов

## Оглавление

1. [Обзор архитектуры](#обзор-архитектуры)
2. [Быстрый старт](#быстрый-старт)
3. [Базовый класс ToolPlugin](#базовый-класс-toolplugin)
4. [Спецификация инструмента (ToolSpec)](#спецификация-инструмента-toolspec)
5. [Меню плагина](#меню-плагина)
6. [Telegram-команды (устаревший подход)](#telegram-команды-устаревший-подход)
7. [Inline-кнопки](#inline-кнопки-callback_handlers--dialog_button--action_button)
8. [Интерактивные диалоги (DialogMixin)](#интерактивные-диалоги-dialogmixin)
   - [Когда использовать DialogMixin](#когда-использовать-dialogmixin)
   - [Жизненный цикл диалога](#жизненный-цикл-диалога)
   - [Состояние диалога (DialogState)](#состояние-диалога-dialogstate)
   - [Шаги диалога](#шаги-диалога)
   - [Отмена и таймауты](#отмена-и-таймауты)
   - [Приём нетекстового контента](#приём-нетекстового-контента)
   - [Подсказки по шагам (step_hint)](#подсказки-по-шагам-step_hint)
   - [Переопределение DIALOG_TIMEOUT](#переопределение-dialog_timeout)
9. [Агент-инициированные запросы (AskUser)](#агент-инициированные-запросы-askuser)
10. [Автоматическая очистка диалогов](#автоматическая-очистка-диалогов)
11. [Жизненный цикл плагина](#жизненный-цикл-плагина)
12. [Сервисы (services)](#сервисы-services)
13. [Регистрация и автозагрузка](#регистрация-и-автозагрузка)
14. [Обработка ошибок](#обработка-ошибок)
15. [Чеклист для нового плагина](#чеклист-для-нового-плагина)
16. [Примеры](#примеры)

---

## Обзор архитектуры

```
┌─────────────────────────────────────────────────┐
│  Telegram                                        │
│  (сообщения, команды, callback-кнопки)           │
└──────────┬──────────────────────────────────────┘
           │
           ▼
┌──────────────────────┐    ┌──────────────────────┐
│  bot.py (BotApp)     │    │  SessionManager      │
│                      │    │  on_session_change()  │
│  on_message          │    └──────────┬───────────┘
│  on_callback         │               │
│  on_command          │               │ callback при
│                      │               │ create/switch/close
│  _AgentEnabledFilter │◄──────────────┘
│  _PLUGIN_GROUP = -1  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  ToolRegistry        │
│                      │
│  register()          │
│  execute()           │
│  get_message_handlers│
│  any_awaiting_input  │
│  cancel_all_inputs   │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  ToolPlugin          │  ← Базовый класс
│  DialogMixin         │  ← Миксин для диалогов
│                      │
│  agent/plugins/      │
│    base.py           │
│    task_management.py│
│    haiper_image_to_video.py
│    text_document_qa.py
│    ask_user.py       │
└──────────────────────┘
```

**Принципы:**
- Каждый плагин — это Python-класс, наследующий `ToolPlugin`.
- Плагин может быть вызван **агентом** (через `execute()`) и/или **пользователем** (через Telegram-команды и диалоги).
- Плагин **не знает** о `bot.py` напрямую. Взаимодействие идёт через абстракции: `get_commands()`, `get_message_handlers()`, `awaiting_input()`, `cancel_input()`.
- Хендлеры плагинов работают в **группе -1** (приоритетнее основного `on_message`), но только если `_AgentEnabledFilter` пропускает (агент активен в текущей сессии).

---

## Быстрый старт

Минимальный плагин без диалогов:

```python
from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from typing import Any, Dict

class MyTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="my_tool",
            description="Делает что-то полезное.",
            parameters={
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Входные данные"},
                },
                "required": ["input"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        value = args.get("input", "")
        result = value.upper()  # Какая-то полезная логика
        return {"success": True, "output": result}
```

Файл размещается в `agent/plugins/my_tool.py` — и автоматически загружается при старте бота.

---

## Базовый класс ToolPlugin

```python
class ToolPlugin(ABC):
    plugin_id: Optional[str] = None       # Уникальный ID (по умолчанию — имя класса)
    function_prefix: Optional[str] = None  # Префикс для имени инструмента в реестре

    def initialize(self, config, services) -> None
    def close(self) -> None
    def get_source_name(self) -> str
    def get_spec(self) -> ToolSpec                        # ОБЯЗАТЕЛЬНЫЙ
    async def execute(self, args, ctx) -> Dict[str, Any]  # ОБЯЗАТЕЛЬНЫЙ
    def get_commands(self) -> List[Dict[str, Any]]
    def get_menu_label(self) -> Optional[str]             # Имя в меню плагинов (None = скрыт)
    def get_menu_actions(self) -> List[Dict[str, str]]    # Кнопки подменю
    def get_message_handlers(self) -> List[Dict[str, Any]]
    def get_inline_handlers(self) -> List[Dict[str, Any]]
    def awaiting_input(self, chat_id: int) -> bool
    def cancel_input(self, chat_id: int) -> bool
```

### Методы, обязательные к реализации

| Метод | Назначение |
|-------|-----------|
| `get_spec()` | Возвращает `ToolSpec` — описание инструмента для агента |
| `execute(args, ctx)` | Основная логика. Вызывается агентом или из Telegram-команд |
| `get_menu_label()` | Имя плагина в двухуровневом меню. `None` = не показывать |
| `get_menu_actions()` | Кнопки подменю (`[{"label": "...", "action": "..."}]`) |

### Контекст execute (`ctx`)

| Ключ | Тип | Описание |
|------|-----|----------|
| `chat_id` | `int` | Telegram chat ID |
| `cwd` | `str` | Рабочий каталог текущей сессии |
| `state_root` | `str` | Корневой каталог для хранения данных |
| `bot` | `BotApp` | Экземпляр бота (для прямых вызовов) |
| `context` | `ContextTypes.DEFAULT_TYPE` | Telegram context |
| `session_id` | `str` | ID текущей сессии |
| `allowed_tools` | `List[str]` | Разрешённые инструменты |

### Формат ответа execute

```python
# Успех:
{"success": True, "output": "Результат работы"}

# Ошибка:
{"success": False, "error": "Описание ошибки"}
```

---

## Спецификация инструмента (ToolSpec)

```python
@dataclass
class ToolSpec:
    name: str                      # Уникальное имя (snake_case)
    description: str               # Описание для агента (кратко, на русском)
    parameters: Dict[str, Any]     # JSON Schema параметров
    timeout_ms: int = 120_000      # Таймаут выполнения (мс)
    risk_level: str = "low"        # low | medium | high
    requires_approval: bool = False # Требует подтверждения пользователя
    tags: List[str] = []           # Теги для фильтрации
    parallelizable: bool = True    # Можно ли запускать параллельно
```

Поле `parameters` — JSON Schema (тип `object`):

```python
parameters={
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "delete"]},
        "name": {"type": "string", "description": "Имя элемента"},
        "count": {"type": "integer", "description": "Количество"},
    },
    "required": ["action", "name"],
}
```

---

## Меню плагина

Плагины, видимые пользователю, регистрируются в двухуровневом inline-меню бота (кнопка «Плагины» в меню агента). Для этого реализуйте два метода:

### get_menu_label

```python
def get_menu_label(self) -> Optional[str]:
    """Человекочитаемое имя плагина в меню. None = скрыт."""
    return "Документы"
```

### get_menu_actions

```python
def get_menu_actions(self) -> List[Dict[str, str]]:
    """Кнопки подменю плагина.
    Каждая запись: {"label": "Список", "action": "list"}
    action маршрутизируется в callback_handlers()[action].
    """
    return [
        {"label": "Список", "action": "list"},
        {"label": "Загрузить", "action": "upload"},
    ]
```

### Как это работает

```
Меню агента
  └── Плагины
        ├── Задачи        → [Список] [Добавить]
        ├── Документы      → [Список] [Загрузить] [Задать вопрос]
        ├── Haiper (видео) → [Создать видео]
        └── Напоминания    → [Список] [Создать]
```

Кнопки подменю генерируются через `action_button()` самого плагина. При нажатии callback_data (`cb:{plugin_id}:{action}`) маршрутизируется в `_dispatch_callback` -> `callback_handlers()[action]`. Никаких Telegram-команд не требуется.

### Полный пример

```python
class MyPlugin(DialogMixin, ToolPlugin):
    def get_menu_label(self):
        return "Мой плагин"

    def get_menu_actions(self):
        return [
            {"label": "Действие 1", "action": "do_thing"},
            {"label": "Диалог", "action": "start_dialog"},
        ]

    def get_commands(self):
        return self._dialog_callback_commands()

    def callback_handlers(self):
        return {
            "do_thing": self._cb_do_thing,
            "start_dialog": self._cb_start_dialog,
        }

    def dialog_steps(self):
        return {"wait_input": self._on_input}
```

- Плагины без `get_menu_label()` (возвращает `None`) в меню не попадают.
- `execute()` по-прежнему работает для вызовов от агента.

---

## Telegram-команды (устаревший подход)

> **Устаревший подход.** Для пользовательских плагинов используйте [Меню плагина](#меню-плагина) + `callback_handlers()`. Telegram-команды оставлены для обратной совместимости и системных хендлеров.

Метод `get_commands()` возвращает список словарей — описаний команд:

```python
def get_commands(self) -> List[Dict[str, Any]]:
    return [
        {
            "command": "my_cmd",                  # Без слеша! bot.py добавит сам
            "description": "Описание для меню",   # Показывается в /help
            "handler": self.cmd_my_handler,        # async (update, context) -> None
            "handler_kwargs": {},                  # Доп. kwargs для handler
            "add_to_menu": True,                   # Добавить в меню Telegram
        }
    ]
```

**Важно:** хендлер команды оборачивается в `_cmd_wrap`, который:
- Проверяет whitelist (`is_allowed`).
- Проверяет, что агент активен (`agent_enabled`).
- Если агент не активен — отвечает «Агент не активен».

Если плагин должен работать **без агента** (например, справочная команда), нужно не использовать Telegram-команду через `get_commands()`, а зарегистрировать хендлер иным способом.

---

## Inline-кнопки (callback_handlers / dialog_button / action_button)

`DialogMixin` предоставляет **два встроенных механизма** для работы с inline-кнопками, плюс кнопку отмены. Все три используют единый `_dispatch_callback` и регистрируются одним вызовом `_dialog_callback_commands()`.

### Формат callback_data

Все callback_data автоматически содержат `plugin_id`, что исключает конфликты:

| Префикс | Формат | Назначение |
|---------|--------|-----------|
| `dlg:` | `dlg:{plugin_id}:{payload}` | Кнопки внутри шага диалога |
| `cb:` | `cb:{plugin_id}:{action}:{payload}` | Автономные кнопки (меню) |
| `dlg_cancel:` | `dlg_cancel:{plugin_id}` | Кнопка «Отмена» |

### 1. Кнопки внутри шага диалога (`dialog_button`)

На шаге диалога можно предложить inline-кнопки **вместе с** (или вместо) текстовым вводом. Нажатие маршрутизируется к `"callback"` обработчику текущего шага.

Создание кнопки:

```python
# Внутри метода плагина:
btn = self.dialog_button("High", "high")
# Генерирует callback_data = "dlg:MyPlugin:high"
```

Обработка — через `dialog_steps()` с dict-значением:

```python
def dialog_steps(self):
    return {
        "choose_priority": {
            "message": self._on_priority_text,    # текстовый ввод (опционально)
            "callback": self._on_priority_button,  # кнопка (опционально)
        },
    }
```

Извлечение payload из callback:

```python
async def _on_priority_button(self, update, context):
    payload = self.parse_callback_payload(update)  # "high"
    # ...
```

### 2. Автономные кнопки — inline-меню (`action_button` + `callback_handlers`)

Для кнопок, работающих **вне диалога** (список задач, управление статусом), используйте `callback_handlers()`:

```python
def callback_handlers(self):
    return {
        "refresh": self._cb_refresh,
        "view": self._cb_view,
        "del": self._cb_delete,
    }
```

Каждый обработчик — `async def handler(update, context, payload: str)`:

```python
async def _cb_refresh(self, update, context, payload):
    query = update.callback_query
    # payload = "" (если не передан) или строка
    text, markup = self._build_menu(user_id)
    await query.edit_message_text(text, reply_markup=markup)
```

Создание кнопок:

```python
btn1 = self.action_button("Обновить", "refresh")
# callback_data = "cb:MyPlugin:refresh"

btn2 = self.action_button("Удалить", "del", task_id)
# callback_data = "cb:MyPlugin:del:tsk_12345"
```

### 3. Кнопка отмены (`cancel_markup`)

Готовая кнопка для диалогов:

```python
await msg.reply_text("Введите данные...", reply_markup=self.cancel_markup())
# Создаёт кнопку с callback_data = "dlg_cancel:MyPlugin"
```

### Регистрация

Все три типа кнопок обрабатываются **одним** `CallbackQueryHandler`, который регистрируется через `_dialog_callback_commands()`:

```python
def get_commands(self):
    return self._dialog_callback_commands()
```

Этот вызов заменяет устаревший `_base_cancel_commands()` (который остаётся как алиас для обратной совместимости). Telegram-команды больше не нужны — действия плагина доступны через двухуровневое inline-меню (`get_menu_label` + `get_menu_actions`).

### Автоматическая проверка агента

`DialogMixin` **автоматически** проверяет, активен ли агент, в двух точках:

- **`_dispatch_callback`** — при любом нажатии кнопки плагина. Если агент не активен, пользователь получит «Агент не активен.» и обработчик **не вызывается**.
- **`handle_message`** — при любом текстовом вводе в активный диалог. Если агент выключен, диалог автоматически отменяется.

**Не нужно** вызывать `_ensure_agent_enabled()` в обработчиках вручную — это сделает базовый класс.

### Хелпер parse_callback_payload

```python
payload = self.parse_callback_payload(update)
# Для "dlg:Pid:my_data"       → "my_data"
# Для "cb:Pid:action:my_data"  → "my_data"
# Для "cb:Pid:action"          → ""
```

---

## Интерактивные диалоги (DialogMixin)

### Когда использовать DialogMixin

Используйте `DialogMixin`, если ваш плагин:
- Требует **многошаговый ввод** от пользователя (текст, фото, документы).
- Должен **перехватывать все сообщения** пользователя, пока диалог активен.
- Нуждается в **текстовой отмене** (слова «отмена», «cancel», «выход»).

**Не используйте**, если:
- Плагин вызывается только агентом и не имеет Telegram UI (как `AskUserTool`).
- Плагин не требует пользовательского ввода вообще.

### Объявление класса

```python
from agent.plugins.base import DialogMixin, ToolPlugin

class MyPlugin(DialogMixin, ToolPlugin):
    ...
```

> **Критически важно:** `DialogMixin` должен стоять **перед** `ToolPlugin` в списке базовых классов. Это обеспечивает правильный порядок разрешения методов (MRO): `get_message_handlers()`, `awaiting_input()` и `cancel_input()` из `DialogMixin` переопределяют дефолты из `ToolPlugin`.

### Жизненный цикл диалога

```
Пользователь               Плагин                    Бот (bot.py)
    │                         │                          │
    │  /command или кнопка    │                          │
    │─────────────────────────▶                          │
    │                         │ start_dialog(chat_id,    │
    │                         │   "step_1", data={...})  │
    │                         │                          │
    │                         │ awaiting_input() = True  │
    │  Текст/фото             │                          │
    │─────────────────────────┼──────────────────────────▶
    │                         │ _AgentEnabledFilter: OK  │
    │                         │ _dialog_active_filter: OK│
    │                         ◀──────────────────────────│
    │                         │ handle_message()         │
    │                         │   → dialog_steps()["step_1"]
    │                         │   → self._on_step_1()    │
    │                         │                          │
    │                         │ set_step(chat_id, "step_2")
    │  Ещё текст              │                          │
    │─────────────────────────┼──────────────────────────▶
    │                         │ → self._on_step_2()      │
    │                         │ end_dialog(chat_id)      │
    │                         │                          │
    │                         │ awaiting_input() = False │
    │  Обычное сообщение      │                          │
    │─────────────────────────┼──────────────────────────▶
    │                         │ Фильтр не совпал         │
    │                         │                      on_message()
    │                         │                      → CLI сессия
```

### Состояние диалога (DialogState)

```python
@dataclass
class DialogState:
    step: str                      # Имя текущего шага
    data: Dict[str, Any]           # Произвольные данные диалога
    user_id: int = 0               # Telegram user ID
    started_at: float              # Время начала (для таймаута)
```

Состояние хранится **per-chat** (по `chat_id`). Это значит, что один плагин может вести **параллельные диалоги** в разных чатах (если бот обслуживает несколько).

### Шаги диалога

Переопределите `dialog_steps()` — сопоставление имени шага с обработчиком.

**Вариант 1: только текст/медиа (обратная совместимость)**

```python
def dialog_steps(self):
    return {
        "wait_image": self._on_image,
        "wait_prompt": self._on_prompt,
    }
```

**Вариант 2: текст + inline-кнопки**

```python
def dialog_steps(self):
    return {
        "choose_priority": {
            "message": self._on_priority_text,    # текстовый ввод
            "callback": self._on_priority_button,  # нажатие кнопки
        },
        "wait_title": self._on_title,  # можно смешивать форматы
    }
```

Обработчик текста — `async def handler(update, context) -> None`.
Обработчик кнопки — `async def handler(update, context) -> None` (update содержит `callback_query`).

Переход между шагами и завершение:

```python
async def _on_image(self, update, context):
    msg = update.effective_message
    chat_id = update.effective_chat.id

    # Обработка ввода...

    # Переход к следующему шагу:
    self.set_step(chat_id, "wait_prompt", data={"image_path": path})

    # Или завершение диалога:
    # self.end_dialog(chat_id)
```

### API управления состоянием

| Метод | Назначение |
|-------|-----------|
| `start_dialog(chat_id, step, data=None, user_id=0)` | Начать диалог с указанного шага |
| `end_dialog(chat_id)` | Завершить диалог |
| `get_dialog(chat_id) → Optional[DialogState]` | Получить текущее состояние (или None если истёк таймаут) |
| `set_step(chat_id, step, data=None)` | Перейти к другому шагу (сбрасывает таймер) |
| `is_cancel_text(text) → bool` | Проверить, является ли текст словом отмены |
| `_ensure_agent_enabled(context) → bool` | Проверка активности агента (вызывается автоматически) |
| `cancel_markup() → InlineKeyboardMarkup` | Клавиатура с кнопкой «Отмена» |
| `dialog_button(label, data) → InlineKeyboardButton` | Кнопка для шага диалога |
| `action_button(label, action, payload) → InlineKeyboardButton` | Кнопка для автономного меню |
| `parse_callback_payload(update) → str` | Извлечь payload из callback_data |
| `_dialog_callback_commands() → List[Dict]` | Единый CallbackQueryHandler для всех кнопок |

### Отмена и таймауты

**Текстовая отмена.** `DialogMixin` автоматически распознаёт слова отмены в `handle_message()`, **до** вызова обработчика шага:

```python
CANCEL_WORDS = {"отмена", "отменить", "cancel", "выход", "exit", "-"}
```

Если пользователь отправляет одно из этих слов, диалог сбрасывается и отправляется «Отменено.» Обработчик шага **не вызывается**.

**Кнопочная отмена.** `DialogMixin` предоставляет готовую кнопку «Отмена» через `cancel_markup()`. Обработчик уже включён в `_dialog_callback_commands()`. Подробнее см. раздел [Inline-кнопки](#inline-кнопки-callback_handlers--dialog_button--action_button).

**Таймаут.** По умолчанию диалог автоматически сбрасывается через 300 секунд (5 минут) неактивности:

```python
DIALOG_TIMEOUT = 300  # секунды
```

Таймаут проверяется при каждом вызове `get_dialog()`. Если время вышло — состояние удаляется и `get_dialog()` возвращает `None`.

**Важно:** `set_step()` сбрасывает таймер. То есть таймаут считается от последнего перехода между шагами.

### Приём нетекстового контента

По умолчанию `DialogMixin` регистрирует хендлер только для `filters.TEXT`. Чтобы принимать фото, документы и т.д., переопределите `extra_message_filters()`:

```python
from telegram.ext import filters

def extra_message_filters(self) -> Any:
    return filters.PHOTO | filters.Document.IMAGE
```

Результат будет OR-скомбинирован с `filters.TEXT`, поэтому `handle_message()` будет вызываться и для текста, и для фото.

**Внутри обработчика шага** проверяйте тип контента:

```python
async def _on_image_step(self, update, context):
    msg = update.effective_message
    has_photo = msg.photo or (
        msg.document and msg.document.mime_type
        and msg.document.mime_type.startswith("image/")
    )
    if not has_photo:
        # Пользователь отправил текст, а мы ждём фото
        await msg.reply_text("Жду изображение. Отправьте фото или «отмена».")
        return
    # Обработка фото...
```

### Подсказки по шагам (step_hint)

Переопределите `step_hint(step)`, чтобы возвращать подсказку для пользователя:

```python
def step_hint(self, step: str) -> Optional[str]:
    if step == "wait_image":
        return "Сейчас жду изображение. Отправьте картинку."
    return None
```

Подсказка вызывается автоматически миксином, если пользователь отправляет текст на шаге, который имеет только `"callback"` обработчик (без `"message"`). В остальных случаях используйте `step_hint()` в своих обработчиках вручную.

### Переопределение DIALOG_TIMEOUT

Если операция плагина длительная (генерация видео, ожидание внешнего API), увеличьте таймаут:

```python
class MyLongRunningPlugin(DialogMixin, ToolPlugin):
    DIALOG_TIMEOUT = 60 * 60  # 1 час
```

---

## Агент-инициированные запросы (AskUser)

Плагин `AskUserTool` — пример другого паттерна: **агент** инициирует вопрос пользователю с кнопками. Пользователь отвечает нажатием кнопки, и ответ возвращается агенту через `asyncio.Future`.

Этот паттерн **не использует** `DialogMixin`, потому что:
- Инициатор — агент, а не пользователь.
- Ответ — нажатие кнопки, а не свободный текст.
- `awaiting_input()` не нужен — пользователь не вводит текст.

Используйте `AskUserTool` как инструмент агента, а не как шаблон для разработки.

---

## Автоматическая очистка диалогов

Диалоги плагинов **автоматически сбрасываются** при:

1. **Смене сессии** (`SessionManager.set_active()`) — callback `on_session_change` вызывает `cancel_all_inputs()` для всех чатов.
2. **Создании новой сессии** (`SessionManager.create()`) — аналогично.
3. **Закрытии сессии** (`SessionManager.close()`) — аналогично.
4. **Выключении агента** (`agent_set:off` в `on_callback`) — явный вызов `_cancel_plugin_dialogs(chat_id)`.
5. **Safety net в `on_message`** — если `awaiting_input()` возвращает `True`, но агент выключен, диалоги очищаются и сообщение проходит дальше в CLI.

Механизм реализован через callback `SessionManager.on_session_change`, который `BotApp` регистрирует при инициализации. Это **системный** подход — вам **не нужно** вручную очищать диалоги при смене сессий.

```
SessionManager.create/set_active/close
    → _fire_session_change()
        → BotApp._on_session_change()
            → _cancel_plugin_dialogs(chat_id) для всех whitelisted чатов
                → registry.cancel_all_inputs(chat_id)
                    → plugin.cancel_input(chat_id) для каждого плагина
                        → DialogMixin.end_dialog(chat_id)
```

**Что это значит для разработчика плагина:** вам достаточно корректно реализовать `cancel_input()` (если вы используете `DialogMixin`, он уже реализован). Всё остальное сделает система.

---

## Жизненный цикл плагина

```
1. Загрузка     PluginLoader сканирует agent/plugins/*.py
                Находит классы, наследующие ToolPlugin
                Создаёт экземпляр: cls()

2. Регистрация  ToolRegistry.register(plugin)
                → plugin.initialize(config, services)
                → plugin.get_spec() — регистрация спецификации
                → plugin.get_commands() — регистрация команд
                → plugin.get_message_handlers() — регистрация хендлеров

3. Работа       execute() — вызов от агента
                Telegram-команды — вызов от пользователя
                Диалоги — интерактивный ввод

4. Завершение   ToolRegistry.close_all()
                → plugin.close() — освобождение ресурсов
```

---

## Сервисы (services)

При инициализации плагин получает словарь `services` — общие хранилища данных:

```python
def initialize(self, config=None, services=None) -> None:
    self.config = config
    self.services = services or {}
```

Доступные ключи в `services`:

| Ключ | Тип | Назначение |
|------|-----|-----------|
| `config` | `AppConfig` | Конфигурация приложения |
| `pending_questions` | `Dict[str, Future]` | Ожидающие ответа вопросы (AskUser) |
| `recent_messages` | `Dict[int, List[int]]` | Последние message_id по чатам |
| `task_store` | `Dict` | Хранилище задач |
| `scheduler_tasks` | `Dict` | Запланированные задачи |
| `user_tasks` | `Dict[int, set]` | Задачи по пользователям |

`DialogMixin` использует `services` для хранения состояния диалогов (ключ `_dialog_mixin_{id(self)}`). Не трогайте этот ключ.

---

## Регистрация и автозагрузка

Плагины загружаются автоматически из `agent/plugins/`. Правила:

- Файл должен быть `*.py` в каталоге `agent/plugins/`.
- Файлы `__init__.py` и `base.py` исключены.
- Класс должен наследовать `ToolPlugin` (напрямую или через `DialogMixin`).
- Имя файла не влияет на имя инструмента — оно берётся из `get_spec().name`.
- Если в одном файле несколько классов-потомков `ToolPlugin`, все будут зарегистрированы.

**Имя инструмента в реестре:** `{function_prefix}.{spec.name}`. По умолчанию `function_prefix` = имя класса. Пример: `TaskManagementTool.task_management`.

---

## Обработка ошибок

### В execute()

Всегда возвращайте `{"success": False, "error": "..."}` вместо исключений:

```python
async def execute(self, args, ctx):
    try:
        result = await self._do_work(args)
        return {"success": True, "output": result}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {"success": False, "error": f"Internal error: {e}"}
```

### В обработчиках шагов DialogMixin

`DialogMixin.handle_message()` автоматически оборачивает вызов обработчика шага в `try/except`:
- При ошибке диалог сбрасывается.
- Пользователю отправляется «Ошибка в диалоге, попробуйте заново.»

Вы можете обрабатывать ошибки в шагах самостоятельно для более детальных сообщений.

### В Telegram-командах

Обёртка `_cmd_wrap` в bot.py ловит исключения и отправляет «Ошибка при выполнении команды плагина.» Для лучшего UX обрабатывайте ошибки в хендлере.

---

## Чеклист для нового плагина

- [ ] Файл в `agent/plugins/` (не `base.py`, не `__init__.py`)
- [ ] Класс наследует `DialogMixin, ToolPlugin` (DialogMixin перед ToolPlugin в MRO)
- [ ] Реализован `get_spec()` с корректной JSON Schema
- [ ] Реализован `execute()` с форматом `{"success": bool, "output"|"error": str}`
- [ ] Реализован `get_menu_label()` и `get_menu_actions()` для двухуровневого меню
- [ ] `get_commands()` возвращает `self._dialog_callback_commands()`
- [ ] `callback_handlers()` определяет обработчики для всех action из `get_menu_actions()`
- [ ] Если есть диалоги — реализован `dialog_steps()` (callable или dict с `"message"` / `"callback"`)
- [ ] Если есть диалоги — приглашения используют `reply_markup=self.cancel_markup()`
- [ ] Кнопки создаются через `dialog_button()` (внутри шага) или `action_button()` (автономные)
- [ ] Если принимается нетекстовый контент — переопределён `extra_message_filters()`
- [ ] Если операция долгая — увеличен `DIALOG_TIMEOUT`
- [ ] Нет прямых зависимостей от `bot.py` — только через абстракции `ToolPlugin`

---

## Примеры

### Пример 1: Простой плагин без диалогов (TextDocumentQATool)

Плагин работает только через команды и вызовы агента. Не нужен `DialogMixin`.

```python
class TextDocumentQATool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(name="text_document_qa", ...)

    def get_commands(self):
        return [
            {"command": "list_documents", "description": "...", "handler": self.cmd_list, ...},
            {"command": "upload_document", "description": "...", "handler": self.cmd_upload, ...},
        ]

    async def execute(self, args, ctx):
        action = args.get("action")
        if action == "list":
            ...
        if action == "upload":
            ...
```

### Пример 2: Диалог + автономные кнопки (TaskManagementTool)

Плагин сочетает диалог (добавление задачи) и inline-меню (управление списком).
Все действия доступны через двухуровневое меню (без Telegram-команд).

```python
class TaskManagementTool(DialogMixin, ToolPlugin):
    def get_menu_label(self):
        return "Задачи"

    def get_menu_actions(self):
        return [
            {"label": "Список", "action": "refresh"},
            {"label": "Добавить", "action": "add"},
        ]

    def get_commands(self):
        return self._dialog_callback_commands()

    def dialog_steps(self):
        return {"wait_text": self._on_add_text}

    def callback_handlers(self):
        return {
            "add": self._cb_start_add,       # Кнопка «Добавить» -> запуск диалога
            "refresh": self._cb_refresh,      # Обновить список
            "del": self._cb_del,              # Удалить задачу
            "next": self._cb_next,            # Переключить статус
            "view": self._cb_view,            # Просмотр задачи
        }

    def _build_tasks_menu(self, user_id):
        rows = [
            [self.action_button("Добавить", "add")],
        ]
        for t in tasks:
            tid = t["id"]
            rows.append([
                self.action_button("Статус", "next", tid),
                self.action_button("Удалить", "del", tid),
            ])
        rows.append([self.action_button("Обновить", "refresh")])
        return "Задачи:", InlineKeyboardMarkup(rows)

    async def _cb_start_add(self, update, context, payload):
        chat_id = update.callback_query.message.chat_id
        self.start_dialog(chat_id, "wait_text", data={"mode": "add"})
        await update.callback_query.message.reply_text(
            "Отправьте текст задачи...",
            reply_markup=self.cancel_markup(),
        )

    async def _cb_refresh(self, update, context, payload):
        user_id = update.callback_query.from_user.id
        text, markup = self._build_tasks_menu(user_id)
        await update.callback_query.edit_message_text(text, reply_markup=markup)

    async def _on_add_text(self, update, context):
        msg = update.effective_message
        chat_id = update.effective_chat.id
        # Обработка текста...
        self.end_dialog(chat_id)
        await msg.reply_text("Задача добавлена!")
```

### Пример 3: Многошаговый диалог с медиа (HaiperImageToVideoTool)

Два шага: сначала изображение, потом текстовый промпт. Запускается из подменю.

```python
class HaiperImageToVideoTool(DialogMixin, ToolPlugin):
    DIALOG_TIMEOUT = 60 * 60  # 1 час — генерация видео долгая

    def get_menu_label(self):
        return "Haiper (видео)"

    def get_menu_actions(self):
        return [{"label": "Создать видео", "action": "start"}]

    def get_commands(self):
        return self._dialog_callback_commands()

    def dialog_steps(self):
        return {
            "wait_image": self._on_image_step,
            "wait_prompt": self._on_prompt_step,
        }

    def callback_handlers(self):
        return {"start": self._cb_start}

    def extra_message_filters(self):
        return filters.PHOTO | filters.Document.IMAGE

    def step_hint(self, step):
        if step == "wait_image":
            return "Жду изображение."
        return None

    async def _cb_start(self, update, context, payload):
        query = update.callback_query
        chat_id = query.message.chat_id
        self.start_dialog(chat_id, "wait_image")
        await query.message.reply_text(
            "Отправьте изображение...",
            reply_markup=self.cancel_markup(),
        )

    async def _on_image_step(self, update, context):
        # Проверка: пришло ли фото
        if not has_photo:
            await msg.reply_text(self.step_hint("wait_image"))
            return
        # Скачивание и сохранение...
        self.set_step(chat_id, "wait_prompt", data={"image_path": path})
        await msg.reply_text("Теперь отправьте промпт...")

    async def _on_prompt_step(self, update, context):
        if not msg.text:
            await msg.reply_text("Жду текст.")
            return
        # Генерация видео...
        self.end_dialog(chat_id)
```

### Пример 4: Полный шаблон плагина с меню, диалогом и кнопками

```python
from __future__ import annotations

from typing import Any, Callable, Dict, List

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from agent.plugins.base import DialogMixin, ToolPlugin
from agent.tooling.spec import ToolSpec


class MyDialogPlugin(DialogMixin, ToolPlugin):
    """Полный шаблон: меню, кнопки, диалог с выбором режима."""

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="my_dialog_tool",
            description="Описание для агента.",
            parameters={
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
        )

    # -- меню плагина --

    def get_menu_label(self):
        return "Мой плагин"

    def get_menu_actions(self):
        return [
            {"label": "Запустить диалог", "action": "start"},
            {"label": "Инфо", "action": "info"},
        ]

    def get_commands(self) -> List[Dict[str, Any]]:
        return self._dialog_callback_commands()

    # -- кнопки и диалог --

    def dialog_steps(self):
        return {
            "choose_mode": {
                "message": self._on_mode_text,
                "callback": self._on_mode_button,
            },
            "wait_input": self._on_input,
        }

    def callback_handlers(self) -> Dict[str, Callable]:
        return {
            "start": self._cb_start,
            "info": self._cb_info,
        }

    # -- callback handlers (из подменю) --

    async def _cb_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        chat_id = query.message.chat_id if query and query.message else 0
        user_id = query.from_user.id if query and query.from_user else 0
        self.start_dialog(chat_id, "choose_mode", user_id=user_id)

        keyboard = InlineKeyboardMarkup([
            [self.dialog_button("Быстрый", "fast"),
             self.dialog_button("Подробный", "detailed")],
        ])
        if query and query.message:
            await query.message.reply_text(
                "Выберите режим (кнопкой или текстом: fast/detailed):",
                reply_markup=keyboard,
            )

    async def _cb_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        if query:
            await query.answer("Информация о плагине.", show_alert=True)

    # -- step handlers --

    async def _on_mode_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mode = self.parse_callback_payload(update)  # "fast" или "detailed"
        chat_id = update.callback_query.message.chat_id
        self.set_step(chat_id, "wait_input", data={"mode": mode})
        await update.callback_query.message.reply_text(
            "Теперь отправьте данные.",
            reply_markup=self.cancel_markup(),
        )

    async def _on_mode_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mode = (update.effective_message.text or "").strip().lower()
        if mode not in ("fast", "detailed"):
            await update.effective_message.reply_text("Выберите fast или detailed.")
            return
        chat_id = update.effective_chat.id
        self.set_step(chat_id, "wait_input", data={"mode": mode})
        await update.effective_message.reply_text(
            "Теперь отправьте данные.",
            reply_markup=self.cancel_markup(),
        )

    async def _on_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat_id = update.effective_chat.id
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("Жду текстовый ввод.")
            return
        result = await self.execute({"input": text}, {"chat_id": chat_id})
        self.end_dialog(chat_id)
        if result.get("success"):
            await msg.reply_text(f"Готово: {result['output']}")
        else:
            await msg.reply_text(f"Ошибка: {result['error']}")

    # -- execute (вызывается агентом) --

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        value = args.get("input", "")
        return {"success": True, "output": value.upper()}
```
