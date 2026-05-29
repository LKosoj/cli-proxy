# API Spec: `tg/markdown.py`

Generated: 2026-04-27T22:43:23Z

## Symbols
- `def escape_markdown_v2_all(text)` (line 33)
  - *Escape *all* Telegram MarkdownV2 special characters.*
- `def to_telegram_entities(text)` (line 71)
  - *Convert Markdown-ish text to Telegram plain text + entities.*
- `def split_telegram_entities(text, entities)` (line 90)
  - *Split Telegram text/entities into chunks under the UTF-16 limit.*
- `def utf16_length(text)` (line 116)
- `def to_markdown_v2(text)` (line 127)
  - *Convert/escape text for Telegram MarkdownV2.*
