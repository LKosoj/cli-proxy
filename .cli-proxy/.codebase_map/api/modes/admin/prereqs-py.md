# API Spec: `modes/admin/prereqs.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class PrereqTool` (line 29)
*Одна строка manifest'а: CLI-имя + пакет по пакет-менеджеру.*

### `class PrereqsReport` (line 86)
- `def to_dict()` (line 94)

## Symbols
- `def prereqs_command(tools)` (line 115)
  - *Shell-команда, проверяющая наличие каждого CLI через `command -v`.*
- `def parse_prereqs_output(text)` (line 134)
  - *Парсит вывод prereqs_command в dict: tool_name → present?*
- `def evaluate_prereqs(presence)` (line 183)
  - *По результату check + info о distro составить PrereqsReport.*
- `def generate_bootstrap_script(report)` (line 218)
  - *Идемпотентный shell-скрипт, устанавливающий missing пакеты через детектированный*
