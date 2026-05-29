# API Spec: `code_stats.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class FileStats` (line 15)
*Статистика по файлу.*

### `class TypeStats` (line 24)
*Агрегированная статистика по типу файлов.*

## Symbols
- `def get_exclude_patterns()` (line 32)
  - *Возвращает набор паттернов для исключения.*
- `def get_file_extensions()` (line 48)
  - *Возвращает маппинг категорий на расширения файлов.*
- `def count_lines(file_path)` (line 59)
  - *Подсчитывает общее количество строк и строк кода (без комментариев и пустых).*
- `def should_exclude(path, base_path)` (line 132)
  - *Проверяет, должен ли файл быть исключен из анализа.*
- `def collect_files(base_path, extensions, exclude_tests)` (line 159)
  - *Собирает файлы с указанными расширениями.*
- `def analyze_files(files)` (line 198)
  - *Анализирует файлы и возвращает статистику.*
- `def format_size(size_bytes)` (line 223)
  - *Форматирует размер в человекочитаемый вид.*
- `def print_stats(category, file_count, total_lines, code_lines, size_bytes, show_percentage, total_size)` (line 233)
  - *Выводит статистику по категории.*
- `def main()` (line 252)
