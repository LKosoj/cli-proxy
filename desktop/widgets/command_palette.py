from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from i18n import t


@dataclass(frozen=True, slots=True)
class CommandPaletteItem:
    """Описывает пункт command palette."""

    command_id: str
    title: str
    subtitle: str = ""
    keywords: tuple[str, ...] = ()
    section: str = "General"
    shortcut: str = ""


class CommandPaletteDialog(QDialog):
    """Компактная палитра команд для быстрой навигации и действий."""

    commandTriggered = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setObjectName("command_palette")

        self._items: List[CommandPaletteItem] = []
        self._filtered_ids: List[str] = []
        self._recent_ids: List[str] = []
        self._setup_ui()
        self.retranslate_ui("ru")

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self.hint_label = QLabel()
        self.hint_label.setObjectName("command_palette_hint")
        root.addWidget(self.hint_label)

        self.search_input = QLineEdit()
        self.search_input.textChanged.connect(self._apply_filter)
        self.search_input.returnPressed.connect(self._trigger_selected)
        root.addWidget(self.search_input)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._trigger_selected())
        root.addWidget(self.list_widget, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.footer_esc_label = QLabel()
        footer.addWidget(self.footer_esc_label)
        footer.addStretch(1)
        self.footer_reopen_label = QLabel()
        footer.addWidget(self.footer_reopen_label)
        root.addLayout(footer)

    def retranslate_ui(self, lang: str) -> None:
        self.setWindowTitle(t("desktop.palette.title", lang))
        self.hint_label.setText(t("desktop.palette.hint", lang))
        self.search_input.setPlaceholderText(t("desktop.palette.search_placeholder", lang))
        self.footer_esc_label.setText(t("desktop.palette.footer_esc", lang))
        self.footer_reopen_label.setText(t("desktop.palette.footer_reopen", lang))

    def set_commands(self, items: Iterable[CommandPaletteItem]) -> None:
        self._items = list(items)
        self._apply_filter(self.search_input.text())

    def set_recent_commands(self, recent_ids: Iterable[str]) -> None:
        seen = set()
        normalized: List[str] = []
        for item in recent_ids:
            cid = str(item or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            normalized.append(cid)
        self._recent_ids = normalized
        self._apply_filter(self.search_input.text())

    def open_with_query(self, query: str = "") -> None:
        self.search_input.setText(str(query or ""))
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_input.selectAll()
        self._apply_filter(self.search_input.text())
        self.show()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._trigger_selected()
            return
        if key == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def _apply_filter(self, query: str) -> None:
        self.list_widget.clear()
        q = str(query or "").strip().lower()
        self._filtered_ids = []
        recent_rank = {cid: idx for idx, cid in enumerate(self._recent_ids)}
        ranked = sorted(
            self._items,
            key=lambda item: (recent_rank.get(item.command_id, 10_000), item.title.lower()),
        )

        visible_items: List[CommandPaletteItem] = []
        for item in ranked:
            haystack = " ".join(
                [item.title, item.subtitle, item.section, item.shortcut, " ".join(item.keywords), item.command_id]
            ).lower()
            if q and q not in haystack:
                continue
            visible_items.append(item)

        # Sections keep the order in which they first appear in the source command
        # list, so ordering stays correct regardless of the localized section name.
        section_order: List[str] = []
        for item in self._items:
            if item.section not in section_order:
                section_order.append(item.section)
        by_section: dict[str, List[CommandPaletteItem]] = {}
        for item in visible_items:
            by_section.setdefault(item.section, []).append(item)

        ordered_sections = [name for name in section_order if name in by_section]
        ordered_sections.extend(name for name in by_section if name not in ordered_sections)

        for section in ordered_sections:
            header = QListWidgetItem(section)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.UserRole, "")
            self.list_widget.addItem(header)
            for item in by_section[section]:
                row = QListWidgetItem()
                row_widget = self._make_row_widget(item, bool(q), item.command_id in recent_rank)
                row.setSizeHint(row_widget.sizeHint())
                row.setData(Qt.ItemDataRole.UserRole, item.command_id)
                if item.subtitle:
                    row.setToolTip(item.subtitle)
                self.list_widget.addItem(row)
                self.list_widget.setItemWidget(row, row_widget)
                self._filtered_ids.append(item.command_id)

        self._select_first_command()

    def _make_row_widget(self, item: CommandPaletteItem, has_query: bool, is_recent: bool) -> QWidget:
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(6, 4, 6, 4)
        row_layout.setSpacing(8)

        title = item.title
        if is_recent and not has_query:
            title = f"• {title}"
        title_label = QLabel(title)
        title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row_layout.addWidget(title_label, 1)

        if item.shortcut:
            shortcut_label = QLabel(item.shortcut)
            shortcut_label.setObjectName("command_palette_shortcut")
            shortcut_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(shortcut_label)

        return row_widget

    def _select_first_command(self) -> None:
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item is None:
                continue
            command_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if command_id:
                self.list_widget.setCurrentRow(i)
                return
        self.list_widget.setCurrentRow(-1)

    def _find_nearest_command_item(self, start_row: int) -> Optional[QListWidgetItem]:
        if start_row < 0:
            return None
        for i in range(start_row, self.list_widget.count()):
            item = self.list_widget.item(i)
            if item is None:
                continue
            if str(item.data(Qt.ItemDataRole.UserRole) or ""):
                return item
        for i in range(start_row - 1, -1, -1):
            item = self.list_widget.item(i)
            if item is None:
                continue
            if str(item.data(Qt.ItemDataRole.UserRole) or ""):
                return item
        return None

    def _trigger_selected(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        if not str(item.data(Qt.ItemDataRole.UserRole) or ""):
            item = self._find_nearest_command_item(self.list_widget.currentRow())
            if item is None:
                return
            self.list_widget.setCurrentItem(item)
        command_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not command_id:
            return
        self.commandTriggered.emit(command_id)
        self.accept()
