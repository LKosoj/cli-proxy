from bs4 import BeautifulSoup
from pathlib import Path
import re


def test_miniapp_index_html_has_remote_control_elements():
    html_path = Path("miniapp/static/index.html")
    html_content = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_content, "html.parser")

    assert soup.find(id="settingsRemoteControlEnabled") is not None
    assert soup.find(id="settingsRemoteControlHost") is not None
    assert soup.find(id="settingsRemoteControlRecheck") is not None
    assert soup.find(id="settingsExecutionTargetBanner") is not None
    assert soup.find(id="settingsRemoteControlError") is not None


def test_miniapp_index_html_logs_config_no_remote_toggle():
    html_path = Path("miniapp/static/index.html")
    html_content = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_content, "html.parser")

    # Check Logs tab structure
    logs_panel = soup.find(id="tab-logs")
    if logs_panel:
        # Logs should not contain a toggle for remote mode
        toggles = logs_panel.find_all("input", type="checkbox")
        for t in toggles:
            assert "remote" not in str(t.get("id")).lower()

    # Check Config tab structure
    config_panel = soup.find(id="tab-config")
    if config_panel:
        # Config should not contain a toggle for remote mode
        toggles = config_panel.find_all("input", type="checkbox")
        for t in toggles:
            assert "remote" not in str(t.get("id")).lower()


def test_miniapp_app_js_has_remote_control_logic():
    js_path = Path("miniapp/static/app.js")
    js_content = js_path.read_text(encoding="utf-8")

    assert "settingsRemoteControlEnabled" in js_content
    assert "settingsRemoteControlHost" in js_content
    assert "settingsRemoteControlRecheck" in js_content
    assert "miniapp.settings.exec_target_remote" in js_content
    assert "miniapp.settings.exec_target_local" in js_content

    # Verify busy session handling includes new elements
    assert re.search(r"rcEnabled\.disabled\s*=\s*isBusy", js_content) is not None
    assert re.search(r"rcHostSelect\.disabled\s*=\s*isBusy", js_content) is not None
