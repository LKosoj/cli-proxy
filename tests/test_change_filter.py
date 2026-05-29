from modes.sdk.runtime.tooling.change_filter import (
    filter_git_name_status_lines,
    filter_git_porcelain_lines,
    filter_git_stat_text,
    format_git_log_name_status,
    is_noise_path,
)


def test_is_noise_path_filters_common_noise_dirs_and_lockfiles_and_sensitive():
    assert is_noise_path("node_modules/react/index.js")[0] is True
    assert is_noise_path("frontend/node_modules/.vite/deps/chunk.js")[0] is True
    assert is_noise_path(".venv/bin/python")[0] is True
    assert is_noise_path("dist/app.js")[0] is True
    assert is_noise_path("package-lock.json")[0] is True
    assert is_noise_path(".env")[0] is True

    assert is_noise_path("src/app/main.py")[0] is False
    assert is_noise_path("frontend/src/App.tsx")[0] is False


def test_filter_git_name_status_lines_drops_noise_paths():
    lines = [
        "M\tfrontend/node_modules/.vite/deps/chunk-AAA.js",
        "A\tsrc/app/main.py",
        "R100\tnode_modules/a.js\tnode_modules/b.js",
        "M\tREADME.md",
    ]
    kept, summ = filter_git_name_status_lines(lines)
    assert "A\tsrc/app/main.py" in kept
    assert "M\tREADME.md" in kept
    assert all("node_modules" not in x for x in kept)
    assert summ.filtered == 2


def test_filter_git_porcelain_lines_drops_noise_paths_and_keeps_unknown_lines():
    lines = [
        " M node_modules/a.js",
        "?? src/new_file.py",
        "R  node_modules/a.js -> node_modules/b.js",
        "UU ???unparseable???",
    ]
    kept, summ = filter_git_porcelain_lines(lines)
    assert "?? src/new_file.py" in kept
    # Unknown/unparseable lines are kept conservatively.
    assert "UU ???unparseable???" in kept
    assert all("node_modules" not in x for x in kept if "\t" not in x)
    assert summ.filtered == 2


def test_filter_git_stat_text_drops_noise_file_lines_but_keeps_totals():
    stat = "\n".join(
        [
            " node_modules/a.js | 10 ++++++++++",
            " src/app/main.py   |  2 ++",
            " 2 files changed, 12 insertions(+)",
        ]
    )
    out, summ = filter_git_stat_text(stat)
    assert "node_modules/a.js" not in out
    assert "src/app/main.py" in out
    assert "2 files changed" in out
    assert summ.filtered == 1


def test_format_git_log_name_status_filters_and_caps():
    raw = "\n".join(
        [
            "fix: something (abc123)",
            "M\tnode_modules/a.js",
            "M\tsrc/app/main.py",
            "M\tpackage-lock.json",
        ]
    )
    out = format_git_log_name_status(raw, max_lines=80)
    assert "fix: something (abc123)" in out
    assert "node_modules/a.js" not in out
    assert "package-lock.json" not in out
    assert "M\tsrc/app/main.py" in out
    # Should mention that something was hidden.
    assert "скрыто" in out
