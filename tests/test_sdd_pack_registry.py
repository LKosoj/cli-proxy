from __future__ import annotations

from pathlib import Path

import pytest

from modes.sdd.packs.detectors import score_pack
from modes.sdd.packs.registry import load_pack_registry, save_project_pack_index, write_pack_definition
from modes.sdd.packs.selector import select_packs
from modes.sdd.packs.schema import PackDefinition


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not available on this filesystem")


def _pack(pack_id: str, *, source: str = "project") -> PackDefinition:
    return PackDefinition.from_dict(
        {
            "schema_version": "1",
            "pack_id": pack_id,
            "title": pack_id,
            "lifecycle": source,
            "version": "1.0",
        },
        source=source,
    )


def test_sdd_pack_selector_detects_rust_cargo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"demo\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text("fn main() {}\n", encoding="utf-8")

    registry = load_pack_registry(workdir=str(tmp_path))
    selection = select_packs(registry=registry, workdir=str(tmp_path))

    selected_ids = [score.pack.pack_id for score in selection.selected]
    assert "core-baseline" in selected_ids
    assert "rust-cargo" in selected_ids
    assert selection.status == "selected"


def test_sdd_pack_selector_detects_rust_cargo_workspace(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        "[workspace]\nmembers = [\"crates/app\"]\n",
        encoding="utf-8",
    )
    crate_src = tmp_path / "crates" / "app" / "src"
    crate_src.mkdir(parents=True)
    (crate_src / "lib.rs").write_text("pub fn run() {}\n", encoding="utf-8")

    registry = load_pack_registry(workdir=str(tmp_path))
    selection = select_packs(registry=registry, workdir=str(tmp_path))

    selected_ids = [score.pack.pack_id for score in selection.selected]
    assert "rust-cargo" in selected_ids


def test_sdd_pack_selector_detects_esp32_and_platformio(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text(
        "include($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(demo)\n",
        encoding="utf-8",
    )
    (tmp_path / "sdkconfig").write_text("CONFIG_IDF_TARGET=\"esp32\"\n", encoding="utf-8")
    (tmp_path / "platformio.ini").write_text(
        "[env:esp32dev]\nplatform = espressif32\nframework = espidf\n",
        encoding="utf-8",
    )

    registry = load_pack_registry(workdir=str(tmp_path))
    selection = select_packs(registry=registry, workdir=str(tmp_path))

    selected_ids = [score.pack.pack_id for score in selection.selected]
    assert "esp32-embedded" in selected_ids
    assert "platformio" in selected_ids


def test_sdd_pack_selector_creates_proposed_pack_for_uncovered_project(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
    (tmp_path / "main.zig").write_text("pub fn main() void {}\n", encoding="utf-8")

    registry = load_pack_registry(workdir=str(tmp_path))
    selection = select_packs(registry=registry, workdir=str(tmp_path))

    assert selection.status == "proposed"
    assert selection.proposed
    assert selection.proposed[0].pack_id.startswith("proposed-")


def test_sdd_pack_selector_does_not_auto_select_persisted_proposed_pack(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
    (tmp_path / "main.zig").write_text("pub fn main() void {}\n", encoding="utf-8")

    registry = load_pack_registry(workdir=str(tmp_path))
    first = select_packs(registry=registry, workdir=str(tmp_path))
    assert first.proposed
    write_pack_definition(workdir=str(tmp_path), pack=first.proposed[0], lifecycle="proposed")

    registry = load_pack_registry(workdir=str(tmp_path))
    second = select_packs(registry=registry, workdir=str(tmp_path))

    assert second.status == "proposed"
    selected_ids = [score.pack.pack_id for score in second.selected]
    assert first.proposed[0].pack_id not in selected_ids


def test_sdd_pack_detector_rejects_paths_outside_workdir(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.json"
    outside.write_text('{"secret": true}\n', encoding="utf-8")
    pack = PackDefinition.from_dict(
        {
            "schema_version": "1",
            "pack_id": "unsafe-detector",
            "title": "Unsafe Detector",
            "lifecycle": "builtin",
            "version": "1.0",
            "detectors": {
                "rules": [
                    {
                        "id": "outside",
                        "kind": "json_field",
                        "path": "../outside.json",
                        "field": "secret",
                    }
                ]
            },
        },
        source="builtin",
    )

    result = score_pack(pack, workdir=str(tmp_path))

    assert result.evidence == []
    assert result.score == 0


def test_sdd_xml_detector_rejects_symlink_outside_workdir(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.csproj"
    outside.write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>\n', encoding="utf-8")
    link = tmp_path / "demo.csproj"
    _symlink_or_skip(link, outside)

    pack = PackDefinition.from_dict(
        {
            "schema_version": "1",
            "pack_id": "dotnet-symlink",
            "title": "Dotnet Symlink",
            "lifecycle": "builtin",
            "version": "1.0",
            "detectors": {
                "rules": [
                    {
                        "id": "csproj",
                        "kind": "xml_hint",
                        "pattern": "*.csproj",
                        "sdk_contains": "Microsoft.NET.Sdk",
                    }
                ]
            },
        },
        source="builtin",
    )

    result = score_pack(pack, workdir=str(tmp_path))

    assert result.evidence == []
    assert result.score == 0


def test_sdd_any_file_detector_rejects_symlink_outside_workdir(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-Dockerfile"
    outside.write_text("FROM alpine\n", encoding="utf-8")
    _symlink_or_skip(tmp_path / "Dockerfile", outside)
    pack = PackDefinition.from_dict(
        {
            "schema_version": "1",
            "pack_id": "ops-symlink",
            "title": "Ops Symlink",
            "lifecycle": "builtin",
            "version": "1.0",
            "detectors": {
                "rules": [
                    {
                        "id": "dockerfile",
                        "kind": "any_file_matches",
                        "pattern": "Dockerfile",
                    }
                ]
            },
        },
        source="builtin",
    )

    result = score_pack(pack, workdir=str(tmp_path))

    assert result.evidence == []
    assert result.score == 0


def test_sdd_ext_count_detector_rejects_symlink_outside_workdir(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-main.rs"
    outside.write_text("fn main() {}\n", encoding="utf-8")
    _symlink_or_skip(tmp_path / "main.rs", outside)
    pack = PackDefinition.from_dict(
        {
            "schema_version": "1",
            "pack_id": "rust-symlink",
            "title": "Rust Symlink",
            "lifecycle": "builtin",
            "version": "1.0",
            "detectors": {
                "rules": [
                    {
                        "id": "rs",
                        "kind": "ext_count",
                        "ext": ".rs",
                        "min": 1,
                    }
                ]
            },
        },
        source="builtin",
    )

    result = score_pack(pack, workdir=str(tmp_path))

    assert result.evidence == []
    assert result.score == 0


def test_sdd_pack_registry_rejects_pack_yaml_symlink_outside_workdir(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-pack.yaml"
    outside.write_text(
        "schema_version: '1'\npack_id: outside-pack\ntitle: Outside\nlifecycle: project\nversion: '1.0'\n",
        encoding="utf-8",
    )
    pack_dir = tmp_path / ".cli-proxy" / "sdd" / "packs" / "project"
    pack_dir.mkdir(parents=True)
    _symlink_or_skip(pack_dir / "outside-pack.yaml", outside)

    registry = load_pack_registry(workdir=str(tmp_path))

    assert registry.get("outside-pack") is None


def test_sdd_pack_registry_rejects_pack_dir_symlink_outside_workdir(tmp_path: Path) -> None:
    outside_pack_dir = tmp_path.parent / f"{tmp_path.name}-outside-packs"
    outside_pack_dir.mkdir()
    (outside_pack_dir / "outside-pack.yaml").write_text(
        "schema_version: '1'\npack_id: outside-pack\ntitle: Outside\nlifecycle: project\nversion: '1.0'\n",
        encoding="utf-8",
    )
    pack_root = tmp_path / ".cli-proxy" / "sdd" / "packs"
    pack_root.mkdir(parents=True)
    _symlink_or_skip(pack_root / "project", outside_pack_dir)

    registry = load_pack_registry(workdir=str(tmp_path))

    assert registry.get("outside-pack") is None


def test_sdd_pack_registry_rejects_cli_proxy_symlink_on_index_write(tmp_path: Path) -> None:
    outside_cli_proxy = tmp_path.parent / f"{tmp_path.name}-outside-cli-proxy"
    outside_cli_proxy.mkdir()
    _symlink_or_skip(tmp_path / ".cli-proxy", outside_cli_proxy)

    with pytest.raises(RuntimeError, match="unsafe_sdd_pack_path"):
        save_project_pack_index(workdir=str(tmp_path), packs=[_pack("safe-pack")])

    assert not (outside_cli_proxy / "sdd" / "packs" / "index.json").exists()


def test_sdd_pack_registry_rejects_cli_proxy_symlink_on_pack_write(tmp_path: Path) -> None:
    outside_cli_proxy = tmp_path.parent / f"{tmp_path.name}-outside-cli-proxy"
    outside_cli_proxy.mkdir()
    _symlink_or_skip(tmp_path / ".cli-proxy", outside_cli_proxy)

    with pytest.raises(RuntimeError, match="unsafe_sdd_pack_path"):
        write_pack_definition(workdir=str(tmp_path), pack=_pack("safe-pack", source="proposed"), lifecycle="proposed")

    assert not (outside_cli_proxy / "sdd" / "packs" / "proposed" / "safe-pack.yaml").exists()


def test_sdd_proposed_pack_does_not_shadow_existing_pack_id(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"demo\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    pack_dir = tmp_path / ".cli-proxy" / "sdd" / "packs" / "proposed"
    pack_dir.mkdir(parents=True)
    (pack_dir / "rust-cargo.yaml").write_text(
        """
schema_version: '1'
pack_id: rust-cargo
title: Shadow Rust
lifecycle: project
version: '1.0'
detectors:
  min_confidence: 0.55
  rules:
    - id: cargo
      kind: file_exists
      path: Cargo.toml
      weight: 1.0
""".lstrip(),
        encoding="utf-8",
    )

    registry = load_pack_registry(workdir=str(tmp_path))
    pack = registry.get("rust-cargo")
    selection = select_packs(registry=registry, workdir=str(tmp_path))

    assert pack is not None
    assert pack.source != "proposed"
    assert pack.title != "Shadow Rust"
    assert "rust-cargo" in [score.pack.pack_id for score in selection.selected]


def test_sdd_proposed_pack_source_is_not_auto_selected_even_if_lifecycle_project(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
    pack_dir = tmp_path / ".cli-proxy" / "sdd" / "packs" / "proposed"
    pack_dir.mkdir(parents=True)
    (pack_dir / "cheat.yaml").write_text(
        """
schema_version: '1'
pack_id: proposed-cheat
title: Proposed Cheat
lifecycle: project
version: '1.0'
detectors:
  min_confidence: 0.55
  rules:
    - id: makefile
      kind: file_exists
      path: Makefile
      weight: 1.0
""".lstrip(),
        encoding="utf-8",
    )

    selection = select_packs(registry=load_pack_registry(workdir=str(tmp_path)), workdir=str(tmp_path))

    selected_ids = [score.pack.pack_id for score in selection.selected]
    assert "proposed-cheat" not in selected_ids


def test_sdd_pack_write_rejects_unsafe_pack_id(tmp_path: Path) -> None:
    pack = PackDefinition.from_dict(
        {
            "schema_version": "1",
            "pack_id": "safe-pack",
            "title": "Safe Pack",
            "lifecycle": "proposed",
            "version": "1.0",
        },
        source="proposed",
    )
    object.__setattr__(pack, "pack_id", "../bad")

    with pytest.raises(ValueError):
        write_pack_definition(workdir=str(tmp_path), pack=pack, lifecycle="proposed")


def test_sdd_ui_pack_detects_tsx_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.tsx").write_text("export function App() { return null }\n", encoding="utf-8")

    selection = select_packs(registry=load_pack_registry(workdir=str(tmp_path)), workdir=str(tmp_path))

    assert "ui" in [score.pack.pack_id for score in selection.selected]


def test_sdd_pack_selector_detects_root_level_adr_directory(tmp_path: Path) -> None:
    adr_dir = tmp_path / "adr"
    adr_dir.mkdir()
    (adr_dir / "001-record.md").write_text("# ADR\n", encoding="utf-8")

    selection = select_packs(registry=load_pack_registry(workdir=str(tmp_path)), workdir=str(tmp_path))

    assert "adr" in [score.pack.pack_id for score in selection.selected]


def test_sdd_pack_selector_detects_root_level_architecture_doc(tmp_path: Path) -> None:
    (tmp_path / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")

    selection = select_packs(registry=load_pack_registry(workdir=str(tmp_path)), workdir=str(tmp_path))

    assert "architecture" in [score.pack.pack_id for score in selection.selected]
