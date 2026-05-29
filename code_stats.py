#!/usr/bin/env python3
"""
Скрипт для подсчета объема кодовой базы проекта.
Выводит статистику по типам файлов: количество файлов, строки, размер.
"""

import os
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Set


@dataclass
class FileStats:
    """Статистика по файлу."""
    path: str
    lines: int
    code_lines: int
    size_bytes: int


@dataclass
class TypeStats:
    """Агрегированная статистика по типу файлов."""
    file_count: int = 0
    total_lines: int = 0
    total_code_lines: int = 0
    total_size: int = 0


def get_exclude_patterns() -> Set[str]:
    """Возвращает набор паттернов для исключения."""
    return {
        '.venv',
        '__pycache__',
        '.git',
        'logs',
        '.cli-proxy',
        '.mypy_cache',
        'session_ticks',
        'node_modules',
        'dist',
        'build',
    }


def get_file_extensions() -> Dict[str, List[str]]:
    """Возвращает маппинг категорий на расширения файлов."""
    return {
        'Python': ['.py'],
        'JavaScript/TypeScript': ['.js', '.ts', '.tsx'],
        'YAML/JSON': ['.yaml', '.yml', '.json'],
        'Shell': ['.sh', '.bash'],
        'Markdown': ['.md', '.rst'],
    }


def count_lines(file_path: Path) -> tuple[int, int]:
    """
    Подсчитывает общее количество строк и строк кода (без комментариев и пустых).

    Returns:
        Кортеж (total_lines, code_lines)
    """
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        total = len(lines)
        code = 0

        ext = file_path.suffix.lower()
        in_multiline_comment = False

        for line in lines:
            stripped = line.strip()

            # Пропускаем пустые строки
            if not stripped:
                continue

            # Обработка многострочных комментариев для Python
            if ext == '.py':
                if '"""' in stripped or "'''" in stripped:
                    quote = '"""' if '"""' in stripped else "'''"
                    count = stripped.count(quote)
                    if count % 2 == 1:
                        in_multiline_comment = not in_multiline_comment
                    continue
                if in_multiline_comment:
                    continue
                if stripped.startswith('#'):
                    continue

            # Обработка комментариев для JS/TS
            elif ext in ['.js', '.ts', '.tsx']:
                if stripped.startswith('//'):
                    continue
                if '/*' in stripped and '*/' in stripped:
                    continue
                if '/*' in stripped:
                    in_multiline_comment = True
                    continue
                if in_multiline_comment:
                    if '*/' in stripped:
                        in_multiline_comment = False
                    continue

            # Обработка комментариев для YAML
            elif ext in ['.yaml', '.yml']:
                if stripped.startswith('#'):
                    continue

            # Обработка комментариев для JSON (технически не поддерживает, но на всякий случай)
            elif ext == '.json':
                pass  # JSON не поддерживает комментарии

            # Обработка комментариев для Shell
            elif ext in ['.sh', '.bash']:
                if stripped.startswith('#'):
                    continue

            code += 1

        return total, code

    except (IOError, OSError):
        return 0, 0


def should_exclude(path: Path, base_path: Path) -> bool:
    """Проверяет, должен ли файл быть исключен из анализа."""
    try:
        rel_path = path.relative_to(base_path)
        parts = rel_path.parts

        exclude_patterns = get_exclude_patterns()

        # Проверяем каждую часть пути
        for part in parts:
            if part in exclude_patterns:
                return True

        # Исключаем лог-файлы
        if path.suffix == '.log' or path.name.endswith('.log.1'):
            return True

        # Исключаем файлы в директориях tests для "основного" подсчета
        if 'tests' in parts and path.parent.name == 'tests':
            pass  # Будет обработано отдельно

        return False

    except ValueError:
        return False


def collect_files(base_path: Path, extensions: List[str], exclude_tests: bool = False) -> List[Path]:
    """Собирает файлы с указанными расширениями."""
    files = []

    for root, dirs, filenames in os.walk(base_path):
        root_path = Path(root)

        # Фильтрация директорий
        exclude_patterns = get_exclude_patterns()
        dirs[:] = [d for d in dirs if d not in exclude_patterns]

        for filename in filenames:
            file_path = root_path / filename

            # Проверяем расширение
            if file_path.suffix.lower() not in extensions:
                continue

            # Исключаем тесты если нужно
            if exclude_tests and 'tests' in file_path.parts:
                continue

            # Исключаем логи
            if filename.endswith('.log') or filename.endswith('.log.1'):
                continue

            # Исключаем session_ticks
            if 'session_ticks' in file_path.parts:
                continue

            # Исключаем .cli-proxy
            if '.cli-proxy' in file_path.parts:
                continue

            files.append(file_path)

    return files


def analyze_files(files: List[Path]) -> tuple[int, int, int]:
    """
    Анализирует файлы и возвращает статистику.

    Returns:
        Кортеж (file_count, total_lines, total_size_bytes)
    """
    total_lines = 0
    total_code_lines = 0
    total_size = 0

    for file_path in files:
        try:
            size = file_path.stat().st_size
            lines, code_lines = count_lines(file_path)

            total_lines += lines
            total_code_lines += code_lines
            total_size += size
        except (OSError, IOError):
            continue

    return len(files), total_lines, total_code_lines, total_size


def format_size(size_bytes: int) -> str:
    """Форматирует размер в человекочитаемый вид."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes} B"


def print_stats(
    category: str,
    file_count: int,
    total_lines: int,
    code_lines: int,
    size_bytes: int,
    show_percentage: bool = False,
    total_size: int = 0
):
    """Выводит статистику по категории."""
    size_str = format_size(size_bytes)
    percentage = ""
    if show_percentage and total_size > 0:
        pct = (size_bytes / total_size) * 100 if total_size > 0 else 0
        percentage = f" ({pct:.1f}%)"

    print(f"{category:<25} {file_count:>6} файлов  {total_lines:>8} строк  {code_lines:>8} кода  {size_str:>10}{percentage}")


def main():
    parser = argparse.ArgumentParser(
        description='Подсчет объема кодовой базы проекта'
    )
    parser.add_argument(
        'path',
        nargs='?',
        default='.',
        help='Путь к проекту (по умолчанию: текущая директория)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Показать подробную статистику по файлам'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Вывод в формате JSON'
    )

    args = parser.parse_args()

    base_path = Path(args.path).resolve()

    if not base_path.exists():
        print(f"Ошибка: путь '{base_path}' не существует")
        return 1

    if not base_path.is_dir():
        print(f"Ошибка: '{base_path}' не является директорией")
        return 1

    extensions_map = get_file_extensions()
    results = {}

    # Собираем статистику по каждой категории
    for category, extensions in extensions_map.items():
        # Для Python разделяем основной код и тесты
        if category == 'Python':
            # Основной код (без тестов)
            main_files = collect_files(base_path, extensions, exclude_tests=True)
            file_count, total_lines, code_lines, size = analyze_files(main_files)
            results[f'{category} (main)'] = {
                'files': file_count,
                'lines': total_lines,
                'code_lines': code_lines,
                'size': size
            }

            # Тесты
            test_files = [f for f in collect_files(base_path, extensions) if 'tests' in f.parts]
            file_count, total_lines, code_lines, size = analyze_files(test_files)
            results[f'{category} (tests)'] = {
                'files': file_count,
                'lines': total_lines,
                'code_lines': code_lines,
                'size': size
            }
        else:
            files = collect_files(base_path, extensions)
            file_count, total_lines, code_lines, size = analyze_files(files)
            results[category] = {
                'files': file_count,
                'lines': total_lines,
                'code_lines': code_lines,
                'size': size
            }

    # Считаем итоги
    total_files = sum(r['files'] for r in results.values())
    total_lines = sum(r['lines'] for r in results.values())
    total_code_lines = sum(r['code_lines'] for r in results.values())
    total_size = sum(r['size'] for r in results.values())

    if args.json:
        import json
        output = {
            'categories': results,
            'total': {
                'files': total_files,
                'lines': total_lines,
                'code_lines': total_code_lines,
                'size': total_size,
                'size_formatted': format_size(total_size)
            }
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print("=" * 80)
        print(f"Статистика кодовой базы: {base_path}")
        print("=" * 80)
        print()
        print(f"{'Категория':<25} {'Файлов':>10} {'Строк':>12} {'Кода':>12} {'Размер':>12}")
        print("-" * 80)

        for category, stats in results.items():
            print_stats(
                category,
                stats['files'],
                stats['lines'],
                stats['code_lines'],
                stats['size'],
                show_percentage=True,
                total_size=total_size
            )

        print("-" * 80)
        print_stats(
            'ИТОГО',
            total_files,
            total_lines,
            total_code_lines,
            total_size
        )
        print("=" * 80)

        if args.verbose:
            print("\nПодробная статистика по файлам:")
            print("-" * 80)

            for category, extensions in extensions_map.items():
                files = collect_files(base_path, extensions)
                if category == 'Python':
                    # Разделяем на main и tests
                    main_files = [f for f in files if 'tests' not in f.parts]
                    test_files = [f for f in files if 'tests' in f.parts]

                    if main_files:
                        print(f"\n{category} (main):")
                        for f in sorted(main_files, key=lambda x: x.stat().st_size, reverse=True)[:10]:
                            try:
                                size = f.stat().st_size
                                lines, _ = count_lines(f)
                                print(f"  {format_size(size):>8}  {lines:>6} строк  {f.relative_to(base_path)}")
                            except OSError:
                                pass

                    if test_files:
                        print(f"\n{category} (tests):")
                        for f in sorted(test_files, key=lambda x: x.stat().st_size, reverse=True)[:10]:
                            try:
                                size = f.stat().st_size
                                lines, _ = count_lines(f)
                                print(f"  {format_size(size):>8}  {lines:>6} строк  {f.relative_to(base_path)}")
                            except OSError:
                                pass
                else:
                    if files:
                        print(f"\n{category}:")
                        for f in sorted(files, key=lambda x: x.stat().st_size, reverse=True)[:10]:
                            try:
                                size = f.stat().st_size
                                lines, _ = count_lines(f)
                                print(f"  {format_size(size):>8}  {lines:>6} строк  {f.relative_to(base_path)}")
                            except OSError:
                                pass

    return 0


if __name__ == '__main__':
    exit(main())
