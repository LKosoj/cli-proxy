# agent/plugins/movie_info

Модуль предоставляет инструмент для получения информации о фильмах через API The Movie Database (TMDb).  
Поддерживает два действия: получение фильмов, которые сейчас в прокате, и поиск по жанрам через discover.  
Инструмент интегрируется в систему плагинов с поддержкой асинхронного выполнения и валидацией параметров через ToolSpec.

Ключевые структуры данных  
ToolSpec — Описание интерфейса инструмента, включая параметры, типы и поведение.

---
class MovieInfoTool  
Плагин для получения информации о фильмах из TMDb (The Movie Database)  
Поля  
TMDB_BASE_URL — Базовый URL для обращения к TMDb API  
Методы  
get_source_name() -> str — Возвращает имя источника данных — "TMDb"  
get_spec() -> ToolSpec — Возвращает спецификацию инструмента, включая параметры и ограничения  
execute(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any] — Асинхронно выполняет запрос к TMDb и возвращает отформатированный результат  
_fetch_sync(key: str, action: str, language: str, region: str, genre_id: Optional[int]) -> List[Dict[str, Any]] — Синхронно выполняет HTTP-запрос к TMDb и возвращает список фильмов

---
get_source_name() -> str  
Возвращает имя источника данных, используемого плагином.  
Возвращает  
Имя источника — "TMDb"

---
get_spec() -> ToolSpec  
Возвращает спецификацию инструмента, включая параметры, типы и поведение.  
Возвращает  
Объект ToolSpec с описанием инструмента.

---
execute(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]  
Асинхронно выполняет запрос к TMDb и возвращает отформатированный результат.  
Аргументы  
args — Параметры выполнения: action, genre_id, count, language, region  
ctx — Контекст выполнения (не используется)  
Возвращает  
Словарь с результатом: success, error или output  
Исключения  
Любые исключения обрабатываются внутри метода и возвращаются как часть результата с success=False

---
_fetch_sync(key: str, action: str, language: str, region: str, genre_id: Optional[int]) -> List[Dict[str, Any]]  
Синхронно выполняет HTTP-запрос к TMDb и возвращает список фильмов.  
Аргументы  
key — API-ключ для TMDb  
action — Тип действия: "now_playing" или "discover"  
language — Язык локализации (например, ru-RU)  
region — Регион (например, RU)  
genre_id — Опциональный фильтр по жанру  
Возвращает  
Список фильмов в формате JSON  
Исключения  
ValueError — При некорректном значении action  
RuntimeError — При ошибке HTTP-запроса или ответа от API
