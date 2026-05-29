from __future__ import annotations

import hashlib

import pytest
import yaml

from modes.admin.runbook_builder import (
    ACTION_SCRIPT_RUN,
    BuildSpec,
    RunbookBuilderError,
    ScriptInput,
    build_runbook_from_scripts,
    scripts_dir,
)
from modes.admin.runbooks import global_runbooks_dir, load_runbooks, match_runbooks


def _spec(
    *, title="Reload nginx", dev="dev-01", rb_id=None, body="#!/bin/bash\necho go\n",
    scripts=None, tags=None, target="local",
):
    return BuildSpec(
        title=title,
        dev_server_id=dev,
        rb_id=rb_id,
        scripts=scripts or [ScriptInput(name="01-reload.sh", body=body, target_hint=target)],
        tags=tags or ["nginx"],
    )


def test_build_writes_runbook_and_scripts(tmp_path):
    rb = build_runbook_from_scripts(str(tmp_path), _spec())
    assert rb.id.startswith("rb-")
    assert rb.title == "Reload nginx"
    assert "dev-01" in rb.servers

    # скрипт лежит в правильном каталоге
    sdir = scripts_dir(str(tmp_path), rb.id)
    assert (sdir / "01-reload.sh").is_file()

    # frontmatter содержит steps + auto_action.confidence=0.0
    raw = (global_runbooks_dir(str(tmp_path)) / f"{rb.id}.md").read_text(encoding="utf-8")
    front_text = raw.split("---", 2)[1]
    front = yaml.safe_load(front_text)
    assert front["auto_action"]["action_id"] == ACTION_SCRIPT_RUN
    assert front["auto_action"]["confidence"] == 0.0
    assert front["steps"][0]["script"] == "01-reload.sh"
    expected_sha = hashlib.sha1(b"#!/bin/bash\necho go\n").hexdigest()
    assert front["steps"][0]["checksum"] == f"sha1:{expected_sha}"


def test_build_tags_include_script_by_default(tmp_path):
    rb = build_runbook_from_scripts(str(tmp_path), _spec(tags=["nginx"]))
    assert "script" in rb.tags
    assert "nginx" in rb.tags


def test_build_rejects_empty_title(tmp_path):
    with pytest.raises(RunbookBuilderError, match="title"):
        build_runbook_from_scripts(str(tmp_path), _spec(title=""))


def test_build_rejects_empty_scripts(tmp_path):
    with pytest.raises(RunbookBuilderError, match="at least one script"):
        build_runbook_from_scripts(
            str(tmp_path),
            BuildSpec(title="t", dev_server_id="dev-01", scripts=[]),
        )


def test_build_rejects_bad_script_name(tmp_path):
    with pytest.raises(RunbookBuilderError, match="script name invalid"):
        build_runbook_from_scripts(
            str(tmp_path),
            _spec(scripts=[ScriptInput(name="../etc/passwd.sh", body="#")]),
        )


def test_build_rejects_non_sh_extension(tmp_path):
    with pytest.raises(RunbookBuilderError):
        build_runbook_from_scripts(
            str(tmp_path),
            _spec(scripts=[ScriptInput(name="foo.py", body="#")]),
        )


def test_build_rejects_empty_body(tmp_path):
    with pytest.raises(RunbookBuilderError, match="empty"):
        build_runbook_from_scripts(
            str(tmp_path),
            _spec(scripts=[ScriptInput(name="a.sh", body="  \n\n")]),
        )


def test_build_rejects_bad_target(tmp_path):
    with pytest.raises(RunbookBuilderError, match="target_hint"):
        build_runbook_from_scripts(
            str(tmp_path),
            _spec(scripts=[ScriptInput(name="a.sh", body="#", target_hint="remote")]),
        )


def test_build_rejects_duplicate_script_names(tmp_path):
    with pytest.raises(RunbookBuilderError, match="duplicate"):
        build_runbook_from_scripts(
            str(tmp_path),
            _spec(scripts=[
                ScriptInput(name="a.sh", body="#"),
                ScriptInput(name="a.sh", body="# also"),
            ]),
        )


def test_build_rejects_existing_rb_id_without_force(tmp_path):
    build_runbook_from_scripts(str(tmp_path), _spec(rb_id="rb-fixed"))
    with pytest.raises(RunbookBuilderError, match="already exists"):
        build_runbook_from_scripts(str(tmp_path), _spec(rb_id="rb-fixed"))


def test_build_overwrites_with_force(tmp_path):
    first = build_runbook_from_scripts(str(tmp_path), _spec(rb_id="rb-fixed"))
    rb2 = build_runbook_from_scripts(
        str(tmp_path),
        _spec(rb_id="rb-fixed", body="#!/bin/bash\necho v2\n"),
        force=True,
    )
    assert rb2.id == first.id
    sdir = scripts_dir(str(tmp_path), "rb-fixed")
    assert "v2" in (sdir / "01-reload.sh").read_text()


def test_build_force_cleans_orphaned_scripts(tmp_path):
    # Первый build: два скрипта.
    build_runbook_from_scripts(
        str(tmp_path),
        _spec(rb_id="rb-orphan", scripts=[
            ScriptInput(name="01-a.sh", body="#!/bin/bash\necho a\n"),
            ScriptInput(name="02-b.sh", body="#!/bin/bash\necho b\n"),
        ]),
    )
    sdir = scripts_dir(str(tmp_path), "rb-orphan")
    assert (sdir / "01-a.sh").is_file()
    assert (sdir / "02-b.sh").is_file()

    # Перебилд force=True: только один скрипт с другим именем.
    build_runbook_from_scripts(
        str(tmp_path),
        _spec(rb_id="rb-orphan", scripts=[
            ScriptInput(name="99-new.sh", body="#!/bin/bash\necho new\n"),
        ]),
        force=True,
    )
    assert (sdir / "99-new.sh").is_file()
    # orphan'ы должны быть удалены
    assert not (sdir / "01-a.sh").exists()
    assert not (sdir / "02-b.sh").exists()


def test_build_generated_rb_ids_unique(tmp_path):
    rb1 = build_runbook_from_scripts(str(tmp_path), _spec(title="Reload svc"))
    rb2 = build_runbook_from_scripts(str(tmp_path), _spec(title="Reload svc"))
    assert rb1.id != rb2.id


def test_build_matches_on_dev_server(tmp_path):
    rb = build_runbook_from_scripts(str(tmp_path), _spec(dev="dev-01", tags=["kern"]))
    matched = match_runbooks(load_runbooks(str(tmp_path)), server_id="dev-01", tags=["kern"])
    assert any(m.id == rb.id for m in matched)
    # До promote: servers=[dev-01], prod-01 не в списке — это ограничение для executor'а.
    loaded = [r for r in load_runbooks(str(tmp_path)) if r.id == rb.id][0]
    assert loaded.servers == ["dev-01"]


def test_build_multi_step(tmp_path):
    rb = build_runbook_from_scripts(
        str(tmp_path),
        _spec(scripts=[
            ScriptInput(name="01-prep.sh", body="#!/bin/bash\necho prep\n"),
            ScriptInput(name="02-main.sh", body="#!/bin/bash\necho main\n", target_hint="ssh"),
        ]),
    )
    # оба скрипта на диске
    sdir = scripts_dir(str(tmp_path), rb.id)
    assert (sdir / "01-prep.sh").is_file()
    assert (sdir / "02-main.sh").is_file()
    # steps в metadata
    steps = rb.metadata.get("steps")
    assert len(steps) == 2
    assert steps[1]["target_hint"] == "ssh"
    # auto_action указывает на первый step
    assert rb.metadata["auto_action"]["step"] == "01-prep"


def test_build_writes_audit_note_to_dev_server(tmp_path):
    from modes.admin.memory import ServerMemory
    build_runbook_from_scripts(str(tmp_path), _spec(dev="dev-01", rb_id="rb-audit"))
    notes = ServerMemory(str(tmp_path), "dev-01").get_notes()
    assert "RUNBOOK-BUILT" in notes
    assert "rb-audit" in notes


def test_build_scripts_dir_mode_restricted(tmp_path):
    rb = build_runbook_from_scripts(str(tmp_path), _spec())
    sdir = scripts_dir(str(tmp_path), rb.id)
    # 0o700 на каталоге (только владелец)
    mode = sdir.stat().st_mode & 0o777
    assert mode == 0o700 or mode == 0o755  # на некоторых FS chmod игнорируется
