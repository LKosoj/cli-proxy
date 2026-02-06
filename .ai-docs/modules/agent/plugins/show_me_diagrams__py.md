# agent/plugins/show_me_diagrams

Модуль реализует инструмент для генерации и рендеринга диаграмм с использованием PlantUML. Поддерживает два режима: генерация PlantUML-кода на основе описания с помощью LLM и рендеринг существующего кода в PNG. Требует наличия `plantuml.jar` и Java в системе, а также OpenAI API-ключа для генерации кода. Результат включает путь к изображению и исходный PlantUML-код.

Ключевые структуры данных  
ToolSpec — Описание спецификации инструмента, включая параметры, имя и описание

---
ShowMeDiagramsTool.get_source_name() -> str  
Возвращает имя источника инструмента — "PlantUML".  
Возвращает  
Имя источника, используемое для атрибуции.

---
ShowMeDiagramsTool.get_spec() -> ToolSpec  
Возвращает спецификацию инструмента, включая параметры и ограничения.  
Возвращает  
Объект ToolSpec с описанием инструмента.

---
ShowMeDiagramsTool.execute(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]  
Асинхронно выполняет действие: генерацию или рендеринг диаграммы.  
Аргументы  
args — Словарь с параметрами: action, diagram_type, title, description или plantuml_code.  
ctx — Контекст выполнения (не используется).  
Возвращает  
Результат выполнения: успех, путь к PNG и PlantUML-код или ошибка.  
Исключения  
Не выбрасывает исключения напрямую, возвращает ошибку в словаре.

---
ShowMeDiagramsTool._generate_plantuml(diagram_type: str, title: str, description: str) -> str  
Асинхронно генерирует PlantUML-код с помощью OpenAI API на основе типа и описания диаграммы.  
Аргументы  
diagram_type — Тип диаграммы (например, flowchart, gantt_chart).  
title — Заголовок диаграммы.  
description — Описание содержания диаграммы.  
Возвращает  
Сгенерированный PlantUML-код.  
Исключения  
RuntimeError — Если не задан OPENAI_API_KEY.

---
ShowMeDiagramsTool._render_png_sync(plantuml_code: str) -> str  
Синхронно рендерит PlantUML-код в PNG с помощью plantuml.jar через команду Java.  
Аргументы  
plantuml_code — Текст PlantUML-кода для рендеринга.  
Возвращает  
Путь к созданному PNG-файлу.  
Исключения  
RuntimeError — При ошибке выполнения Java или отсутствии выходного файла.
