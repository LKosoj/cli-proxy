from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from modes.admin.runbook_builder import BuildSpec, ScriptInput, build_runbook_from_scripts, scripts_dir
from modes.admin.runbook_promoter import (
    PromoteResult,
    RunbookPromoteError,
    promote_runbook,
)
from modes.admin.runbooks import global_runbooks_dir, load_runbooks, match_runbooks


def _build(tmp_path: Path, *, rb_id="rb-promo", dev="dev-01", body="#!/bin/bash\necho ok\n"):
    spec = BuildSpec(
        title="Promo",
        dev_server_id=dev,
        rb_id=rb_id,
        scripts=[ScriptInput(name="01-run.sh", body=body)],
        tags=["svc"],
    )
    return build_runbook_from_scripts(str(tmp_path), spec)


def _load_front(tmp_path: Path, rb_id: str) -> dict:
    path = global_runbooks_dir(str(tmp_path)) / f"{rb_id}.md"
    text = path.read_text(encoding="utf-8")
    front_text = text.split("---", 2)[1]
    return yaml.safe_load(front_text)


def test_promote_adds_prod_server(tmp_path):
    _build(tmp_path)
    res = asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"]))
    assert isinstance(res, PromoteResult)
    assert res.added_servers == ["prod-01"]
    assert res.already_present == []
    front = _load_front(tmp_path, "rb-promo")
    assert "dev-01" in front["servers"]
    assert "prod-01" in front["servers"]


def test_promote_deduplicates_when_already_present(tmp_path):
    _build(tmp_path)
    asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"]))
    res = asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01", "prod-02"]))
    assert res.added_servers == ["prod-02"]
    assert res.already_present == ["prod-01"]
    front = _load_front(tmp_path, "rb-promo")
    assert front["servers"].count("prod-01") == 1


def test_promote_raises_confidence(tmp_path):
    _build(tmp_path)
    res = asyncio.run(
        promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"], confidence=0.8),
    )
    assert res.confidence_before == 0.0
    assert res.confidence_after == 0.8
    front = _load_front(tmp_path, "rb-promo")
    assert front["auto_action"]["confidence"] == 0.8


def test_promote_rejects_out_of_range_confidence(tmp_path):
    _build(tmp_path)
    with pytest.raises(RunbookPromoteError, match="confidence"):
        asyncio.run(
            promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"], confidence=1.5),
        )


def test_promote_rejects_empty_add_servers(tmp_path):
    _build(tmp_path)
    with pytest.raises(RunbookPromoteError, match="add_servers"):
        asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=[]))


def test_promote_rejects_unknown_rb(tmp_path):
    with pytest.raises(RunbookPromoteError, match="not found"):
        asyncio.run(promote_runbook(str(tmp_path), "rb-missing", add_servers=["prod-01"]))


def test_promote_rejects_invalid_rb_id(tmp_path):
    with pytest.raises(RunbookPromoteError, match="rb_id invalid"):
        asyncio.run(promote_runbook(str(tmp_path), "../etc", add_servers=["prod-01"]))


def test_promote_fails_when_validation_fails(tmp_path):
    _build(tmp_path)
    # подпорчиваем скрипт — validate_runbook должен вернуть ok=False
    sdir = scripts_dir(str(tmp_path), "rb-promo")
    (sdir / "01-run.sh").write_text("#!/bin/bash\necho tampered\n", encoding="utf-8")
    with pytest.raises(RunbookPromoteError, match="validation"):
        asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"]))
    # Файл не должен был быть изменён (servers остались прежними).
    front = _load_front(tmp_path, "rb-promo")
    assert "prod-01" not in front["servers"]


def test_promote_skips_validation_when_disabled(tmp_path):
    _build(tmp_path)
    sdir = scripts_dir(str(tmp_path), "rb-promo")
    (sdir / "01-run.sh").write_text("#!/bin/bash\necho tampered\n", encoding="utf-8")
    res = asyncio.run(
        promote_runbook(
            str(tmp_path), "rb-promo", add_servers=["prod-01"], run_validation=False,
        ),
    )
    assert "prod-01" in res.added_servers
    assert res.validation is None


def test_promote_preserves_body_and_other_fields(tmp_path):
    _build(tmp_path)
    asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"]))
    path = global_runbooks_dir(str(tmp_path)) / "rb-promo.md"
    text = path.read_text(encoding="utf-8")
    # body с заголовком сохранён
    assert "# Promo" in text
    # steps/auto_action на месте
    front = _load_front(tmp_path, "rb-promo")
    assert front["steps"][0]["script"] == "01-run.sh"
    assert front["auto_action"]["rb_id"] == "rb-promo"


def test_promote_makes_runbook_match_prod_after_promote(tmp_path):
    _build(tmp_path)
    # до промоута — prod не матчит по server_id (но матчит по тегам)
    asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"]))
    loaded = [r for r in load_runbooks(str(tmp_path)) if r.id == "rb-promo"][0]
    assert "prod-01" in loaded.servers
    matched = match_runbooks(load_runbooks(str(tmp_path)), server_id="prod-01", tags=["svc"])
    assert any(m.id == "rb-promo" for m in matched)


def test_promote_writes_audit_note_to_added_servers(tmp_path):
    from modes.admin.memory import ServerMemory
    _build(tmp_path)
    asyncio.run(promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"]))
    notes = ServerMemory(str(tmp_path), "prod-01").get_notes()
    assert "RUNBOOK-PROMOTED" in notes
    assert "rb-promo" in notes


def test_promote_normalizes_server_ids(tmp_path):
    _build(tmp_path)
    res = asyncio.run(
        promote_runbook(str(tmp_path), "rb-promo", add_servers=["Prod@01"]),
    )
    # safe_server_id заменит @ на _
    assert res.added_servers and "@" not in res.added_servers[0]


def test_promote_handles_utf8_bom_in_runbook_file(tmp_path):
    _build(tmp_path)
    # Внешний редактор мог сохранить .md с UTF-8 BOM — парсер должен это пережить.
    path = global_runbooks_dir(str(tmp_path)) / "rb-promo.md"
    raw = path.read_bytes()
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    res = asyncio.run(
        promote_runbook(str(tmp_path), "rb-promo", add_servers=["prod-01"], run_validation=False),
    )
    assert "prod-01" in res.added_servers
    front = _load_front(tmp_path, "rb-promo")
    assert "prod-01" in front["servers"]
