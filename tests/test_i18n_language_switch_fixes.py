"""Regression tests for the language-switch fixes.

Covers the bugs diagnosed when "changing language in the session menu did not
change the output text":
  #1 — session status body localization (build_session_status_text)
  #2 — CLI agent response-language directive injected into Session.run_prompt
  run.* error/header locale keys present and formatting works
"""
from __future__ import annotations

import asyncio
import types

from i18n import t
from sessions.session_status import build_session_status_text


def _cfg(user_languages=None, default_language="ru"):
    return types.SimpleNamespace(
        telegram=types.SimpleNamespace(user_languages=user_languages or {}),
        defaults=types.SimpleNamespace(default_language=default_language),
    )


def _status_session():
    return types.SimpleNamespace(
        id="s1",
        name="demo",
        workdir="/tmp/demo",
        busy=True,
        git=None,
        git_busy=False,
        git_conflict=False,
        started_at=1000,
        last_output_ts=1100,
        last_tick_ts=1150,
        tick_seen=3,
        queue=[],
        resume_token="tok",
        tool=types.SimpleNamespace(name="codex"),
        runtime_progress_last_event=None,
    )


# ---------------------------------------------------------------------------
# #1 status body localization
# ---------------------------------------------------------------------------

def _render_status(lang, monkeypatch):
    import sessions.session_status as ss

    monkeypatch.setattr(ss, "load_ssh_config", lambda _w: {})
    monkeypatch.setattr(ss, "get_active_mode", lambda _s, _d: "")
    monkeypatch.setattr(ss, "is_orchestrator_enabled", lambda _s, _d: False)
    monkeypatch.setattr(ss.time, "time", lambda: 2000)
    return build_session_status_text(_status_session(), lang=lang)


def test_status_body_russian(monkeypatch):
    text = _render_status("ru", monkeypatch)
    assert "Активная сессия" in text
    assert "занята" in text
    assert "Статус" in text
    assert "Очередь" in text
    assert "Resume: tok" in text
    assert "Оркестратор: выкл" in text


def test_status_body_english(monkeypatch):
    text = _render_status("en", monkeypatch)
    assert "Active session" in text
    assert "busy" in text
    assert "Status" in text
    assert "Queue" in text
    assert "Resume: tok" in text
    assert "Orchestrator: off" in text
    # The switch must actually change the text — no Russian leaking through.
    assert "Активная сессия" not in text
    assert "занята" not in text


def test_status_body_changes_with_language(monkeypatch):
    ru = _render_status("ru", monkeypatch)
    en = _render_status("en", monkeypatch)
    de = _render_status("de", monkeypatch)
    assert ru != en != de
    assert "Aktive Sitzung" in de


# ---------------------------------------------------------------------------
# #2 CLI agent response-language directive
# ---------------------------------------------------------------------------

def _bare_session(chat_id, user_languages, default_language="ru"):
    from session import Session

    s = Session.__new__(Session)
    s.config = _cfg(user_languages=user_languages, default_language=default_language)
    s.chat_id = chat_id
    return s


def test_cli_directive_empty_for_fallback_language():
    s = _bare_session(777, {777: "ru"})
    assert s._cli_language_directive() == ""


def test_cli_directive_present_for_english():
    s = _bare_session(555, {555: "en"})
    directive = s._cli_language_directive()
    assert directive
    assert "English" in directive


def test_cli_directive_uses_default_language():
    # No explicit per-user choice, default is German → directive in German.
    s = _bare_session(999, {}, default_language="de")
    directive = s._cli_language_directive()
    assert directive
    assert "Deutsch" in directive


def test_run_prompt_prepends_directive_for_non_ru():
    s = _bare_session(555, {555: "en"})
    s.tool = types.SimpleNamespace(mode="headless", name="codex")
    captured = {}

    async def _fake_headless(prompt, force_fresh=False, **_kw):
        captured["prompt"] = prompt
        return "ok"

    s._run_headless = _fake_headless
    out = asyncio.run(s.run_prompt("Build the feature"))
    assert out == "ok"
    assert "Build the feature" in captured["prompt"]
    assert "English" in captured["prompt"]
    # User request must remain intact at the tail; directive is a prefix.
    assert captured["prompt"].rstrip().endswith("Build the feature")


def test_run_prompt_unchanged_for_ru():
    s = _bare_session(777, {777: "ru"})
    s.tool = types.SimpleNamespace(mode="headless", name="codex")
    captured = {}

    async def _fake_headless(prompt, force_fresh=False, **_kw):
        captured["prompt"] = prompt
        return "ok"

    s._run_headless = _fake_headless
    asyncio.run(s.run_prompt("Собери фичу"))
    assert captured["prompt"] == "Собери фичу"


# ---------------------------------------------------------------------------
# run.* locale keys
# ---------------------------------------------------------------------------

def test_run_error_keys_localized():
    assert t("run.cli_abnormal", "en", details="s1 @ /tmp") != t("run.cli_abnormal", "ru", details="s1 @ /tmp")
    assert "{e}" not in t("run.exec_error", "de", e="boom")
    assert "{mode_id}" not in t("run.mode_interrupted", "zh", mode_id="manager")
    # output header renders multi-line with all params substituted
    header = t(
        "run.output_header",
        "en",
        sid="s1",
        name="demo",
        tool="codex",
        workdir="/tmp",
        length=4200,
        queue=0,
        resume="yes",
        delivery="tail",
    )
    assert "\n" in header
    assert "{" not in header
