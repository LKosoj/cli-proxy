from __future__ import annotations

import pytest

from config import load_config


LEGACY_FLAGS = (
    "schema_normalizer_v2_enabled",
    "validation_pipeline_v2_enabled",
    "orchestrator_v2_enabled",
)


def test_load_config_rejects_legacy_v2_flags(tmp_path) -> None:
    cfg_text = """
telegram:
  token: "t"
  whitelist_chat_ids: [1]
tools: {}
defaults:
  workdir: "/tmp"
  schema_normalizer_v2_enabled: true
  validation_pipeline_v2_enabled: true
  orchestrator_v2_enabled: true
"""
    path = tmp_path / "config_legacy_flags.yaml"
    path.write_text(cfg_text, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(path))

    assert exc_info.value.code == 1
