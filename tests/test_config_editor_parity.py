from app.config_runtime.field_paths import RUNTIME_CONFIG_FIELD_PATHS
from desktop.widgets.config_editor import (
    DESKTOP_EDITABLE_CONFIG_FIELDS,
    DESKTOP_UNSUPPORTED_CONFIG_FIELDS,
)
from miniapp.services.config_service import editable_config_fields


def test_config_editor_surfaces_cover_runtime_config_fields():
    miniapp_editable = editable_config_fields()
    desktop_editable = set(DESKTOP_EDITABLE_CONFIG_FIELDS)
    desktop_unsupported = set(DESKTOP_UNSUPPORTED_CONFIG_FIELDS)

    covered_fields = miniapp_editable | desktop_editable | desktop_unsupported

    assert RUNTIME_CONFIG_FIELD_PATHS <= covered_fields


def test_config_editor_surface_policies_do_not_conflict():
    miniapp_editable = editable_config_fields()

    assert miniapp_editable.isdisjoint(DESKTOP_UNSUPPORTED_CONFIG_FIELDS)
    assert DESKTOP_EDITABLE_CONFIG_FIELDS.isdisjoint(DESKTOP_UNSUPPORTED_CONFIG_FIELDS)


def test_desktop_config_editor_does_not_surface_phantom_defaults_theme():
    assert "defaults.theme" not in DESKTOP_EDITABLE_CONFIG_FIELDS
    assert "defaults.theme" not in DESKTOP_UNSUPPORTED_CONFIG_FIELDS
    assert "defaults.theme" not in RUNTIME_CONFIG_FIELD_PATHS
