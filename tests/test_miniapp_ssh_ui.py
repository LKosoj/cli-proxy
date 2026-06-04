from pathlib import Path

from bs4 import BeautifulSoup


def test_miniapp_index_html_exposes_remote_project_root_field():
    html_path = Path("miniapp/static/index.html")
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    remote_root = soup.find(id="sshHostRemoteProjectRoot")
    assert remote_root is not None
    assert remote_root.name == "input"
    assert remote_root.get("placeholder") == "/absolute/path/to/project"


def test_miniapp_app_js_ssh_crud_uses_settings_project_workdir_and_root_field():
    js_path = Path("miniapp/static/app.js")
    js_content = js_path.read_text(encoding="utf-8")

    assert "available?.project_workdir" in js_content
    assert 'document.getElementById("sshHostRemoteProjectRoot").value.trim()' in js_content
    assert "const workdir = getSshWorkdir();" in js_content
    assert "Не удалось определить каталог проекта для сохранения SSH host." in js_content


def test_miniapp_app_js_remote_control_explains_empty_target_host():
    js_path = Path("miniapp/static/app.js")
    js_content = js_path.read_text(encoding="utf-8")

    assert 'miniapp.settings.no_eligible_hosts' in js_content
    assert "для Remote Control нужно заполнить remote_project_root" in js_content
    assert "rcHostSelect.disabled = isBusy || validHosts.length === 0;" in js_content
