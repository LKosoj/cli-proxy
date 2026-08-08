from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.cli_limits_service import CliLimitsService


def _session(cli_name: str, workdir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{cli_name}-session",
        workdir=str(workdir),
        cli=SimpleNamespace(active_cli=cli_name),
        tool=SimpleNamespace(name=cli_name),
    )


@pytest.mark.asyncio
async def test_describe_for_sessions_reads_codex_limits_from_matching_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "repo-codex"
    project_dir.mkdir()
    codex_root = tmp_path / "codex" / "sessions" / "2026" / "03" / "26"
    codex_root.mkdir(parents=True)
    transcript = codex_root / "rollout-test.jsonl"
    records = [
        {
            "timestamp": "2026-03-26T18:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": str(project_dir)},
        },
        {
            "timestamp": "2026-03-26T18:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 345,
                        "output_tokens": 67,
                        "total_tokens": 1267,
                    }
                },
                "rate_limits": {
                    "primary": {"used_percent": 9.0, "window_minutes": 300, "resets_at": 1770689712},
                    "secondary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": 1771265156},
                    "credits": {"has_credits": False, "unlimited": False, "balance": None},
                },
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
    )
    monkeypatch.setattr(service, "_read_codex_direct_usage", lambda: None)

    text = await service.describe_for_sessions([_session("codex", project_dir)])

    assert "🟢 codex" in text
    assert "📦 codex" in text
    assert "💎 primary" in text
    assert "91%" in text
    assert "💎 secondary" in text
    assert "95%" in text
    assert "📊 1.3K total" in text


@pytest.mark.asyncio
async def test_describe_for_sessions_reads_claude_quota_for_matching_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "repo-claude"
    project_dir.mkdir()
    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
    )
    monkeypatch.setattr(
        service,
        "_read_claude_direct_usage",
        lambda: {
            "five_hour": {"utilization": 4, "resets_at": "2026-03-27T04:00:00+00:00"},
            "seven_day": {"utilization": 31, "resets_at": "2026-03-31T06:00:00+00:00"},
        },
    )

    text = await service.describe_for_sessions([_session("claude", project_dir)])

    assert "🟢 claude" in text
    assert "🤖 claude" in text
    assert "5ч" in text
    assert "96%" in text
    assert "7д" in text
    assert "69%" in text


def test_read_claude_direct_usage_uses_claude_bot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude_home = tmp_path / "claude-bot-home"
    credentials_dir = claude_home / ".claude"
    credentials_dir.mkdir(parents=True)
    credentials_path = credentials_dir / ".credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "oauth-token-123",
                    "refreshToken": "refresh-token-456",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(CliLimitsService, "_home_for_user", staticmethod(lambda username: claude_home))
    service = CliLimitsService(claude_username="claude-bot")
    captured: dict[str, str] = {}

    def _fake_request_json(request: object) -> dict[str, object]:
        assert hasattr(request, "full_url")
        assert getattr(request, "full_url") == service._CLAUDE_USAGE_URL
        assert hasattr(request, "header_items")
        headers = {key.lower(): value for key, value in request.header_items()}
        captured["authorization"] = headers.get("authorization")
        captured["beta"] = headers.get("anthropic-beta")
        return {
            "five_hour": {"utilization": 4, "resets_at": "2026-03-27T04:00:00+00:00"},
            "seven_day": {"utilization": 31, "resets_at": "2026-03-31T06:00:00+00:00"},
        }

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    usage = service._read_claude_direct_usage()

    assert usage is not None
    assert captured["authorization"] == "Bearer oauth-token-123"
    assert captured["beta"] == service._CLAUDE_OAUTH_BETA


@pytest.mark.asyncio
async def test_describe_for_sessions_appends_codex_direct_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = tmp_path / "repo-codex"
    project_dir.mkdir()
    codex_root = tmp_path / "codex" / "sessions" / "2026" / "03" / "27"
    codex_root.mkdir(parents=True)
    transcript = codex_root / "rollout-test.jsonl"
    records = [
        {
            "timestamp": "2026-03-27T10:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": str(project_dir)},
        },
        {
            "timestamp": "2026-03-27T10:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 210,
                    }
                },
                "rate_limits": {
                    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1770689712},
                    "secondary": {"used_percent": 8.0, "window_minutes": 10080, "resets_at": 1771265156},
                },
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
    )
    monkeypatch.setattr(
        service,
        "_read_codex_direct_usage",
        lambda: {
            "plan_type": "chatgpt-plus",
            "email": "user@example.com",
            "primary_window": {
                "used_percent": 18.0,
                "limit_window_seconds": 18_000,
                "reset_at": 1770689712,
                "reset_after_seconds": 900,
            },
            "secondary_window": {
                "used_percent": 25.0,
                "limit_window_seconds": 604_800,
                "reset_at": 1771265156,
                "reset_after_seconds": 5_400,
            },
            "credits": {"unlimited": True},
        },
    )

    text = await service.describe_for_sessions([_session("codex", project_dir)])

    assert "💎 primary" in text
    assert "82%" in text
    assert "💎 secondary" in text
    assert "75%" in text
    assert "📊 210 total" in text
    assert "api:" not in text
    assert "credits:" not in text


@pytest.mark.asyncio
async def test_describe_for_sessions_reads_gemini_direct_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = tmp_path / "repo-gemini"
    project_dir.mkdir()
    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
    )
    monkeypatch.setattr(
        service,
        "_read_gemini_usage_for_workdir",
        lambda workdir: {
            "project_id": "proj-123",
            "models": [
                {
                    "model_id": "gemini-2.5-pro",
                    "token_type": "input",
                    "remaining_fraction": 0.67,
                    "reset_time": "2026-03-27T18:00:00Z",
                },
                {
                    "model_id": "gemini-2.5-flash",
                    "token_type": "output",
                    "remaining_fraction": 0.82,
                    "reset_time": "2026-03-27T19:00:00Z",
                },
            ],
        },
    )

    text = await service.describe_for_sessions([_session("gemini", project_dir)])

    assert "🟢 gemini" in text
    assert "♊ gemini" in text
    assert "gemini-2.5-flash" in text
    assert "82%" in text
    assert "gemini-2.5-pro" in text
    assert "67%" in text


@pytest.mark.asyncio
async def test_describe_for_sessions_reads_grok_local_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "repo-grok"
    project_dir.mkdir()
    encoded = urllib.parse.quote(str(project_dir.resolve()), safe="")
    session_dir = tmp_path / "grok" / "sessions" / encoded / "grok-session-1"
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text(
        json.dumps(
            {
                "current_model_id": "grok-build",
                "generated_title": "Grok local usage",
                "updated_at": "2026-03-27T18:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "signals.json").write_text(
        json.dumps(
            {
                "contextTokensUsed": 50_000,
                "contextWindowTokens": 500_000,
                "turnCount": 4,
                "toolCallCount": 7,
                "userMessageCount": 4,
                "assistantMessageCount": 4,
                "primaryModelId": "grok-build",
            }
        ),
        encoding="utf-8",
    )
    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
        grok_sessions_roots=[tmp_path / "grok" / "sessions"],
    )
    monkeypatch.setattr(
        service,
        "_read_grok_direct_usage",
        lambda: {
            "window": "weekly",
            "used_percent": 25.0,
            "resets_at": "July 11, 17:03 PT",
        },
    )

    text = await service.describe_for_sessions([_session("grok", project_dir)])

    assert "🟢 grok" in text
    assert "✕ grok — repo-grok · grok-build" in text
    assert "context" in text
    assert "90%" in text
    assert "неделя" in text
    assert "75% осталось" in text
    assert "↻July 11, 17:03 PT" in text
    assert "📊 context 50K / 500K" in text
    assert "turns 4 · tools 7 · messages 4/4" in text
    assert "quota: недоступно" not in text


def test_read_grok_direct_usage_uses_isolated_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CliLimitsService(network_timeout_sec=2.0)
    calls: list[list[str]] = []
    call_environments: list[dict[str, str]] = []
    capture_count = 0
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GROK_AUTH_PATH", str(auth_path))

    monkeypatch.setattr(
        "app.services.cli_limits_service.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal capture_count
        calls.append(argv)
        env = kwargs.get("env")
        if isinstance(env, dict):
            call_environments.append(env)
        stdout = ""
        if "capture-pane" in argv:
            capture_count += 1
            stdout = (
                "❯\n"
                if capture_count == 1
                else "Weekly limit: 12.5%\nNext reset: July 11, 17:03 PT\n❯\n"
            )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.services.cli_limits_service.subprocess.run", fake_run)

    usage = service._read_grok_direct_usage()

    assert usage == {
        "window": "weekly",
        "used_percent": 12.5,
        "resets_at": "July 11, 17:03 PT",
    }
    new_session_call = next(call for call in calls if "new-session" in call)
    assert new_session_call[:2] == ["/usr/bin/tmux", "-S"]
    assert "/usr/bin/grok" in new_session_call
    assert "--leader-socket" in new_session_call
    assert "--no-memory" in new_session_call
    assert "--no-subagents" in new_session_call
    assert any("kill-server" in call for call in calls)
    assert call_environments[0]["GROK_AUTH_PATH"] == str(auth_path)
    assert Path(call_environments[0]["GROK_HOME"]).name == "grok-home"
    assert call_environments[0]["GROK_HOME"] != str(Path.home() / ".grok")


def test_parse_grok_direct_usage_supports_monthly_limit() -> None:
    usage = CliLimitsService._parse_grok_direct_usage_output(
        "Monthly limit: 40%\nNext reset: August 1, 00:00 PT\n"
    )

    assert usage == {
        "window": "monthly",
        "used_percent": 40.0,
        "resets_at": "August 1, 00:00 PT",
    }
    assert CliLimitsService._format_grok_direct_quota_line(usage) == (
        "🟡 месяц ██████░░░░ 60% осталось ↻August 1, 00:00 PT"
    )


@pytest.mark.asyncio
async def test_describe_for_sessions_reports_no_active_cli_for_empty_input(tmp_path: Path) -> None:
    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
    )

    text = await service.describe_for_sessions([])

    assert "Нет доступных CLI" in text


def test_format_snapshots_shows_cli_without_active_sessions() -> None:
    service = CliLimitsService()

    text = service.format_snapshots(
        [],
        active_clis=("claude", "codex"),
        available_clis=("claude", "codex", "gemini", "grok", "qwen"),
    )

    assert "🟢 claude · codex" in text
    assert "⚫️ gemini · grok · qwen" in text


@pytest.mark.asyncio
async def test_collect_snapshot_reports_kimi_quota_as_unavailable() -> None:
    service = CliLimitsService()

    snapshot = await service._collect_cli_snapshot("kimi", [])

    assert snapshot.cli_name == "kimi"
    assert snapshot.status == "unavailable"
    assert "kimi" in CliLimitsService.SUPPORTED_CLI_NAMES


@pytest.mark.asyncio
async def test_collect_snapshot_reports_opencode_quota_as_unavailable() -> None:
    service = CliLimitsService()

    snapshot = await service._collect_cli_snapshot("opencode", [])

    assert snapshot.cli_name == "opencode"
    assert snapshot.status == "unavailable"
    assert "opencode" in CliLimitsService.SUPPORTED_CLI_NAMES


def test_refresh_gemini_credentials_uses_client_secret_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    service = CliLimitsService(gemini_oauth_client_secret="config-gemini-secret")
    captured: dict[str, list[str]] = {}

    def _fake_request_json(request: object) -> dict[str, object]:
        data = getattr(request, "data")
        captured.update(urllib.parse.parse_qs(data.decode("utf-8")))
        return {"access_token": "new-token", "token_type": "Bearer", "expires_in": 3600}

    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    refreshed = service._refresh_gemini_credentials({"refresh_token": "refresh-token"})

    assert refreshed is not None
    assert refreshed["access_token"] == "new-token"
    assert captured["client_secret"] == ["config-gemini-secret"]
    assert captured["refresh_token"] == ["refresh-token"]


def test_refresh_gemini_credentials_skips_without_config_client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    service = CliLimitsService()

    def _unexpected_request_json(_request: object) -> dict[str, object]:
        raise AssertionError("Gemini refresh should not call OAuth token endpoint without client secret")

    monkeypatch.setattr(service, "_request_json", _unexpected_request_json)

    assert service._refresh_gemini_credentials({"refresh_token": "refresh-token"}) is None


@pytest.mark.asyncio
async def test_describe_for_sessions_collects_all_available_clis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = tmp_path / "repo-shared"
    project_dir.mkdir()
    codex_root = tmp_path / "codex" / "sessions" / "2026" / "03" / "27"
    codex_root.mkdir(parents=True)
    transcript = codex_root / "rollout-test.jsonl"
    records = [
        {
            "timestamp": "2026-03-27T10:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": str(project_dir)},
        },
        {
            "timestamp": "2026-03-27T10:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 210,
                    }
                },
                "rate_limits": {
                    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1770689712},
                    "secondary": {"used_percent": 8.0, "window_minutes": 10080, "resets_at": 1771265156},
                },
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
    )
    monkeypatch.setattr(service, "_read_codex_direct_usage", lambda: None)
    monkeypatch.setattr(
        service,
        "_read_claude_direct_usage",
        lambda: {
            "five_hour": {"utilization": 4, "resets_at": "2026-03-27T04:00:00+00:00"},
            "seven_day": {"utilization": 31, "resets_at": "2026-03-31T06:00:00+00:00"},
        },
    )
    monkeypatch.setattr(
        service,
        "_read_gemini_usage_for_workdir",
        lambda workdir: {
            "project_id": "proj-123",
            "models": [
                {
                    "model_id": "gemini-2.5-pro",
                    "token_type": "input",
                    "remaining_fraction": 0.67,
                    "reset_time": "2026-03-27T18:00:00Z",
                }
            ],
        }
        if str(workdir) == str(project_dir)
        else None,
    )
    monkeypatch.setattr(
        service,
        "_read_grok_direct_usage",
        lambda: {
            "window": "weekly",
            "used_percent": 20.0,
            "resets_at": "July 11, 17:03 PT",
        },
    )

    text = await service.describe_for_sessions(
        [_session("codex", project_dir)],
        available_clis=("claude", "codex", "gemini", "grok", "qwen"),
    )

    assert "🟢 codex" in text
    assert "⚫️ claude · gemini · grok · qwen" in text
    assert "🤖 claude" in text
    assert "5ч" in text
    assert "96%" in text
    assert "📦 codex" in text
    assert "📊 210 total" in text
    assert "♊ gemini" in text
    assert "gemini-2.5-pro" in text
    assert "67%" in text
    assert "✕ grok" in text
    assert "80% осталось" in text
    assert "session file не найден" not in text
    assert "🔮 qwen" in text
    assert "квоты недоступны (non-interactive)" in text


@pytest.mark.asyncio
async def test_describe_for_sessions_prefers_single_project_context_for_gemini(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "repo-primary"
    project_dir.mkdir()
    other_dir = tmp_path / "repo-secondary"
    other_dir.mkdir()
    service = CliLimitsService(
        codex_sessions_roots=[tmp_path / "codex" / "sessions"],
        claude_projects_roots=[tmp_path / "claude" / "projects"],
    )
    monkeypatch.setattr(
        service,
        "_read_gemini_usage_for_workdir",
        lambda workdir: (
            {
                "project_id": "proj-primary",
                "models": [
                    {
                        "model_id": "gemini-2.5-pro",
                        "remaining_fraction": 0.67,
                        "reset_time": "2026-03-27T18:00:00Z",
                    }
                ],
            }
            if str(workdir) == str(project_dir)
            else {
                "project_id": "proj-secondary",
                "models": [{"model_id": "gemini-2.5-pro", "remaining_fraction": 0.12}],
            }
        ),
    )

    text = await service.describe_for_sessions(
        [
            _session("codex", other_dir),
            _session("claude", project_dir),
        ],
        available_clis=("gemini",),
        preferred_workdir=str(project_dir),
    )

    assert "gemini-2.5-pro" in text
    assert "67%" in text
    assert "12%" not in text
