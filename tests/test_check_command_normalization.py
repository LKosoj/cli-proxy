"""Интеграционные тесты check_command() на реальном blocked-patterns.json после
подключения нормализации через agent.tooling.command_scan.scannable_command.

Группа A: обходы политики через кавычки/ANSI-C/обёртки, которые должны ловиться.
Группа B: легитимные команды — регресс-тест, что нормализация не плодит ложные срабатывания.
Группа C: граничные случаи (пустая строка, лимиты длины/глубины/бюджета, битые паттерны и т.п.).

ВАЖНО (см. отчёт агента): часть кейсов из группы A ловится уже на СЫРОЙ строке, без всякой
нормализации — потому что regex ищет подстроку независимо от shell-кавычек (кавычки не прячут
текст от substring-поиска). Настоящими "новыми" обходами, которые раньше проходили мимо
проверки и стали ловиться только благодаря нормализации, являются случаи, где сама
последовательность символов ключевого слова разбита (кавычками) или закодирована (ANSI-C hex).
Это отмечено в комментариях к соответствующим тестам.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from agent.tooling import helpers


@pytest.fixture(autouse=True)
def _reset_pattern_cache():
    helpers._COMPILED_PATTERNS_CACHE = None
    yield
    helpers._COMPILED_PATTERNS_CACHE = None


# ==== A. Обходы, которые должны ловиться после патча ====

@pytest.mark.parametrize(
    "command,expected_reason_substring",
    [
        # Разбитые кавычками "sudo" — раньше \bsudo\b не матчился на сырой строке вообще,
        # т.к. буквы "sudo" не шли подряд. Настоящий новый улов.
        ('su""do id', "sudo"),
        ("su''do id", "sudo"),
        (r"$'\x73\x75\x64\x6f' id", "sudo"),
        # bash -c/sh -c/eval/sudo bash -c — "sudo"/"printenv"/"export -p" уже присутствуют
        # как подстрока сырой команды (кавычки их не прячут от substring-поиска), поэтому
        # ловились бы даже без нормализации; тест — регресс на то, что патч их не сломал.
        ("bash -c 'sudo id'", "sudo"),
        ('sh -c "printenv"', "environment"),
        ("eval 'export -p'", "exported"),
        # первым по списку паттернов срабатывает env-direct-2 ("printenv"), не "sudo" —
        # first-match-wins по порядку в blocked-patterns.json, а не по позиции в тексте.
        ("sudo bash -c 'printenv'", "environment"),
        ('echo "$(printenv)"', "environment"),
        ("echo `printenv`", "environment"),
        ("env DEBUG=1 printenv", "environment"),
        ('git commit -m "$(cat ~/.ssh/id_rsa)"', "SSH private key"),
    ],
)
def test_group_a_obfuscated_commands_are_gated(command, expected_reason_substring):
    dangerous, blocked, reason = helpers.check_command(command, "private")
    assert dangerous or blocked, f"expected gate for: {command}"
    assert reason is not None
    assert expected_reason_substring.lower() in reason.lower(), (command, reason)


def test_group_a_matched_pattern_ids_for_new_bypasses():
    # Явно фиксируем, каким паттерном ловится каждый из "настоящих новых" обходов.
    patterns = {p.id: p for p in helpers._compile_blocked_patterns()}
    sudo_regex = patterns["sudo"].regex
    for command in ('su""do id', "su''do id", r"$'\x73\x75\x64\x6f' id"):
        normalized = __import__("agent.tooling.command_scan", fromlist=["scannable_command"]).scannable_command(
            command.strip()
        ).lower()
        assert sudo_regex.search(normalized), (command, normalized)


# ==== B. Легитимные команды — регресс ====

@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix stuff"',
        'git commit -m "drop table users"',
        "npm run build -- --mode=production",
        "pip install -r requirements.txt",
        'pytest tests/ -k "not slow" -v',
        "docker compose up -d --build",
        "echo 'rm -rf /'",
        'sed -i "s/foo/bar/" file.txt',
        'find . -name "*.py" -newer file.txt',
        "curl -s https://api.github.com/repos/x/y",
    ],
)
def test_group_b_legitimate_commands_are_not_gated(command):
    dangerous, blocked, reason = helpers.check_command(command, "private")
    assert (dangerous, blocked, reason) == (False, False, None), command


def test_group_b_docker_run_with_command_substitution_is_gated_pre_existing():
    # "docker run --rm -v $(pwd):/app myimage" СОДЕРЖИТ буквальную подстроку "$(pwd)" уже в
    # сырой команде — паттерн bypass-dollar-paren (`\$\([^)]+\)`) матчит её независимо от
    # нормализации. Это пре-существующее поведение blocked-patterns.json (любой $(...)
    # требует approval), а не ложное срабатывание, вызванное этим патчем. Проверено сравнением
    # с поведением на сырой строке напрямую (см. отчёт агента).
    dangerous, blocked, reason = helpers.check_command("docker run --rm -v $(pwd):/app myimage", "private")
    assert dangerous is True
    assert blocked is False
    assert reason == "BLOCKED: $() command substitution"
    # и подтверждаем, что дело именно в сырой строке, а не в нормализации:
    assert re.search(r"\$\([^)]+\)", "docker run --rm -v $(pwd):/app myimage")


# ==== C. Граничные случаи ====

def test_empty_command_is_not_gated():
    assert helpers.check_command("", "private") == (False, False, None)


def test_very_long_command_does_not_crash(logger_records):
    records = logger_records("agent.tooling.command_scan")
    big = "echo " + ("a" * 25_000)
    result = helpers.check_command(big, "private")
    assert result == (False, False, None)
    assert any("command_scan" in r.getMessage() for r in records)


def test_deeply_nested_bash_c_beyond_budget_does_not_crash_or_hang():
    def wrap(inner: str) -> str:
        escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
        return 'bash -c "' + escaped + '"'

    cmd = "echo hi"
    for _ in range(12):
        cmd = wrap(cmd)
    start = time.time()
    result = helpers.check_command(cmd, "private")
    elapsed = time.time() - start
    assert elapsed < 0.5
    assert isinstance(result, tuple) and len(result) == 3


def test_unterminated_quote_does_not_crash():
    result = helpers.check_command("echo 'unterminated", "private")
    assert result == (False, False, None)


def test_broken_heredoc_without_closing_marker_does_not_crash():
    result = helpers.check_command("cat <<EOF\nsome text without a closing marker", "private")
    assert result == (False, False, None)


def test_broken_pattern_in_json_is_skipped_and_logged_with_id(tmp_path, monkeypatch, logger_records):
    import json

    broken_path = tmp_path / "blocked-patterns.json"
    broken_path.write_text(
        json.dumps(
            {
                "patterns": [
                    {"id": "broken-test-pattern", "category": "test", "pattern": "(", "reason": "broken"},
                    {"id": "sudo", "category": "privilege", "pattern": r"\bsudo\b", "reason": "BLOCKED: sudo"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(helpers, "BLOCKED_PATTERNS_PATH", str(broken_path))
    helpers._COMPILED_PATTERNS_CACHE = None

    records = logger_records("agent.tooling.helpers", logging.ERROR)
    result = helpers.check_command("sudo id", "private")

    assert result == (True, False, "BLOCKED: sudo")
    assert any("broken-test-pattern" in r.getMessage() for r in records)


def test_invalid_ansi_c_codepoint_does_not_crash():
    result = helpers.check_command(r"echo $'\UFFFFFFFF'", "private")
    assert result == (False, False, None)


def test_cyrillic_single_word_and_multi_word_both_collapse_due_to_ascii_flag():
    single = helpers.check_command("echo 'привет'", "private")
    multi = helpers.check_command("echo 'привет мир'", "private")
    assert single == (False, False, None)
    assert multi == (False, False, None)


def test_wide_command_substitution_bomb_is_bounded_by_budget():
    def make_nested(n: int, leaf: str) -> str:
        cmd = leaf
        for _ in range(n):
            cmd = f"$({cmd})"
        return cmd

    parts = [make_nested(50, f"echo x{i}") for i in range(50)]
    cmd = " ".join(parts)

    start = time.time()
    helpers.check_command(cmd, "private")
    elapsed = time.time() - start
    assert elapsed < 0.5


def test_group_only_category_gates_only_in_group_chats(monkeypatch):
    # В текущем blocked-patterns.json паттернов категории group_only нет (0 штук), поэтому
    # это синтетический мок кэша скомпилированных паттернов, а не реальный файл.
    synthetic = [
        helpers._CompiledPattern(
            id="grp-test",
            category="group_only",
            regex=re.compile(r"\bkick\b", re.I),
            reason="group only test",
            blocked=False,
        ),
    ]
    monkeypatch.setattr(helpers, "_COMPILED_PATTERNS_CACHE", synthetic)

    assert helpers.check_command("kick user", "group") == (True, False, "group only test")
    assert helpers.check_command("kick user", "private") == (False, False, None)


def test_all_curated_patterns_compile_without_being_dropped():
    """Длинный или битый паттерн выпадает из проверки тихо — только строкой в логе.

    Тест фиксирует, что ни один паттерн из curated-файла не потерян: иначе дыра в политике
    появится незаметно для CI, а blocked-patterns.json — единственный источник правил.
    """
    helpers._COMPILED_PATTERNS_CACHE = None
    try:
        raw = [p for p in helpers._load_blocked_patterns() if p.get("pattern")]
        compiled = helpers._compile_blocked_patterns()
        assert len(compiled) == len(raw)
        assert {p.id for p in compiled} == {str(p.get("id") or "") for p in raw}
    finally:
        helpers._COMPILED_PATTERNS_CACHE = None


def test_broken_pattern_does_not_disable_remaining_checks(monkeypatch, logger_records):
    """Сбой одного паттерна при матчинге не должен снимать проверку остальными."""
    class _Exploding:
        def search(self, _text):
            raise RuntimeError("boom")

    good = re.compile(r"\bsudo\b", re.I)
    synthetic = [
        helpers._CompiledPattern(id="broken", category="", regex=_Exploding(), reason="broken", blocked=False),
        helpers._CompiledPattern(id="good", category="", regex=good, reason="sudo blocked", blocked=False),
    ]
    monkeypatch.setattr(helpers, "_COMPILED_PATTERNS_CACHE", synthetic)

    records = logger_records("agent.tooling.helpers", logging.ERROR)
    assert helpers.check_command("sudo id", None) == (True, False, "sudo blocked")
    assert any("broken" in r.getMessage() for r in records)
