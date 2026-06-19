# API Spec: `tests/test_admin_config_store.py`

Generated: 2026-06-17T10:46:18Z

## Symbols
- `def test_admin_config_store_uses_session_local_cli_proxy_path(tmp_path)` (line 40)
- `def test_admin_config_store_bootstraps_from_template_when_missing(tmp_path)` (line 51)
- `def test_admin_config_store_template_includes_runtime_and_generated_blocks(tmp_path)` (line 62)
- `def test_admin_config_store_does_not_overwrite_existing_file(tmp_path)` (line 78)
- `def test_admin_config_store_load_config_reads_session_file(tmp_path)` (line 92)
- `def test_admin_config_store_isolated_between_sequential_sessions(tmp_path)` (line 105)
- `def test_admin_config_store_rejects_invalid_runtime_schema(tmp_path, admin_payload, expected_fragment)` (line 133)
- `def test_admin_config_service_contract_get_and_save_yaml_without_miniapp_context(tmp_path)` (line 151)
- `def test_admin_config_service_supports_desktop_session_service_lookup(tmp_path)` (line 172)
- `def test_admin_config_service_rejects_stale_yaml_revision(tmp_path)` (line 185)
- `def test_admin_config_service_contract_monitor_servers_get_and_save(tmp_path)` (line 198)
- `def test_admin_config_service_validation_error_path(tmp_path)` (line 235)
- `def test_merge_generated_config_preserves_manual(tmp_path)` (line 254)
- `def test_effective_config_ignores_generated_inventory_for_other_manual_transport(tmp_path)` (line 372)
