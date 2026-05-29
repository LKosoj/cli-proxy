from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .snapshot_store import SEVERITY_ALARM, SEVERITY_INFO, SEVERITY_NOISE, SEVERITY_WARN

DEFAULT_DRIFT_RULES: Dict[str, Dict[str, str]] = {
    "os.kernel":      {"change": SEVERITY_INFO},
    "os.hostname":    {"change": SEVERITY_WARN},
    "os.os_release":  {"change": SEVERITY_INFO},
    "systemd.running": {"added": SEVERITY_WARN, "removed": SEVERITY_WARN},
    "network.listen": {"added": SEVERITY_ALARM, "removed": SEVERITY_INFO},
    "mounts":         {"added": SEVERITY_WARN, "removed": SEVERITY_WARN, "change": SEVERITY_WARN},
    "disk.space":     {"change": SEVERITY_NOISE},
    "users.regular":  {"added": SEVERITY_ALARM, "removed": SEVERITY_WARN},
    "packages.sample": {"added": SEVERITY_INFO, "removed": SEVERITY_INFO, "change": SEVERITY_INFO},
    "crontab.root":   {"change": SEVERITY_WARN},
    # admin.prereqs — dict {tool_name: bool}. Если хоть один tool исчез (True→False) или
    # появился (False→True) — изменение инфраструктуры, показываем как info.
    "admin.prereqs":  {"added": SEVERITY_INFO, "removed": SEVERITY_INFO, "change": SEVERITY_INFO},
}


@dataclass
class DriftRecord:
    check_id: str
    severity: str
    kind: str
    prev_value: Any = None
    new_value: Any = None
    details: Dict[str, Any] = field(default_factory=dict)


def compare_baselines(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    rules: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> List[DriftRecord]:
    """
    Сравнивает baseline vs current (результат scan()). Возвращает список DriftRecord.
    Каждый record описывает конкретное отклонение (added/removed/change).
    """
    rules_map = dict(rules or DEFAULT_DRIFT_RULES)
    prev_checks = dict((baseline or {}).get("checks") or {})
    new_checks = dict((current or {}).get("checks") or {})
    drifts: List[DriftRecord] = []

    seen = set()
    for check_id, new_val in new_checks.items():
        seen.add(check_id)
        prev_val = prev_checks.get(check_id)
        drifts.extend(_classify_check(check_id, prev_val, new_val, rules_map.get(check_id)))

    for check_id, prev_val in prev_checks.items():
        if check_id in seen:
            continue
        drifts.extend(_classify_check(check_id, prev_val, None, rules_map.get(check_id)))

    return drifts


def _classify_check(
    check_id: str,
    prev: Any,
    new: Any,
    rule: Optional[Mapping[str, str]],
) -> List[DriftRecord]:
    if _values_equal(prev, new):
        return []
    if rule is None:
        return []

    if isinstance(prev, list) or isinstance(new, list):
        return _classify_list_change(check_id, prev or [], new or [], rule)
    if isinstance(prev, dict) or isinstance(new, dict):
        return _classify_dict_change(check_id, prev or {}, new or {}, rule)
    return _classify_scalar_change(check_id, prev, new, rule)


def _classify_list_change(
    check_id: str,
    prev_list: List[Any],
    new_list: List[Any],
    rule: Mapping[str, str],
) -> List[DriftRecord]:
    prev_set = set(_hashable(v) for v in prev_list)
    new_set = set(_hashable(v) for v in new_list)
    added = sorted(new_set - prev_set)
    removed = sorted(prev_set - new_set)
    records: List[DriftRecord] = []
    if added and "added" in rule:
        records.append(DriftRecord(
            check_id=check_id,
            severity=rule["added"],
            kind="added",
            prev_value=prev_list,
            new_value=new_list,
            details={"added": list(added)},
        ))
    if removed and "removed" in rule:
        records.append(DriftRecord(
            check_id=check_id,
            severity=rule["removed"],
            kind="removed",
            prev_value=prev_list,
            new_value=new_list,
            details={"removed": list(removed)},
        ))
    return records


def _classify_dict_change(
    check_id: str,
    prev: Mapping[str, Any],
    new: Mapping[str, Any],
    rule: Mapping[str, str],
) -> List[DriftRecord]:
    added_keys = sorted(set(new.keys()) - set(prev.keys()))
    removed_keys = sorted(set(prev.keys()) - set(new.keys()))
    changed = {
        k: {"from": prev[k], "to": new[k]}
        for k in sorted(set(prev.keys()) & set(new.keys()))
        if not _values_equal(prev[k], new[k])
    }
    records: List[DriftRecord] = []
    if added_keys and "added" in rule:
        records.append(DriftRecord(
            check_id=check_id,
            severity=rule["added"],
            kind="added",
            prev_value=dict(prev),
            new_value=dict(new),
            details={"added_keys": added_keys, "added": {k: new[k] for k in added_keys}},
        ))
    if removed_keys and "removed" in rule:
        records.append(DriftRecord(
            check_id=check_id,
            severity=rule["removed"],
            kind="removed",
            prev_value=dict(prev),
            new_value=dict(new),
            details={"removed_keys": removed_keys, "removed": {k: prev[k] for k in removed_keys}},
        ))
    if changed and "change" in rule:
        records.append(DriftRecord(
            check_id=check_id,
            severity=rule["change"],
            kind="change",
            prev_value=dict(prev),
            new_value=dict(new),
            details={"changed": changed},
        ))
    return records


def _classify_scalar_change(
    check_id: str,
    prev: Any,
    new: Any,
    rule: Mapping[str, str],
) -> List[DriftRecord]:
    if "change" not in rule:
        return []
    return [DriftRecord(
        check_id=check_id,
        severity=rule["change"],
        kind="change",
        prev_value=prev,
        new_value=new,
        details={"from": prev, "to": new},
    )]


def _values_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if isinstance(a, list) and isinstance(b, list):
        return sorted(_hashable(v) for v in a) == sorted(_hashable(v) for v in b)
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_equal(a[k], b[k]) for k in a)
    return a == b


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value


def drifts_summary(drifts: List[DriftRecord]) -> Dict[str, int]:
    counts = {SEVERITY_NOISE: 0, SEVERITY_INFO: 0, SEVERITY_WARN: 0, SEVERITY_ALARM: 0}
    for d in drifts:
        if d.severity in counts:
            counts[d.severity] += 1
    return counts


__all__ = [
    "DEFAULT_DRIFT_RULES",
    "DriftRecord",
    "compare_baselines",
    "drifts_summary",
]
