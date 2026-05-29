from __future__ import annotations

import logging
from typing import Dict, Optional


class ThemeService:
    """Service for managing application themes and stylesheets."""

    LIGHT_THEME = {
        "bg_primary": "#ffffff",
        "bg_secondary": "#f8f9fa",
        "bg_tertiary": "#f1f2f6",
        "text_primary": "#2c3e50",
        "text_secondary": "#7f8c8d",
        "border": "#dee2e6",
        "accent": "#3498db",
        "accent_hover": "#2980b9",
        "success": "#27ae60",
        "success_hover": "#229954",
        "danger": "#e74c3c",
        "danger_hover": "#c0392b",
        "warning": "#f39c12",
        "sidebar_bg": "#34495e",
        "sidebar_text": "#ecf0f1",
        "sidebar_hover": "#3d566e",
        "sidebar_active": "#3498db",
        "chat_user_bg": "#e8f4fd",
        "chat_agent_bg": "#f2f9f4",
        "analyst_bg": "#fcf3cf",
        "analyst_border": "#f7dc6f",
        "analyst_text": "#7d6608",
    }

    DARK_THEME = {
        "bg_primary": "#1e1e1e",
        "bg_secondary": "#252526",
        "bg_tertiary": "#2d2d2d",
        "text_primary": "#e0e0e0",
        "text_secondary": "#aaaaaa",
        "border": "#333333",
        "accent": "#007acc",
        "accent_hover": "#0062a3",
        "success": "#4ec9b0",
        "success_hover": "#3da892",
        "danger": "#f44747",
        "danger_hover": "#d43737",
        "warning": "#dcdcaa",
        "sidebar_bg": "#1a1a1a",
        "sidebar_text": "#cccccc",
        "sidebar_hover": "#2a2d2e",
        "sidebar_active": "#094771",
        "chat_user_bg": "#264f78",
        "chat_agent_bg": "#2d3d32",
        "analyst_bg": "#3e3e10",
        "analyst_border": "#5e5e20",
        "analyst_text": "#dcdcaa",
    }

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._current_theme_name = "light"
        self._themes = {
            "light": self.LIGHT_THEME,
            "dark": self.DARK_THEME,
        }

    def set_theme(self, theme_name: str) -> bool:
        if theme_name in self._themes:
            self._current_theme_name = theme_name
            return True
        return False

    def get_theme_colors(self) -> Dict[str, str]:
        return self._themes.get(self._current_theme_name, self.LIGHT_THEME)

    def get_main_stylesheet(self) -> str:
        colors = self.get_theme_colors()
        font_family = "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

        return f"""
            QMainWindow, QWidget {{
                background-color: {colors['bg_primary']};
                color: {colors['text_primary']};
                font-family: {font_family};
            }}

            QFrame#sidebar {{
                background-color: {colors['sidebar_bg']};
            }}

            QFrame#sidebar QPushButton {{
                background-color: transparent;
                color: {colors['sidebar_text']};
                border: none;
                text-align: left;
                padding: 10px 12px;
                font-size: 14px;
            }}

            QFrame#sidebar QPushButton:hover {{
                background-color: {colors['sidebar_hover']};
                color: #ffffff;
            }}

            QFrame#sidebar QPushButton:checked {{
                background-color: {colors['sidebar_active']};
                color: #ffffff;
            }}

            QTextBrowser {{
                background-color: {colors['bg_primary']};
                border: 1px solid {colors['border']};
                color: {colors['text_primary']};
            }}

            QTextBrowser#history_browser {{
                border: none;
                padding: 10px;
                font-size: 14px;
            }}

            QTextEdit {{
                background-color: {colors['bg_primary']};
                border: 1px solid {colors['border']};
                color: {colors['text_primary']};
            }}

            QPlainTextEdit {{
                background-color: {colors['bg_primary']};
                border: 1px solid {colors['border']};
                color: {colors['text_primary']};
                font-family: 'Courier New', Courier, monospace;
            }}

            QTextEdit#message_input {{
                border: 1px solid {colors['border']};
                border-radius: 5px;
                background-color: {colors['bg_primary']};
                padding: 5px;
            }}

            QTabWidget::pane {{
                border: 1px solid {colors['border']};
            }}

            QTabBar::tab {{
                background: {colors['bg_secondary']};
                border: 1px solid {colors['border']};
                padding: 8px 12px;
            }}

            QTabBar::tab:selected {{
                background: {colors['bg_primary']};
                border-bottom-color: {colors['bg_primary']};
            }}

            QScrollArea {{
                border: none;
                background-color: transparent;
            }}

            QSplitter::handle {{
                background-color: {colors['border']};
            }}

            QFrame#separator, QFrame#mode_separator {{
                background-color: {colors['border']};
            }}

            QWidget#input_container {{
                background-color: {colors['bg_secondary']};
                border-top: 1px solid {colors['border']};
            }}

            QFrame#attachment_preview {{
                background-color: {colors['bg_primary']};
                border: 1px solid {colors['border']};
                border-radius: 5px;
            }}

            QPushButton#attachment_remove_btn {{
                background-color: {colors['danger']};
                color: white;
                border-radius: 8px;
                font-weight: bold;
                font-size: 10px;
                border: none;
                padding: 0;
            }}

            QPushButton#attachment_remove_btn:hover {{
                background-color: {colors['danger_hover']};
            }}

            QProgressBar#attachment_progress {{
                background-color: {colors['bg_tertiary']};
                border: none;
                border-radius: 2px;
            }}

            QProgressBar#attachment_progress::chunk {{
                background-color: {colors['success']};
                border-radius: 2px;
            }}

            QPushButton {{
                padding: 6px 12px;
                border-radius: 4px;
                border: 1px solid {colors['border']};
                background-color: {colors['bg_secondary']};
            }}

            QPushButton:hover {{
                background-color: {colors['bg_tertiary']};
            }}

            QPushButton#send_button {{
                background-color: {colors['accent']};
                color: white;
                font-weight: bold;
                border: none;
            }}

            QPushButton#send_button:hover {{
                background-color: {colors['accent_hover']};
            }}

            QPushButton#send_button:disabled {{
                background-color: {colors['border']};
            }}

            QPushButton#attach_button {{
                background-color: {colors['bg_tertiary']};
                color: {colors['text_primary']};
                font-weight: bold;
                font-size: 16px;
                border: 1px solid {colors['border']};
            }}

            QPushButton#stop_button {{
                background-color: {colors['danger']};
                color: white;
                font-weight: bold;
                border: none;
            }}

            QPushButton#stop_button:hover {{
                background-color: {colors['danger_hover']};
            }}

            QComboBox {{
                padding: 4px;
                border: 1px solid {colors['border']};
                border-radius: 4px;
                background-color: {colors['bg_primary']};
            }}

            QSpinBox, QDoubleSpinBox {{
                padding: 4px;
                border: 1px solid {colors['border']};
                border-radius: 4px;
                background-color: {colors['bg_primary']};
            }}

            QFrame#report_toolbar {{
                background-color: {colors['bg_secondary']};
                border-bottom: 1px solid {colors['border']};
            }}

            QLabel#title_label {{
                font-weight: bold;
                color: {colors['text_primary']};
            }}

            QLabel#panel_icon {{
                font-size: 18px;
            }}

            QLabel#status_label {{
                color: {colors['text_secondary']};
                font-size: 12px;
            }}

            QPushButton#export_btn {{
                background-color: {colors['success']};
                color: white;
                font-weight: bold;
            }}

            QPushButton#export_btn:hover {{
                background-color: {colors['success_hover']};
            }}

            QTreeWidget {{
                border: 1px solid {colors['border']};
                background-color: {colors['bg_primary']};
            }}

            QPushButton[class="analyst_option"] {{
                background-color: {colors['bg_primary']};
                border: 1px solid {colors['analyst_border']};
                color: {colors['analyst_text']};
                font-weight: bold;
            }}

            QPushButton[class="analyst_option"]:hover {{
                background-color: {colors['bg_tertiary']};
                border-color: {colors['accent']};
            }}

            QWidget#session_manager {{
                background-color: {colors['bg_secondary']};
                border-right: 1px solid {colors['border']};
            }}

            QWidget#git_integration {{
                background-color: {colors['bg_secondary']};
                border-left: 1px solid {colors['border']};
            }}

            QLabel#session_item_name, QLabel#git_title, QLabel#mode_menu_title, QLabel#mode_panel_cli {{
                font-weight: bold;
                color: {colors['text_primary']};
            }}

            QLabel#attachment_text_preview {{
                font-size: 10px;
                font-weight: bold;
                color: {colors['text_secondary']};
            }}

            QLabel#mode_panel_status_icon {{
                font-size: 16px;
            }}

            QLabel#mode_panel_status_text {{
                font-weight: bold;
                color: {colors['text_secondary']};
            }}

            QLabel#mode_menu_text {{
                color: {colors['text_primary']};
            }}

            QPushButton#mode_menu_button {{
                text-align: left;
                padding: 6px 10px;
            }}

            QLabel#session_item_info, QLabel#task_queue_summary, QLabel#task_item_info {{
                font-size: 10px;
                color: {colors['text_secondary']};
            }}

            QLabel#progress_title, QLabel#task_queue_title {{
                font-weight: bold;
                font-size: 11px;
                color: {colors['text_secondary']};
            }}

            QFrame#task_item_frame {{
                border: 1px solid {colors['border']};
                border-radius: 4px;
                background-color: {colors['bg_secondary']};
            }}

            QPushButton#save_btn {{
                background-color: {colors['success']};
                color: white;
                font-weight: bold;
                padding: 8px;
            }}

            QPushButton#save_btn:hover {{
                background-color: {colors['success_hover']};
            }}

            QProgressBar {{
                background-color: {colors['bg_tertiary']};
                border: none;
                border-radius: 4px;
                text-align: center;
            }}

            QProgressBar::chunk {{
                background-color: {colors['accent']};
                border-radius: 4px;
            }}
        """
