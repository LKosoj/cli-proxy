from bs4 import BeautifulSoup
from pathlib import Path


def test_miniapp_index_html_has_all_banners():
    html_path = Path("miniapp/static/index.html")
    html_content = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_content, "html.parser")

    # 1. Settings
    assert soup.find(id="settingsExecutionTargetBanner") is not None
    # 2. Files
    assert soup.find(id="filesExecutionTargetBanner") is not None
    assert soup.find(id="filesRemoteFsBanner") is not None
    # 3. Editor
    assert soup.find(id="editorExecutionTargetBanner") is not None
    assert soup.find(id="editorRemoteFsBanner") is not None
    # 4. Status cards
    assert soup.find(id="statusExecutionTargetBanner") is not None


def test_miniapp_app_js_remote_banners_logic():
    js_path = Path("miniapp/static/app.js")
    js_content = js_path.read_text(encoding="utf-8")

    # Banner assignments
    assert "Execution Target: Remote" in js_content
    assert "Execution Target: Local" in js_content
    assert "Remote FS" in js_content

    # Check that git semantics uses git_available
    assert "active.execution_target === \"remote\" && active.git_available === false" in js_content
    assert "git unavailable for this target" in js_content

    # Path context showing remote path
    assert "Текущий путь (Remote:" in js_content


def test_miniapp_routes_exports_remote_fields():
    routes_path = Path("miniapp/routes.py")
    routes_content = routes_path.read_text(encoding="utf-8")

    # Check that effective_state is injected in _build_session_payload
    assert "\"execution_target\": effective_state.execution_target.value" in routes_content
    assert "\"remote_host_alias\": effective_state.host_alias" in routes_content
    assert "\"remote_project_root\": effective_state.remote_project_root" in routes_content
    assert "git_available = effective_state.git_available" in routes_content
    assert "\"git_available\": git_available" in routes_content
