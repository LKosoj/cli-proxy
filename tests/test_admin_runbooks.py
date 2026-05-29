from pathlib import Path

from modes.admin.runbooks import (
    global_runbooks_dir,
    load_runbooks,
    match_runbooks,
    server_runbooks_dir,
    summarize_runbooks,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_runbook(
    dir_path: Path,
    name: str,
    *,
    rb_id=None,
    title="X",
    servers=None,
    tags=None,
    triggers=None,
    body="1. step one\n",
):
    front = ["---"]
    front.append(f"id: {rb_id or name.replace('.md', '')}")
    front.append(f"title: \"{title}\"")
    if servers is not None:
        front.append("servers: [" + ", ".join(f'"{s}"' for s in servers) + "]")
    if tags is not None:
        front.append("tags: [" + ", ".join(f'"{t}"' for t in tags) + "]")
    if triggers is not None:
        front.append("triggers: [" + ", ".join(f'"{t}"' for t in triggers) + "]")
    front.append("---")
    content = "\n".join(front) + "\n" + body
    _write(dir_path / name, content)


def test_load_runbooks_empty_returns_empty(tmp_path):
    assert load_runbooks(str(tmp_path)) == []


def test_load_global_runbook_with_full_frontmatter(tmp_path):
    _write_runbook(
        global_runbooks_dir(str(tmp_path)),
        "disk-cleanup.md",
        rb_id="disk-cleanup",
        title="Disk cleanup",
        servers=["web-*"],
        tags=["disk", "log"],
        triggers=["disk_pct>90"],
    )
    rbs = load_runbooks(str(tmp_path))
    assert len(rbs) == 1
    rb = rbs[0]
    assert rb.id == "disk-cleanup"
    assert rb.title == "Disk cleanup"
    assert rb.servers == ["web-*"]
    assert rb.tags == ["disk", "log"]
    assert rb.triggers == ["disk_pct>90"]
    assert rb.scope == "global"
    assert rb.owner_server_id is None
    assert "step one" in rb.body


def test_load_runbook_without_frontmatter_is_skipped(tmp_path):
    path = global_runbooks_dir(str(tmp_path)) / "bad.md"
    _write(path, "# No frontmatter here")
    assert load_runbooks(str(tmp_path)) == []


def test_load_server_scoped_runbook(tmp_path):
    _write_runbook(
        server_runbooks_dir(str(tmp_path), "web-01"),
        "nginx-reload.md",
        tags=["nginx"],
    )
    rbs = load_runbooks(str(tmp_path), server_ids=["web-01"])
    assert len(rbs) == 1
    assert rbs[0].scope == "server"
    assert rbs[0].owner_server_id == "web-01"


def test_match_runbooks_scores_server_glob(tmp_path):
    _write_runbook(global_runbooks_dir(str(tmp_path)), "a.md", rb_id="a", servers=["web-*"])
    _write_runbook(global_runbooks_dir(str(tmp_path)), "b.md", rb_id="b", servers=["db-*"])
    rbs = load_runbooks(str(tmp_path))
    matched = match_runbooks(rbs, server_id="web-01")
    assert [m.id for m in matched] == ["a"]


def test_match_runbooks_scores_tags(tmp_path):
    _write_runbook(global_runbooks_dir(str(tmp_path)), "disk.md", rb_id="disk", tags=["disk", "log"])
    _write_runbook(global_runbooks_dir(str(tmp_path)), "net.md", rb_id="net", tags=["network"])
    rbs = load_runbooks(str(tmp_path))
    matched = match_runbooks(rbs, tags=["disk"])
    assert [m.id for m in matched] == ["disk"]


def test_match_runbooks_combines_server_and_tags(tmp_path):
    _write_runbook(global_runbooks_dir(str(tmp_path)), "a.md", rb_id="a", servers=["web-*"], tags=["disk"])
    _write_runbook(global_runbooks_dir(str(tmp_path)), "b.md", rb_id="b", servers=["web-*"])
    _write_runbook(global_runbooks_dir(str(tmp_path)), "c.md", rb_id="c", tags=["disk"])
    rbs = load_runbooks(str(tmp_path))
    matched = match_runbooks(rbs, server_id="web-01", tags=["disk"])
    assert [m.id for m in matched] == ["a", "b", "c"]


def test_match_runbooks_includes_general_purpose_runbook_with_low_score(tmp_path):
    _write_runbook(global_runbooks_dir(str(tmp_path)), "general.md", rb_id="general")
    _write_runbook(global_runbooks_dir(str(tmp_path)), "specific.md", rb_id="specific", tags=["disk"])
    rbs = load_runbooks(str(tmp_path))
    matched = match_runbooks(rbs, tags=["disk"])
    assert matched[0].id == "specific"
    assert "general" in {m.id for m in matched}


def test_match_runbooks_prefers_server_scoped_with_owner_match(tmp_path):
    _write_runbook(global_runbooks_dir(str(tmp_path)), "glob.md", rb_id="glob", servers=["web-*"])
    _write_runbook(server_runbooks_dir(str(tmp_path), "web-01"), "local.md", rb_id="local")
    rbs = load_runbooks(str(tmp_path), server_ids=["web-01"])
    matched = match_runbooks(rbs, server_id="web-01")
    assert matched[0].id == "local"


def test_duplicate_id_is_deduplicated(tmp_path):
    _write_runbook(global_runbooks_dir(str(tmp_path)), "a.md", rb_id="same")
    _write_runbook(server_runbooks_dir(str(tmp_path), "web-01"), "b.md", rb_id="same")
    rbs = load_runbooks(str(tmp_path), server_ids=["web-01"])
    assert len(rbs) == 1


def test_summarize_runbooks_respects_limit(tmp_path):
    for i in range(10):
        _write_runbook(global_runbooks_dir(str(tmp_path)), f"r{i}.md", rb_id=f"r{i}", tags=["x"])
    rbs = load_runbooks(str(tmp_path))
    matched = match_runbooks(rbs, tags=["x"])
    summary = summarize_runbooks(matched, limit=3)
    assert len(summary) == 3
    for item in summary:
        assert "id" in item and "title" in item and "path" in item


def test_unmatched_returns_empty_when_no_generic_runbooks(tmp_path):
    _write_runbook(global_runbooks_dir(str(tmp_path)), "a.md", rb_id="a", servers=["web-*"])
    rbs = load_runbooks(str(tmp_path))
    assert match_runbooks(rbs, server_id="db-01", tags=["net"]) == []
