import os
import logging
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils import is_within_root


def prepare_dirs(
    dirs_menu: Dict[int, list],
    dirs_base: Dict[int, str],
    dirs_page: Dict[int, int],
    dirs_root: Dict[int, str],
    chat_id: int,
    base: str,
    allow_empty: bool = False,
) -> Optional[str]:
    root = dirs_root.get(chat_id, base)
    if not is_within_root(base, root):
        return "Нельзя выйти за пределы корневого каталога."
    try:
        entries = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return f"Ошибка чтения каталога: {e}"
    if not entries:
        if allow_empty:
            dirs_base[chat_id] = base
            dirs_page[chat_id] = 0
            dirs_menu[chat_id] = []
            return None
        return "Подкаталогов нет. Добавьте хотя бы один каталог и попробуйте снова."
    dirs_base[chat_id] = base
    dirs_page[chat_id] = 0
    full_paths = [os.path.join(base, d) for d in entries]
    dirs_menu[chat_id] = full_paths
    return None


def build_dirs_keyboard(
    dirs_menu: Dict[int, list],
    dirs_base: Dict[int, str],
    dirs_page: Dict[int, int],
    short_label,
    chat_id: int,
    base: str,
    page: int,
) -> InlineKeyboardMarkup:
    dirs_base[chat_id] = base
    dirs_page[chat_id] = page
    items = dirs_menu.get(chat_id, [])
    page_size = 10
    start = page * page_size
    end = start + page_size
    rows = []
    for i, full in enumerate(items[start:end], start=start):
        label = short_label(os.path.basename(full))
        rows.append([InlineKeyboardButton(label, callback_data=f"dir_pick:{i}")])
    nav = []
    parent = os.path.dirname(base.rstrip(os.sep))
    if parent and parent != base:
        nav.append(InlineKeyboardButton("⬆️ Вверх", callback_data="dir_up"))
    if start > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"dir_page:{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("▶️ Далее", callback_data=f"dir_page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✅ Использовать этот каталог", callback_data="dir_use_current")])
    rows.append([InlineKeyboardButton("📁 Создать каталог", callback_data="dir_create")])
    rows.append([InlineKeyboardButton("📦 git clone", callback_data="dir_git_clone")])
    rows.append([InlineKeyboardButton("✏️ Ввести путь", callback_data="dir_enter")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")])
    return InlineKeyboardMarkup(rows)
