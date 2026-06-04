import os
import logging
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.telegram_ui_scope import TelegramUiKey
from i18n import t
from utils.paths import is_within_root


def prepare_dirs(
    dirs_menu: Dict[TelegramUiKey, list],
    dirs_base: Dict[TelegramUiKey, str],
    dirs_page: Dict[TelegramUiKey, int],
    dirs_root: Dict[TelegramUiKey, str],
    ui_key: TelegramUiKey,
    base: str,
    allow_empty: bool = False,
    include_files: bool = False,
) -> Optional[str]:
    root = dirs_root.get(ui_key, base)
    if not is_within_root(base, root):
        return "Нельзя выйти за пределы корневого каталога."
    try:
        names = list(os.listdir(base))
        if include_files:
            entries = []
            for n in names:
                p = os.path.join(base, n)
                if os.path.isdir(p) or os.path.isfile(p):
                    entries.append(n)
            entries.sort(key=lambda n: (not os.path.isdir(os.path.join(base, n)), n.lower()))
        else:
            entries = sorted(d for d in names if os.path.isdir(os.path.join(base, d)))
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return f"Ошибка чтения каталога: {e}"
    if not entries:
        if allow_empty:
            dirs_base[ui_key] = base
            dirs_page[ui_key] = 0
            dirs_menu[ui_key] = []
            return None
        return "Подкаталогов нет. Добавьте хотя бы один каталог и попробуйте снова."
    dirs_base[ui_key] = base
    dirs_page[ui_key] = 0
    full_paths = [os.path.join(base, d) for d in entries]
    dirs_menu[ui_key] = full_paths
    return None


def build_dirs_keyboard(
    dirs_menu: Dict[TelegramUiKey, list],
    dirs_base: Dict[TelegramUiKey, str],
    dirs_page: Dict[TelegramUiKey, int],
    short_label,
    ui_key: TelegramUiKey,
    base: str,
    page: int,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    dirs_base[ui_key] = base
    dirs_page[ui_key] = page
    items = dirs_menu.get(ui_key, [])
    page_size = 10
    start = page * page_size
    end = start + page_size
    rows = []
    for i, full in enumerate(items[start:end], start=start):
        base_name = os.path.basename(full)
        prefix = ""
        try:
            if os.path.isdir(full):
                prefix = "📁 "
            elif os.path.isfile(full):
                prefix = "📄 "
        except Exception:
            prefix = ""
        label = prefix + short_label(base_name)
        rows.append([InlineKeyboardButton(label, callback_data=f"dir_pick:{i}")])
    nav = []
    parent = os.path.dirname(base.rstrip(os.sep))
    if parent and parent != base:
        nav.append(InlineKeyboardButton(t("msg.dirs.btn_up", lang), callback_data="dir_up"))
    if start > 0:
        nav.append(InlineKeyboardButton(t("msg.dirs.btn_prev", lang), callback_data=f"dir_page:{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton(t("msg.dirs.btn_next", lang), callback_data=f"dir_page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(t("msg.dirs.btn_use_current", lang), callback_data="dir_use_current")])
    rows.append([InlineKeyboardButton(t("msg.dirs.btn_create", lang), callback_data="dir_create")])
    rows.append([InlineKeyboardButton(t("msg.dirs.btn_git_clone", lang), callback_data="dir_git_clone")])
    rows.append([InlineKeyboardButton(t("msg.dirs.btn_enter", lang), callback_data="dir_enter")])
    rows.append([InlineKeyboardButton(t("msg.dirs.btn_cancel", lang), callback_data="agent_cancel")])
    return InlineKeyboardMarkup(rows)
