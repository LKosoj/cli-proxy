from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from i18n import t
from tg.markdown import escape_markdown_v2_all


def _esc(value: Any) -> str:
    return escape_markdown_v2_all(str(value or ""))


def _truncate(value: Any, max_len: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def build_admin_menu_text(*, session_id: str, active: bool, lang: str = 'ru') -> str:
    stage = t('admin.state.enabled', lang) if bool(active) else t('admin.state.disabled', lang)
    sid = _esc(session_id or "-")
    return "\n".join(
        [
            t('admin.menu.title', lang),
            f"*Session:* {sid}",
            f"{t('admin.menu.state_label', lang)} {_esc(stage)}",
            "",
            t('admin.menu.subtitle', lang),
            t('admin.menu.hint', lang),
        ]
    )


def build_admin_status_text(payload: Dict[str, Any], lang: str = 'ru') -> str:
    stage = t('admin.state.enabled', lang) if bool(payload.get("active")) else t('admin.state.disabled', lang)
    busy_3sig = bool(payload.get("busy") or payload.get("run_lock_locked") or payload.get("tick_active"))
    mode_tasks = bool(payload.get("mode_tasks_running"))
    sid = _esc(payload.get("session_id") or "-")
    pending_skill_installs = payload.get("pending_skill_installs") if isinstance(payload.get("pending_skill_installs"), dict) else {}
    pending_skill_count = int(pending_skill_installs.get("count") or 0)
    mute_state = payload.get("mute_state") if isinstance(payload.get("mute_state"), dict) else {}
    mute_text = t('admin.bool.yes', lang) if bool(mute_state.get("muted")) else t('admin.bool.no', lang)
    pipeline_status = str(payload.get("pipeline_status") or "-")
    monitor_status = str(payload.get("monitor_status") or "-")
    analyzer_status = str(payload.get("analyzer_status") or "-")
    executor_status = str(payload.get("executor_status") or "-")
    notifier_status = str(payload.get("notifier_status") or "-")
    incidents = payload.get("recent_incidents") if isinstance(payload.get("recent_incidents"), list) else []
    approvals = payload.get("approved_overrides") if isinstance(payload.get("approved_overrides"), list) else []
    return "\n".join(
        [
            t('admin.status.title', lang),
            f"*Session:* {sid}",
            f"{t('admin.status.mode_label', lang)} {_esc(stage)}",
            f"*Pipeline:* {_esc(pipeline_status)}",
            f"*Monitor:* {_esc(monitor_status)} | *Analyzer:* {_esc(analyzer_status)}",
            f"*Executor:* {_esc(executor_status)} | *Notifier:* {_esc(notifier_status)}",
            "*Busy 3sig:* " + _esc(t('admin.bool.yes', lang) if busy_3sig else t('admin.bool.no', lang))
            + " | *Mode tasks:* " + _esc(t('admin.bool.present', lang) if mode_tasks else t('admin.bool.no', lang)),
            f"*Mute:* {_esc(mute_text)}",
            f"*Incidents:* {_esc(len(incidents))} | *Approvals:* {_esc(len(approvals))} | *Skills:* {_esc(pending_skill_count)}",
        ]
    )


def build_admin_approval_prompt(
    *,
    action: str,
    confidence: str,
    diagnosis: str,
    reason: str,
    triggers: list[str],
    lang: str = 'ru',
) -> str:
    trigger_map = {
        "low_confidence": t('admin.approval_prompt.trigger.low_confidence', lang),
        "risky_action": t('admin.approval_prompt.trigger.risky_action', lang),
        "signal_conflict": t('admin.approval_prompt.trigger.signal_conflict', lang),
        "policy_conflict": t('admin.approval_prompt.trigger.policy_conflict', lang),
    }
    trigger_text = ", ".join(trigger_map.get(str(item or ""), str(item or "")) for item in triggers if str(item or "").strip())
    if not trigger_text:
        trigger_text = t('admin.approval_prompt.trigger.manual_check', lang)
    return "\n".join(
        [
            t('admin.approval_prompt.header', lang),
            f"{t('admin.approval_prompt.trigger_reason_label', lang)} {trigger_text}",
            f"Action: {str(action or '-').strip() or '-'}",
            f"Confidence: {str(confidence or '-').strip() or '-'}",
            f"Diagnosis: {str(diagnosis or '-').strip() or '-'}",
            f"Reason: {str(reason or '-').strip() or '-'}",
            "",
            t('admin.approval_prompt.confirm_question', lang),
        ]
    )


def merge_menu_with_note(*, menu_text: str, note: str) -> str:
    clean_note = str(note or "").strip()
    if not clean_note:
        return str(menu_text or "")
    return f"_📌 {_esc(clean_note)}_\n\n{str(menu_text or '')}"


def build_admin_error_text(message: str) -> str:
    return f"*🛡 Admin*\n{_esc(message)}"


def build_admin_incidents_screen(*, incidents: Iterable[Mapping[str, Any]], lang: str = 'ru') -> str:
    rows = [dict(row or {}) for row in incidents if isinstance(row, Mapping)]
    if not rows:
        return f"*🚨 Incidents*\n\n{t('admin.incidents.empty', lang)}"
    lines = ["*🚨 Incidents*", ""]
    for index, row in enumerate(rows, start=1):
        incident_id = str(row.get("incident_id") or "-")
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
        severity = str(payload.get("severity") or decision.get("urgency") or payload.get("level") or "-")
        incident_type = _truncate(decision.get("incident_type") or payload.get("incident_type") or "")
        diagnosis = _truncate(
            decision.get("diagnosis")
            or payload.get("diagnosis")
            or decision.get("reason")
            or payload.get("reason")
            or payload.get("alert_id")
            or "-"
        )
        ack = "✅" if payload.get("acknowledged") else "⚪"
        lines.append(f"{index}\\. {ack} `{_esc(incident_id)}` \\| severity=*{_esc(severity)}*")
        if incident_type:
            lines.append(f"   type: `{_esc(incident_type)}`")
        if diagnosis and diagnosis != "-":
            lines.append(f"   _{_esc(diagnosis)}_")
        evidence = decision.get("evidence") if isinstance(decision.get("evidence"), list) else []
        first_evidence = evidence[0] if evidence and isinstance(evidence[0], Mapping) else {}
        evidence_ref = _truncate(first_evidence.get("ref") or first_evidence.get("metric") or "")
        if evidence_ref:
            lines.append(f"   evidence: `{_esc(evidence_ref)}`")
    lines.extend(["", t('admin.incidents.ack_hint', lang)])
    return "\n".join(lines)


def build_admin_actions_screen(*, actions: Iterable[Mapping[str, Any]], lang: str = 'ru') -> str:
    rows = [dict(row or {}) for row in actions if isinstance(row, Mapping)]
    if not rows:
        return f"*⚙️ Admin Actions*\n\n{t('admin.actions.empty', lang)}"
    lines = ["*⚙️ Admin Actions*", ""]
    for index, row in enumerate(rows, start=1):
        action_id = str(row.get("action_id") or "-")
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        success = "✅" if payload.get("success", False) else "❌"
        command = _truncate(payload.get("command") or payload.get("action") or "-")
        lines.append(f"{index}\\. {success} `{_esc(action_id)}`")
        if command and command != "-":
            lines.append(f"   `{_esc(command)}`")
    return "\n".join(lines)


def build_admin_approvals_screen(*, approvals: Iterable[Mapping[str, Any]], lang: str = 'ru') -> str:
    rows = [dict(row or {}) for row in approvals if isinstance(row, Mapping)]
    if not rows:
        return f"*✅ Approvals*\n\n{t('admin.approvals.empty', lang)}"
    lines = ["*✅ Approvals*", ""]
    for index, row in enumerate(rows, start=1):
        override_id = str(row.get("override_id") or "-")
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        action = str(payload.get("action") or payload.get("action_id") or "-")
        reason = _truncate(payload.get("reason") or "-")
        lines.append(f"{index}\\. `{_esc(override_id)}` → action=*{_esc(action)}*")
        if reason and reason != "-":
            lines.append(f"   _{_esc(reason)}_")
    lines.extend(["", t('admin.approvals.revoke_hint', lang)])
    return "\n".join(lines)


def build_admin_skills_screen(*, skills: Iterable[Mapping[str, Any]], lang: str = 'ru') -> str:
    rows = [dict(row or {}) for row in skills if isinstance(row, Mapping)]
    if not rows:
        return f"*🧩 Skill approvals*\n\n{t('admin.skills.empty', lang)}"
    lines = ["*🧩 Skill approvals*", ""]
    for index, row in enumerate(rows, start=1):
        approval_id = str(row.get("approval_id") or "-")
        skill_id = str(row.get("skill_id") or "-")
        source = str(row.get("source") or row.get("acquisition_source") or "-")
        mode_phase = "/".join(
            token
            for token in (str(row.get("mode_id") or "").strip(), str(row.get("phase") or "").strip())
            if token
        )
        lines.append(f"{index}\\. `{_esc(approval_id)}` → *{_esc(skill_id)}*")
        detail_parts = []
        if mode_phase:
            detail_parts.append(f"mode={mode_phase}")
        if source and source != "-":
            detail_parts.append(f"source={source}")
        if detail_parts:
            lines.append(f"   _{_esc(' | '.join(detail_parts))}_")
    lines.extend(["", t('admin.skills.approve_hint', lang)])
    return "\n".join(lines)


def build_admin_runs_screen(*, runs: Iterable[Mapping[str, Any]], lang: str = 'ru') -> str:
    rows = [dict(row or {}) for row in runs if isinstance(row, Mapping)]
    if not rows:
        return f"*📜 Pipeline runs*\n\n{t('admin.runs.empty', lang)}"
    lines = ["*📜 Pipeline runs*", ""]
    for index, row in enumerate(rows, start=1):
        run_id = str(row.get("run_id") or "-")
        status = str(row.get("status") or "-")
        phase = str(row.get("phase") or "-")
        started = row.get("started_at") or row.get("created_at") or "-"
        icon = {
            "completed": "✅",
            "ok": "✅",
            "failed": "❌",
            "running": "🔄",
            "cancelled": "⛔",
        }.get(status.lower(), "•")
        lines.append(
            f"{index}\\. {icon} `{_esc(run_id)}` — *{_esc(status)}* \\| phase=*{_esc(phase)}*"
        )
        lines.append(f"   _started: {_esc(started)}_")
    return "\n".join(lines)


def build_admin_mute_screen(*, mute_state: Optional[Mapping[str, Any]], lang: str = 'ru') -> str:
    state = dict(mute_state or {})
    muted = bool(state.get("muted"))
    until = state.get("muted_until_ts")
    if muted:
        return (
            "*🔕 Mute alerts*\n\n"
            + t('admin.mute.active', lang, until=_esc(until))
        )
    return (
        "*🔕 Mute alerts*\n\n"
        + t('admin.mute.inactive', lang)
    )


def build_admin_dryrun_screen(*, dry_run: bool, lang: str = 'ru') -> str:
    mode = t('admin.state.enabled', lang) if bool(dry_run) else t('admin.state.disabled', lang)
    hint = (
        t('admin.dryrun.hint_off', lang)
        if not dry_run
        else t('admin.dryrun.hint_on', lang)
    )
    return (
        t('admin.dryrun.title', lang) + "\n\n"
        + t('admin.dryrun.state_line', lang, mode=_esc(mode), hint=_esc(hint))
    )


def build_admin_action_result_text(*, title: str, body: str) -> str:
    return "\n".join(
        [
            f"*🛡 {_esc(title)}*",
            "",
            _esc(body),
        ]
    )


_STATUS_ICON = {
    "alarm": "🔴",
    "warn": "🟡",
    "proposed_baseline": "📋",
    "no_baseline": "🆕",
    "ok": "🟢",
}

_SEVERITY_ICON = {
    "alarm": "🔴",
    "warn": "🟡",
    "info": "🔵",
    "noise": "⚪",
}


def build_admin_servers_screen(
    *,
    servers: Iterable[Mapping[str, Any]],
    totals: Optional[Mapping[str, Any]] = None,
    lang: str = 'ru',
) -> str:
    rows = [dict(row or {}) for row in servers if isinstance(row, Mapping)]
    if not rows:
        return f"{t('admin.servers.title', lang)}\n\n{t('admin.servers.empty', lang)}"
    lines = [t('admin.servers.title', lang)]
    if totals:
        summary_parts = [
            f"{t('admin.servers.total_prefix', lang)}{_esc(totals.get('server_count') or len(rows))}",
        ]
        stats = totals.get("statuses") if isinstance(totals.get("statuses"), Mapping) else {}
        for key in ("alarm", "warn", "proposed_baseline", "no_baseline", "ok"):
            cnt = int(stats.get(key) or 0)
            if cnt:
                icon = _STATUS_ICON.get(key, "•")
                summary_parts.append(f"{icon}{cnt}")
        lines.append("_" + _esc(" · ".join(summary_parts)) + "_")
    lines.append("")
    for index, row in enumerate(rows, start=1):
        sid = str(row.get("server_id") or "-")
        label = str(row.get("label") or sid)
        transport = str(row.get("transport") or "-")
        host = str(row.get("host") or "").strip()
        status = str(row.get("status") or "-")
        icon = _STATUS_ICON.get(status, "•")
        drifts = row.get("open_drifts") if isinstance(row.get("open_drifts"), Mapping) else {}
        alarm = int(drifts.get("alarm") or 0)
        warn = int(drifts.get("warn") or 0)
        tail_parts = []
        if alarm:
            tail_parts.append(f"🔴{alarm}")
        if warn:
            tail_parts.append(f"🟡{warn}")
        if not row.get("baseline_present"):
            tail_parts.append("🆕")
        if row.get("has_proposed_baseline"):
            tail_parts.append("📋")
        tail = (" \\| " + _esc(" ".join(tail_parts))) if tail_parts else ""
        line = f"{index}\\. {icon} *{_esc(label)}* `{_esc(sid)}`{tail}"
        lines.append(line)
        meta_parts = [f"transport={transport}"]
        if host:
            meta_parts.append(f"host={host}")
        lines.append(f"   _{_esc(' | '.join(meta_parts))}_")
    lines.append("")
    lines.append(t('admin.servers.detail_hint', lang))
    return "\n".join(lines)


def build_admin_server_detail_screen(*, summary: Mapping[str, Any], lang: str = 'ru') -> str:
    if not isinstance(summary, Mapping) or not summary:
        return f"*🖥 Сервер*\n\n{t('admin.server_detail.no_data', lang)}"
    sid = str(summary.get("server_id") or "-")
    label = str(summary.get("label") or sid)
    transport = str(summary.get("transport") or "-")
    host = str(summary.get("host") or "-")
    tags = summary.get("tags") if isinstance(summary.get("tags"), (list, tuple)) else []
    baseline_present = bool(summary.get("baseline_present"))
    has_proposed = bool(summary.get("has_proposed_baseline"))
    last_scan = summary.get("last_scan_ts") or "-"
    drifts = summary.get("open_drifts") if isinstance(summary.get("open_drifts"), Mapping) else {}
    mem_entries = int(summary.get("memory_entries") or 0)
    status = str(summary.get("status") or "-")
    icon = _STATUS_ICON.get(status, "•")
    baseline_line = t('admin.baseline.present', lang) if baseline_present else t('admin.baseline.absent', lang)
    if has_proposed:
        baseline_line += t('admin.baseline.has_proposed', lang)
    lines = [
        t('admin.server_detail.title', lang, label=_esc(label)),
        "",
        f"*id:* `{_esc(sid)}`",
        f"*status:* {icon} *{_esc(status)}*",
        f"*transport:* {_esc(transport)}" + (f" \\| host: `{_esc(host)}`" if host and host != "-" else ""),
    ]
    if tags:
        tag_text = ", ".join(str(tag) for tag in tags if str(tag or "").strip())
        if tag_text:
            lines.append(f"*tags:* {_esc(tag_text)}")
    lines.append(f"*baseline:* {_esc(baseline_line)}")
    lines.append(f"*last scan:* {_esc(last_scan)}")
    drift_parts = []
    for key in ("alarm", "warn", "info", "noise"):
        cnt = int(drifts.get(key) or 0)
        if cnt:
            drift_parts.append(f"{_SEVERITY_ICON.get(key, '•')}{cnt}")
    drift_text = " ".join(drift_parts) if drift_parts else t('admin.drifts.none_open', lang)
    lines.append(f"*drifts:* {_esc(drift_text)}")
    lines.append(f"*memory entries:* {_esc(mem_entries)}")
    return "\n".join(lines)


def build_admin_baseline_screen(
    *,
    server_id: str,
    info: Mapping[str, Any],
    max_preview_chars: int = 1200,
    lang: str = 'ru',
) -> str:
    if not isinstance(info, Mapping):
        info = {}
    baseline = info.get("baseline") if isinstance(info.get("baseline"), Mapping) else {}
    proposed = info.get("proposed") if isinstance(info.get("proposed"), Mapping) else None
    has_proposed = bool(info.get("has_proposed"))
    has_prev = bool(info.get("has_prev"))
    checks = baseline.get("checks") if isinstance(baseline.get("checks"), Mapping) else {}
    lines = [
        f"*📋 Baseline {_esc(server_id)}*",
        "",
        f"*active:* {_esc(t('admin.bool.yes', lang) if baseline else t('admin.bool.no', lang))}",
        f"*proposed:* {_esc(t('admin.bool.yes', lang) if has_proposed else t('admin.bool.no', lang))}",
        f"*prev:* {_esc(t('admin.bool.present', lang) if has_prev else t('admin.bool.no', lang))}",
        f"*scanned\\_at:* {_esc(baseline.get('scanned_at') or '-')}",
        f"*checks:* {_esc(len(checks))}",
    ]
    if checks:
        preview_keys = sorted(str(k) for k in checks.keys())[:12]
        lines.append("")
        lines.append(t('admin.baseline.checks_list', lang))
        for key in preview_keys:
            lines.append(f"• `{_esc(key)}`")
        extra = len(checks) - len(preview_keys)
        if extra > 0:
            lines.append(t('admin.baseline.more_checks', lang, n=_esc(extra)))
    if has_proposed and isinstance(proposed, Mapping):
        prop_checks = proposed.get("checks") if isinstance(proposed.get("checks"), Mapping) else {}
        diff_keys = sorted(set(prop_checks.keys()) ^ set(checks.keys()))[:8]
        lines.append("")
        lines.append(t('admin.baseline.proposed_header', lang))
        lines.append(f"_proposed checks:_ {_esc(len(prop_checks))}")
        if diff_keys:
            lines.append(t('admin.baseline.changed_keys', lang))
            for key in diff_keys:
                lines.append(f"• `{_esc(key)}`")
        lines.append("")
        lines.append(t('admin.baseline.accept_discard_hint', lang))
    return "\n".join(lines)


def build_admin_autonomy_status_screen(status: Mapping[str, Any], lang: str = 'ru') -> str:
    policy = status.get("policy") if isinstance(status.get("policy"), Mapping) else {}
    counters = status.get("counters") if isinstance(status.get("counters"), Mapping) else {}
    enabled = bool(policy.get("enabled"))
    state_icon = "🟢" if enabled else "⚪"
    severities = ", ".join(str(s) for s in policy.get("auto_apply_severities") or []) or "—"
    actions = policy.get("auto_exec_actions") or []
    actions_preview = ", ".join(str(a) for a in actions[:5]) or "—"
    if len(actions) > 5:
        actions_preview += f" …(+{len(actions) - 5})"
    last_tick_age = counters.get("last_tick_age_sec")
    if last_tick_age is None:
        last_tick_text = t('admin.autonomy.last_tick_never', lang)
    elif last_tick_age < 60:
        last_tick_text = t('admin.autonomy.last_tick_sec', lang, n=int(last_tick_age))
    elif last_tick_age < 3600:
        last_tick_text = t('admin.autonomy.last_tick_min', lang, n=int(last_tick_age // 60))
    else:
        last_tick_text = t('admin.autonomy.last_tick_hour', lang, n=int(last_tick_age // 3600))
    lines = [
        "*🤖 Admin Autonomy*",
        "",
        t('admin.autonomy.state_label', lang) + " " + state_icon + " " + _esc(
            t('admin.autonomy.state.enabled', lang) if enabled
            else t('admin.autonomy.state.disabled', lang)
        ),
        f"*Auto\\-apply severities:* {_esc(severities)}",
        f"*Auto\\-exec allowlist:* {_esc(actions_preview)}",
        f"*Rate limit:* {_esc(policy.get('max_actions_per_hour', '-'))}{t('admin.autonomy.per_hour_suffix', lang)} \\| "
        f"*Cooldown:* {_esc(policy.get('cooldown_sec', '-'))}{t('admin.autonomy.cooldown_suffix', lang)}",
        "*Baseline auto\\-accept:* "
        + _esc(t('admin.bool.yes', lang) if policy.get('baseline_auto_accept_enabled') else t('admin.bool.no', lang))
        + f" \\({_esc(t('admin.autonomy.after_stable_scans', lang, n=policy.get('baseline_auto_accept_after_stable_scans', '-')))}\\)",
        "",
        t('admin.autonomy.counters_header', lang),
        f"• ticks: {_esc(counters.get('tick_count', 0))}",
        f"• executed: {_esc(counters.get('actions_executed_total', 0))}",
        f"• escalated: {_esc(counters.get('escalations_total', 0))}",
        f"• ignored: {_esc(counters.get('ignored_total', 0))}",
        f"• denied by policy: {_esc(counters.get('denied_by_policy_total', 0))}",
        f"• baselines auto\\-accepted: {_esc(counters.get('baselines_auto_accepted_total', 0))}",
        f"• last tick: {_esc(last_tick_text)}",
    ]
    return "\n".join(lines)


def build_admin_autonomy_drifts_screen(
    *,
    server_id: str,
    drifts: Iterable[Mapping[str, Any]],
    lang: str = 'ru',
) -> str:
    rows = [dict(row or {}) for row in drifts if isinstance(row, Mapping)]
    if not rows:
        return (
            f"*🚨 Drifts {_esc(server_id)}*\n\n"
            + t('admin.drifts.empty', lang)
        )
    lines = [f"*🚨 Drifts {_esc(server_id)}*", ""]
    for index, row in enumerate(rows, start=1):
        did = row.get("id") or "-"
        check_id = str(row.get("check_id") or "-")
        severity = str(row.get("severity") or "-")
        icon = _SEVERITY_ICON.get(severity, "•")
        prev_val = _truncate(row.get("prev_value"), 60)
        new_val = _truncate(row.get("new_value"), 60)
        ack = "✅" if row.get("acknowledged_at") else "⚪"
        lines.append(
            f"{index}\\. {icon} {ack} `#{_esc(did)}` — `{_esc(check_id)}` \\| *{_esc(severity)}*"
        )
        if prev_val and prev_val != "-":
            lines.append(f"   was: `{_esc(prev_val)}`")
        if new_val and new_val != "-":
            lines.append(f"   now: `{_esc(new_val)}`")
    lines.extend(["", t('admin.drifts.ack_hint', lang)])
    return "\n".join(lines)


def build_admin_memory_screen(
    *,
    server_id: str,
    memory: Mapping[str, Any],
    max_fact_lines: int = 15,
    max_note_chars: int = 1200,
    lang: str = 'ru',
) -> str:
    facts = memory.get("facts") if isinstance(memory.get("facts"), Mapping) else {}
    notes_text = str(memory.get("notes_text") or "")
    stats = memory.get("stats") if isinstance(memory.get("stats"), Mapping) else {}
    entries = int(stats.get("entries") or 0)
    bytes_count = int(stats.get("bytes") or 0)
    lines = [
        f"*🧠 Memory {_esc(server_id)}*",
        "",
        f"*facts:* {_esc(len([k for k in facts.keys() if k != '_meta']))}",
        f"*notes:* entries={_esc(entries)} \\| bytes={_esc(bytes_count)}",
    ]
    fact_keys = [k for k in facts.keys() if k != "_meta"][:max_fact_lines]
    if fact_keys:
        lines.append("")
        lines.append("*Facts:*")
        for key in fact_keys:
            value = facts.get(key)
            value_text = _truncate(value, 80)
            lines.append(f"• `{_esc(key)}` \\= `{_esc(value_text)}`")
        extra = len([k for k in facts.keys() if k != "_meta"]) - len(fact_keys)
        if extra > 0:
            lines.append(t('admin.memory.more_facts', lang, n=_esc(extra)))
    if notes_text:
        lines.append("")
        lines.append("*Notes \\(хвост\\):*")
        tail = notes_text[-max_note_chars:] if len(notes_text) > max_note_chars else notes_text
        lines.append(f"```\n{tail}\n```")
    return "\n".join(lines)


def build_admin_runbook_catalog_screen(
    *,
    server_id: Optional[str],
    runbooks: Iterable[Mapping[str, Any]],
    lang: str = 'ru',
) -> str:
    rows = [dict(row or {}) for row in runbooks if isinstance(row, Mapping)]
    title = "*📚 Runbooks*"
    if server_id:
        title = t('admin.runbooks.title_for_server', lang, server_id=_esc(server_id))
    if not rows:
        return f"{title}\n\n{t('admin.runbooks.empty', lang)}"
    lines = [title, ""]
    for index, row in enumerate(rows, start=1):
        rb_id = str(row.get("id") or "-")
        rb_title = str(row.get("title") or rb_id)
        tags = row.get("tags") if isinstance(row.get("tags"), (list, tuple)) else []
        owner = str(row.get("owner") or "-")
        lines.append(f"{index}\\. `{_esc(rb_id)}` — *{_esc(rb_title)}*")
        if tags:
            tag_text = ", ".join(str(tag) for tag in tags if str(tag or "").strip())
            if tag_text:
                lines.append(f"   _tags: {_esc(tag_text)}_")
        if owner and owner != "-":
            lines.append(f"   _owner: {_esc(owner)}_")
    return "\n".join(lines)


def build_admin_runbook_detail_screen(*, runbook: Mapping[str, Any], max_body_chars: int = 1800, lang: str = 'ru') -> str:
    if not isinstance(runbook, Mapping) or not runbook:
        return f"*📚 Runbook*\n\n{t('admin.runbook.not_found', lang)}"
    rb_id = str(runbook.get("id") or "-")
    title = str(runbook.get("title") or rb_id)
    tags = runbook.get("tags") if isinstance(runbook.get("tags"), (list, tuple)) else []
    owner = str(runbook.get("owner") or "-")
    body = str(runbook.get("body") or "").strip()
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n…"
    lines = [
        f"*📚 {_esc(title)}*",
        f"`{_esc(rb_id)}`",
        "",
    ]
    if tags:
        tag_text = ", ".join(str(tag) for tag in tags if str(tag or "").strip())
        if tag_text:
            lines.append(f"*tags:* {_esc(tag_text)}")
    if owner and owner != "-":
        lines.append(f"*owner:* {_esc(owner)}")
    if body:
        lines.append("")
        lines.append(f"```\n{body}\n```")
    return "\n".join(lines)


def build_admin_runbook_validation_screen(*, rb_id: str, report: Mapping[str, Any]) -> str:
    ok = bool(report.get("ok"))
    icon = "✅" if ok else "❌"
    lines = [
        f"*{icon} Validate `{_esc(rb_id)}`*",
        "",
        f"*ok:* {_esc(ok)}",
    ]
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    if checks:
        lines.append("")
        lines.append("*Checks:*")
        for c in checks[:10]:
            name = str(c.get("step") or "-")
            chk = str(c.get("checksum") or "-")
            bn = str(c.get("bash_n") or "-")
            sc = str(c.get("shellcheck") or "-")
            lines.append(
                f"• `{_esc(name)}` checksum={_esc(chk)} bash\\-n={_esc(bn)} shellcheck={_esc(sc)}",
            )
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if errors:
        lines.append("")
        lines.append("*Errors:*")
        for e in errors[:10]:
            lines.append(f"• {_esc(_truncate(e, 300))}")
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    if warnings:
        lines.append("")
        lines.append("*Warnings:*")
        for w in warnings[:5]:
            lines.append(f"• {_esc(_truncate(w, 300))}")
    return "\n".join(lines)


def build_admin_runbook_promote_screen(*, rb_id: str, result: Mapping[str, Any]) -> str:
    added = result.get("added_servers") if isinstance(result.get("added_servers"), list) else []
    already = result.get("already_present") if isinstance(result.get("already_present"), list) else []
    conf_before = result.get("confidence_before")
    conf_after = result.get("confidence_after")
    validation = result.get("validation") if isinstance(result.get("validation"), Mapping) else None
    lines = [
        f"*🚀 Promote `{_esc(rb_id)}`*",
        "",
        f"*added:* {_esc(', '.join(added) or '—')}",
    ]
    if already:
        lines.append(f"*already present:* {_esc(', '.join(already))}")
    if conf_before is not None or conf_after is not None:
        lines.append(f"*confidence:* {_esc(conf_before)} → {_esc(conf_after)}")
    if validation is not None:
        v_ok = bool(validation.get("ok"))
        lines.append(f"*validation:* {'✅ ok' if v_ok else '❌ failed'}")
    return "\n".join(lines)


def build_admin_rescan_report_screen(*, server_id: str, report: Mapping[str, Any]) -> str:
    ok = bool(report.get("ok"))
    icon = "✅" if ok else "❌"
    parts = [
        f"*{icon} Rescan {_esc(server_id)}*",
        "",
        f"*ok:* {_esc(ok)}",
        f"*baseline\\_present:* {_esc(bool(report.get('baseline_present')))}",
        f"*snapshots\\_written:* {_esc(int(report.get('snapshots_written') or 0))}",
        f"*drifts\\_written:* {_esc(int(report.get('drifts_written') or 0))}",
    ]
    by_sev = report.get("drifts_by_severity") if isinstance(report.get("drifts_by_severity"), Mapping) else {}
    if by_sev:
        items = [f"{_SEVERITY_ICON.get(k, '•')}{int(v or 0)}" for k, v in by_sev.items() if int(v or 0)]
        if items:
            parts.append(f"*by severity:* {_esc(' '.join(items))}")
    err = str(report.get("error") or "").strip()
    if err:
        parts.append("")
        parts.append(f"*error:* {_esc(err)}")
    return "\n".join(parts)


def build_admin_run_detail_text(*, run: Mapping[str, Any], lang: str = 'ru') -> str:
    if not isinstance(run, Mapping) or not run:
        return f"*📜 Pipeline run*\n\n{t('admin.run_detail.no_data', lang)}"
    run_id = str(run.get("run_id") or "-")
    status = str(run.get("status") or "-")
    phase = str(run.get("phase") or "-")
    started = run.get("started_at") or "-"
    finished = run.get("finished_at") or "-"
    events = run.get("events") if isinstance(run.get("events"), list) else []
    lines = [
        "*📜 Pipeline run*",
        "",
        f"*run_id:* `{_esc(run_id)}`",
        f"*status:* {_esc(status)}",
        f"*phase:* {_esc(phase)}",
        f"*started:* {_esc(started)}",
        f"*finished:* {_esc(finished)}",
    ]
    if events:
        lines.append("")
        lines.append(t('admin.run_detail.events_header', lang))
        for item in list(events)[-5:]:
            if not isinstance(item, Mapping):
                continue
            event_type = str(item.get("event_type") or item.get("type") or "-")
            event_status = str(item.get("status") or "-")
            lines.append(f"• `{_esc(event_type)}` status=*{_esc(event_status)}*")
    return "\n".join(lines)
