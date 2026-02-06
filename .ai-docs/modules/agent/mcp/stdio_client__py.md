# agent/mcp/stdio_client

```markdown
Клиент для взаимодействия с MCP (Model Control Protocol) сервером через стандартные потоки ввода-вывода.  
Позволяет запускать внешний процесс MCP-сервера, управлять им и вызывать инструменты (tools) через JSON-RPC.  
Поддерживает инициализацию по протоколу MCP, получение списка инструментов и их вызов с передачей аргументов.

Ключевые структуры данных  
MCPToolInfo — Информация об инструменте: имя, описание и схема входных параметров.

---
MCPToolInfo(name: str, description: str, input_schema: Dict[str, Any])  
Информация об инструменте MCP.  
Аргументы  
name — Имя инструмента  
description — Описание инструмента  
input_schema — JSON-схема входных параметров инструмента  
Возвращает  
Новый экземпляр MCPToolInfo

---
def _now_ms() -> int  
Возвращает текущее время в миллисекундах с эпохи Unix.  
Возвращает  
Текущее время в миллисекундах

---
def __init__(self, *, name: str, cmd: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, timeout_ms: int = 30000, protocol_version: str = "2024-11-05") -> None  
Инициализирует клиента для MCP-сервера.  
Аргументы  
name — Имя сервера (для логирования)  
cmd — Команда для запуска сервера  
cwd — Рабочая директория процесса  
env — Переменные окружения, добавляемые к процессу  
timeout_ms — Таймаут ожидания ответа от сервера в миллисекундах  
protocol_version — Версия протокола MCP

---
async def start(self) -> None  
Запускает процесс MCP-сервера и устанавливает соединение.  
Исключения  
ValueError — Если команда запуска пуста

---
async def stop(self) -> None  
Останавливает процесс MCP-сервера и завершает соединение.

---
async def list_tools(self) -> List[MCPToolInfo]  
Получает список доступных инструментов от сервера.  
Возвращает  
Список объектов MCPToolInfo с информацией о каждом инструменте

---
async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]  
Вызывает инструмент по имени с заданными аргументами.  
Аргументы  
tool_name — Имя вызываемого инструмента  
arguments — Аргументы вызова в формате словаря  
Возвращает  
Результат выполнения инструмента в виде словаря

---
async def _initialize(self) -> None  
Выполняет инициализацию MCP-сессии (handshake).  
Исключения  
Exception — Ошибка при инициализации (логируется, но не прерывает выполнение)

---
async def _notify(self, method: str, params: Dict[str, Any]) -> None  
Отправляет уведомление (notification) по JSON-RPC.  
Аргументы  
method — Имя метода уведомления  
params — Параметры уведомления  
Исключения  
RuntimeError — Если поток не инициализирован

---
async def _request(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]  
Отправляет JSON-RPC запрос и ожидает ответ.  
Аргументы  
method — Имя метода запроса  
params — Параметры запроса  
Возвращает  
Ответ от сервера в виде словаря или None при таймауте  
Исключения  
RuntimeError — Если поток не инициализирован  
asyncio.TimeoutError — Если превышено время ожидания ответа

---
async def _reader_loop(self) -> None  
Цикл чтения входящих сообщений из stdout процесса.  
Исключения  
Любые исключения логируются, цикл завершается при ошибках чтения или отмене задачи
```
