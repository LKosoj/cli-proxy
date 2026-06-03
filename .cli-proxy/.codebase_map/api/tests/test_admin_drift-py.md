# API Spec: `tests/test_admin_drift.py`

Generated: 2026-06-03T02:24:28Z

## Symbols
- `def test_no_drift_when_baselines_equal()` (line 18)
- `def test_kernel_change_is_info()` (line 24)
- `def test_hostname_change_is_warn()` (line 34)
- `def test_new_listener_is_alarm()` (line 43)
- `def test_removed_listener_is_info()` (line 55)
- `def test_new_user_is_alarm()` (line 65)
- `def test_added_systemd_unit_is_warn()` (line 76)
- `def test_package_version_change_is_info()` (line 88)
- `def test_added_package_is_info()` (line 98)
- `def test_crontab_change_is_warn()` (line 107)
- `def test_disk_space_noise_severity()` (line 115)
- `def test_unknown_check_is_ignored_without_rule()` (line 122)
- `def test_custom_rule_overrides_default()` (line 128)
- `def test_missing_check_in_current_triggers_removed_for_list()` (line 136)
- `def test_drifts_summary_counts()` (line 143)
- `def test_default_rules_covers_standard_checks()` (line 160)
