"""Tests for ensure_cli_proxy_gitignored() in session.py."""

from session import _gitignore_checked, ensure_cli_proxy_gitignored


def _reset_cache():
    _gitignore_checked.clear()


def test_creates_gitignore_when_missing(tmp_path):
    _reset_cache()
    ensure_cli_proxy_gitignored(str(tmp_path))
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    assert ".cli-proxy/" in gitignore.read_text()


def test_appends_to_existing_gitignore(tmp_path):
    _reset_cache()
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    ensure_cli_proxy_gitignored(str(tmp_path))
    content = (tmp_path / ".gitignore").read_text()
    assert "node_modules/" in content
    assert ".cli-proxy/" in content


def test_skips_if_already_present_with_slash(tmp_path):
    _reset_cache()
    (tmp_path / ".gitignore").write_text("foo\n.cli-proxy/\nbar\n")
    ensure_cli_proxy_gitignored(str(tmp_path))
    content = (tmp_path / ".gitignore").read_text()
    assert content.count(".cli-proxy") == 1


def test_skips_if_already_present_without_slash(tmp_path):
    _reset_cache()
    (tmp_path / ".gitignore").write_text(".cli-proxy\n")
    ensure_cli_proxy_gitignored(str(tmp_path))
    content = (tmp_path / ".gitignore").read_text()
    assert content.count(".cli-proxy") == 1


def test_handles_file_without_trailing_newline(tmp_path):
    _reset_cache()
    (tmp_path / ".gitignore").write_text("first")
    ensure_cli_proxy_gitignored(str(tmp_path))
    content = (tmp_path / ".gitignore").read_text()
    assert "first\n.cli-proxy/\n" == content


def test_caches_per_realpath(tmp_path):
    _reset_cache()
    ensure_cli_proxy_gitignored(str(tmp_path))
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    gitignore.unlink()
    # Second call should be cached — file should NOT be recreated
    ensure_cli_proxy_gitignored(str(tmp_path))
    assert not gitignore.exists()


def test_does_not_raise_on_error(tmp_path):
    _reset_cache()
    nonexistent = str(tmp_path / "no" / "such" / "dir")
    # Should not raise despite workdir not existing
    ensure_cli_proxy_gitignored(nonexistent)


def test_empty_gitignore_file(tmp_path):
    _reset_cache()
    (tmp_path / ".gitignore").write_text("")
    ensure_cli_proxy_gitignored(str(tmp_path))
    content = (tmp_path / ".gitignore").read_text()
    assert ".cli-proxy/" in content
