"""Юнит-тесты нормализации shell-команд (agent.tooling.command_scan).

Кейсы адаптированы из /tmp/qm_ref/test/command-policy.test.ts (TypeScript-референс),
из которого портирован алгоритм scannableCommand(). Часть TS-тестов проверяет
CommandPolicy/evaluateCommand — этой абстракции в проекте нет, поэтому здесь тестируется
только scannable_command() и её внутренние помощники напрямую.
"""

from __future__ import annotations

import time

import pytest

from agent.tooling.command_scan import (
    MAX_EXPANSION_NODES,
    MAX_RECURSION_DEPTH,
    MAX_SCAN_INPUT_CHARS,
    MAX_SCAN_OUTPUT_CHARS,
    _char_at,
    _command_start,
    _decode_ansi_c,
    _env_split_words,
    _find_command_substitution,
    _here_string_shell_payloads,
    _literal_producer_payload,
    _option_command,
    _piped_shell_payloads,
    _scan_shell,
    _segment_consumes_shell_stdin,
    _segment_shell_payloads,
    _simple_variable_payloads,
    _strip_written_heredocs,
    _unquote_bare_word,
    scannable_command,
)


# ==== _char_at ====

def test_char_at_in_range():
    assert _char_at("abc", 0) == "a"
    assert _char_at("abc", 2) == "c"


def test_char_at_out_of_range_returns_empty():
    assert _char_at("abc", 3) == ""
    assert _char_at("abc", -1) == ""
    assert _char_at("", 0) == ""


# ==== _decode_ansi_c ====

def test_decode_ansi_c_hex():
    assert _decode_ansi_c(r"\x73\x75\x64\x6f") == "sudo"


def test_decode_ansi_c_unicode_short_and_long():
    assert _decode_ansi_c(r"A") == "A"
    assert _decode_ansi_c(r"\U00000041") == "A"


def test_decode_ansi_c_octal():
    assert _decode_ansi_c(r"\162\155") == "rm"


def test_decode_ansi_c_named_escapes():
    assert _decode_ansi_c(r"a\nb") == "a\nb"
    assert _decode_ansi_c(r"a\tb") == "a\tb"


def test_decode_ansi_c_invalid_codepoint_does_not_raise():
    # \UFFFFFFFF выходит за пределы 0x10FFFF: не должно кидать исключение,
    # экранирующая последовательность остаётся как есть (см. docstring модуля, пункт 2).
    result = _decode_ansi_c(r"\UFFFFFFFF")
    assert result == r"\UFFFFFFFF"


def test_decode_ansi_c_mixed():
    assert _decode_ansi_c(r"acme\x63li") == "acmecli"


# ==== _unquote_bare_word ====

def test_unquote_bare_word_accepts_bare_word():
    assert _unquote_bare_word("tool") == "tool"
    assert _unquote_bare_word("query_database") == "query_database"
    assert _unquote_bare_word("") == ""


def test_unquote_bare_word_rejects_multi_word():
    assert _unquote_bare_word("fix stuff") is None
    assert _unquote_bare_word("a;b") is None


def test_unquote_bare_word_ascii_only_rejects_cyrillic():
    # re.ASCII критичен: в JS \w тоже всегда ASCII-only, без него кириллица
    # ошибочно считалась бы "голым словом" и восстанавливалась бы дословно.
    assert _unquote_bare_word("привет") is None


# ==== _strip_written_heredocs ====

def test_strip_written_heredocs_removes_file_write_body():
    cmd = "\n".join(["cat > x.ts <<EOF", 'const c = "danger";', "EOF"])
    stripped = _strip_written_heredocs(cmd)
    assert stripped == ""


def test_strip_written_heredocs_keeps_shell_fed_heredoc():
    cmd = "bash <<EOF\nrm -rf /\nEOF"
    stripped = _strip_written_heredocs(cmd)
    assert "rm -rf /" in stripped


def test_strip_written_heredocs_unterminated_is_noop():
    cmd = "cat <<EOF\nsome text without closing marker"
    stripped = _strip_written_heredocs(cmd)
    assert stripped == cmd


@pytest.mark.parametrize(
    "wrapper",
    ["", "timeout 30 ", "nice ", "time ", "stdbuf -o0 ", "command ", "exec ", "sudo ", "env A=1 ", "nohup "],
)
def test_strip_written_heredocs_keeps_body_behind_wrapper(wrapper):
    """Обёртка перед ``bash`` не должна выдавать исполняемый heredoc за запись в файл.

    Отдельная regex-эвристика искала ``bash`` только в начале сегмента, поэтому
    ``timeout 30 bash > out.log <<EOF`` считался записью в файл и тело вырезалось —
    вместе со спрятанным в нём ``su""do``, который после этого уже некому было раскрыть.
    """
    cmd = f'{wrapper}bash > /tmp/out.log <<EOF\nsu""do id\nEOF'
    assert "sudo" in scannable_command(cmd)


@pytest.mark.parametrize(
    "command",
    [
        'echo "a\\\n b" ; su""do rm -rf /',
        'docker build --label note="release \\\nnotes here" -t img .\nsu""do rm -rf /data',
        "echo $'a\\\nb' ; su\"\"do rm -rf /",
    ],
)
def test_quoted_span_with_line_continuation_does_not_swallow_next_command(command):
    """Без ``re.DOTALL`` кавычка с продолжением строки спаривалась со следующей в тексте.

    ``\\\\.`` не покрывает перенос строки, поэтому ``"a\\<LF>b"`` не распознавался целиком:
    его закрывающая кавычка бралась как открывающая, и весь текст до следующей кавычки
    схлопывался в ``""`` — вместе с ``su`` из ``su""do``, после чего опасная команда
    не находилась ни в нормализованной строке, ни в сырой.
    """
    assert "sudo rm -rf" in scannable_command(command)


def test_heredoc_marker_inside_quotes_is_not_an_opening():
    """``<<EOF`` внутри строкового литерала — не открытие heredoc.

    Иначе любая команда, которая просто ПЕЧАТАЕТ пример heredoc-синтаксиса в файл,
    становилась обёрткой: всё до ближайшей строки-``EOF`` вырезалось из скана, включая
    настоящие команды между ними.
    """
    command = 'echo "trigger <<EOF marker" > /tmp/x\nsu""do id\nEOF'
    assert "sudo id" in scannable_command(command)


def test_heredoc_marker_inside_multiline_quotes_is_not_an_opening():
    """Состояние кавычек тянется через переводы строк, а не считается заново на каждой."""
    command = 'echo "hello\n<<EOF\nworld" > /tmp/x\nsu""do id\nEOF'
    assert "sudo id" in scannable_command(command)


def test_here_string_is_not_mistaken_for_heredoc_opening():
    """``bash <<<EOF`` — here-string, а не открытие heredoc с маркером ``EOF``.

    Жадный ``[^\\n]*`` находил ``<<`` внутри ``<<<``, следующая строка объявлялась телом
    heredoc «в файл» и вырезалась целиком — а bash исполняет её как отдельную команду.
    """
    command = 'bash <<<EOF > /tmp/out\nsu""do rm -rf /tmp/x\nEOF'
    assert "sudo rm -rf" in scannable_command(command)


@pytest.mark.parametrize(
    "flag",
    ["", "-norc ", "-restricted ", "-protected ", "-posix ", "-noprofile ",
     "-rcfile /x ", "-init-file /x ", "-i ", "-x ", "-s "],
)
def test_shell_single_dash_long_options_still_read_stdin(flag):
    """bash принимает длинные опции и с одним дефисом — ``-norc`` это не флаг ``-c``.

    ``^-[^-]*c`` видел в ``-norc``/``-restricted``/``-protected``/``-rcfile`` флаг ``-c``,
    и heredoc/пайп/here-string считались не читающими stdin: тело не сканировалось вовсе,
    хотя реальный bash его исполняет.
    """
    assert "sudo" in scannable_command(f'bash {flag}> /tmp/out.log <<EOF\nsu""do id\nEOF')
    assert "sudo" in scannable_command(f'printf \'su""do id\' | bash {flag}'.strip())
    assert "sudo" in scannable_command(f'bash {flag}<<< \'su""do id\'')


@pytest.mark.parametrize("flag", ["-c 'true' ", "-xc 'true' ", "-ic 'true' "])
def test_shell_dash_c_still_means_stdin_is_not_a_script(flag):
    """При ``-c`` скрипт приходит аргументом, а heredoc действительно уходит в файл."""
    assert "sudo" not in scannable_command(f'bash {flag}> /tmp/out.log <<EOF\nsu""do id\nEOF')


@pytest.mark.parametrize("redirect", ["<>", "3<>", ">|", ">&", "&>", "&>>", ">>", "<&", "<<<", ">", "<"])
def test_strip_written_heredocs_survives_every_redirection_form(redirect):
    """Оператор с целью отдельным словом должен съедаться целиком вместе с целью.

    Порядок альтернатив в ``_REDIRECTION_RE`` важен: ``re.match`` не бэктрекает к более
    длинной альтернативе, поэтому при неверном порядке ``<>`` распознавался как ``<``,
    цель утекала отдельным словом и ``bash`` переставал считаться читающим stdin.
    """
    cmd = f'bash {redirect} /tmp/out.log <<EOF\nsu""do id\nEOF'
    assert "sudo" in scannable_command(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        'cat > /tmp/f.txt <<EOF\nsu""do id\nEOF',
        'cat >> /tmp/f.txt <<EOF\nsu""do id\nEOF',
        'tee /tmp/f.txt > /dev/null <<EOF\nsu""do id\nEOF',
        'bash -c "true" > /tmp/f.txt <<EOF\nsu""do id\nEOF',
    ],
)
def test_strip_written_heredocs_still_drops_file_writes(cmd):
    """Тело, которое реально уходит в файл, остаётся вырезанным — иначе это ложное срабатывание."""
    assert "sudo" not in scannable_command(cmd)


# ==== _find_command_substitution ====

def test_find_command_substitution_simple():
    text = "echo $(pwd) tail"
    result = _find_command_substitution(text, text.index("$("))
    assert result is not None
    body, end = result
    assert body == "pwd"
    assert text[end:] == " tail"


def test_find_command_substitution_nested():
    text = "echo $(echo $(pwd))"
    result = _find_command_substitution(text, text.index("$("))
    assert result is not None
    body, _end = result
    assert body == "echo $(pwd)"


def test_find_command_substitution_unterminated_returns_none():
    text = "echo $(pwd"
    assert _find_command_substitution(text, text.index("$(")) is None


# ==== _scan_shell ====

def test_scan_shell_splits_words_and_commands():
    scan = _scan_shell("echo a b; echo c")
    assert scan.commands == [["echo", "a", "b"], ["echo", "c"]]


def test_scan_shell_collects_nested_command_substitution():
    scan = _scan_shell('echo "$(printenv)"')
    assert scan.nested == ["printenv"]


def test_scan_shell_collects_nested_backtick():
    scan = _scan_shell("echo `printenv`")
    assert scan.nested == ["printenv"]


def test_scan_shell_decodes_ansi_c_word():
    scan = _scan_shell(r"$'\x73\x75\x64\x6f' id")
    assert scan.commands == [["sudo", "id"]]


# ==== _command_start / _option_command ====

def test_command_start_skips_var_assignments_and_keywords():
    words = ["FOO=1", "if", "acmecli", "login"]
    assert _command_start(words) == 2


def test_command_start_no_prefix():
    assert _command_start(["echo", "hi"]) == 0


def test_option_command_skips_value_options():
    words = ["-u", "root", "acmecli", "login"]
    idx = _option_command(words, 0, {"-u"})
    assert words[idx:] == ["acmecli", "login"]


def test_option_command_stops_at_double_dash():
    words = ["--", "acmecli", "login"]
    idx = _option_command(words, 0, set())
    assert words[idx:] == ["acmecli", "login"]


# ==== _segment_shell_payloads (bash -c / eval / env / sudo dispatch) ====

def test_segment_shell_payloads_bash_c():
    words = _scan_shell("bash -c 'sudo id'").commands[0]
    assert _segment_shell_payloads(words) == ["sudo id"]


def test_segment_shell_payloads_eval():
    words = _scan_shell("eval 'export -p'").commands[0]
    assert _segment_shell_payloads(words) == ["export -p"]


def test_segment_shell_payloads_sudo_bash_c():
    words = _scan_shell("sudo bash -c 'printenv'").commands[0]
    assert _segment_shell_payloads(words) == ["printenv"]


def test_segment_shell_payloads_env_assignment_then_command():
    words = _scan_shell("env DEBUG=1 printenv").commands[0]
    assert _segment_shell_payloads(words) == []
    # env без -S/--split-string не производит payload через segment_shell_payloads
    # (printenv здесь не обёртка шелла) — это ожидаемо, printenv не входит в диспетчер.


def test_segment_shell_payloads_non_wrapper_returns_empty():
    words = _scan_shell("echo hi").commands[0]
    assert _segment_shell_payloads(words) == []


# ==== _env_split_words ====

def test_env_split_words_dash_s():
    words = _scan_shell("env -S 'acmecli login'").commands[0]
    args = words[1:]  # без "env"
    split = _env_split_words(args)
    assert split == ["acmecli", "login"]


def test_env_split_words_no_flag_returns_none():
    args = _scan_shell("DEBUG=1 printenv").commands[0]
    assert _env_split_words(args) is None


# ==== Layer 3: pipes / here-strings / simple variables ====

def test_segment_consumes_shell_stdin_bash_alone():
    words = _scan_shell("bash").commands[0]
    assert _segment_consumes_shell_stdin(words) is True


def test_segment_consumes_shell_stdin_bash_c_is_false():
    words = _scan_shell("bash -c 'echo hi'").commands[0]
    assert _segment_consumes_shell_stdin(words) is False


def test_literal_producer_payload_echo():
    words = _scan_shell("echo rm -rf /tmp/x").commands[0]
    assert _literal_producer_payload(words) == "rm -rf /tmp/x"


def test_literal_producer_payload_printf():
    words = _scan_shell("printf '%s' 'rm -rf /tmp/x'").commands[0]
    payload = _literal_producer_payload(words)
    assert payload is not None
    assert "rm -rf /tmp/x" in payload


def test_piped_shell_payloads_printf_bash():
    payloads = _piped_shell_payloads("printf 'rm -rf /tmp/x\\n' | bash")
    assert any("rm -rf /tmp/x" in p for p in payloads)


def test_piped_shell_payloads_not_consuming_stdin_is_empty():
    payloads = _piped_shell_payloads("printf 'rm -rf /tmp/x\\n' | cat")
    assert payloads == []


def test_here_string_shell_payloads():
    payloads = _here_string_shell_payloads("bash <<< 'rm -rf /tmp/x'")
    assert payloads == ["rm -rf /tmp/x"]


def test_simple_variable_payloads_resolves_assignment():
    payloads = _simple_variable_payloads('r=rm; "$r" -rf /tmp/x')
    assert any("rm -rf /tmp/x" in p for p in payloads)


def test_simple_variable_payloads_no_assignment_is_empty():
    payloads = _simple_variable_payloads("echo $r")
    assert payloads == []


# ==== scannable_command: end-to-end, портировано из command-policy.test.ts ====

def test_scannable_command_strips_heredoc_but_keeps_command_substitution():
    heredoc = "\n".join(["cat > x <<EOF", "git push --force", "EOF"])
    assert "git push --force" not in scannable_command(heredoc)

    assert "rm -rf" not in scannable_command("echo 'rm -rf /'")
    assert "drop table" not in scannable_command('git commit -m "drop table users"').lower()

    assert "rm -rf" in scannable_command('echo "$(rm -rf /)"')


def test_scannable_command_unquotes_bare_words():
    assert scannable_command("acmecli 'tool' query_database") == "acmecli tool query_database"
    assert scannable_command('acmecli "tool" query_database') == "acmecli tool query_database"
    assert scannable_command("git commit -m 'fix stuff'") == "git commit -m ''"
    assert scannable_command("echo 'a;b'") == "echo ''"


def test_scannable_command_dangerous_command_survives_alongside_heredoc():
    cmd = "\n".join(["cat > note.txt <<EOF", "harmless body", "EOF", "git push --force origin main"])
    assert "git push --force origin main" in scannable_command(cmd)


def test_scannable_command_heredoc_fed_to_shell_stays_gated():
    assert "rm -rf /" in scannable_command("bash <<EOF\nrm -rf /\nEOF")
    assert "rm -rf /" in scannable_command("cat <<EOF | bash\nrm -rf /\nEOF")
    assert "rm -rf /" not in scannable_command("cat > /tmp/s.sh <<EOF\nrm -rf /\nEOF")


def test_scannable_command_shell_wrappers_cannot_hide_payload():
    for command in (
        "bash -c 'rm -rf /tmp/x'",
        "eval 'git push --force origin main'",
        "sudo bash -lc 'rm -rf /tmp/x'",
        "echo \"$(bash -c 'rm -rf /tmp/x')\"",
    ):
        result = scannable_command(command)
        assert "rm -rf" in result or "--force" in result, command


def test_scannable_command_quoted_payload_inside_bash_c_stays_hidden():
    # bash -c 'echo "rm -rf /tmp/x"' — payload данные внутри двойных кавычек (echo-аргумент),
    # многословные -> схлопываются, поэтому итоговый текст не содержит "rm -rf".
    result = scannable_command("bash -c 'echo \"rm -rf /tmp/x\"'")
    assert "rm -rf" not in result


def test_scannable_command_ansi_c_hex_word_cannot_bypass():
    assert scannable_command(r"$'\x73\x75\x64\x6f' id") == "sudo id"


def test_scannable_command_split_quotes_cannot_bypass():
    assert scannable_command('su""do id') == "sudo id"
    assert scannable_command("su''do id") == "sudo id"


def test_quote_inside_other_quotes_does_not_pair_across_lines():
    # `"` внутри `'...'` — обычный символ. Пока схлопывание шло тремя независимыми
    # regex-проходами, эта кавычка спаривалась с настоящей `"` тремя строками ниже,
    # и весь текст между ними (вместе с `su""do rm -rf`) исчезал из нормализации.
    command = "echo 'a \"b'\nsu\"\"do rm -rf /tmp/x\necho 'c\"'"
    assert "sudo rm -rf /tmp/x" in scannable_command(command)


def test_apostrophe_inside_double_quotes_does_not_pair_across_lines():
    # Симметричный случай: `'` внутри `"..."`.
    command = "echo \"it's here\"\nsu''do rm -rf /tmp/x\necho \"and it's gone\""
    assert "sudo rm -rf /tmp/x" in scannable_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo 'unclosed",
        'echo "unclosed',
        "echo $'unclosed",
    ],
)
def test_unclosed_quote_does_not_crash(command):
    assert isinstance(scannable_command(command), str)


def test_scannable_command_piped_and_here_string_payloads_stay_gated():
    assert "rm -rf /tmp/x" in scannable_command("printf 'rm -rf /tmp/x\\n' | bash")
    assert "rm -rf /tmp/x" in scannable_command("bash <<< 'rm -rf /tmp/x'")
    assert "rm -rf /tmp/x" in scannable_command('r=rm; "$r" -rf /tmp/x')


def test_scannable_command_pipe_to_non_shell_not_expanded():
    result = scannable_command("printf 'rm -rf /tmp/x\\n' | cat")
    assert "rm -rf /tmp/x" not in result


def test_scannable_command_conditional_pipe_not_expanded():
    # `||`/`&&` — это не пайп, bash справа не читает stdin от printf.
    assert "rm -rf /tmp/x" not in scannable_command("printf 'rm -rf /tmp/x\\n' || bash")
    assert "rm -rf /tmp/x" not in scannable_command("printf 'rm -rf /tmp/x\\n' && bash")


# ==== Границы: длина входа, глубина рекурсии, бюджет разворачиваний ====

def test_scannable_command_empty_string():
    assert scannable_command("") == ""


def test_scannable_command_over_length_limit_returns_raw(logger_records):
    records = logger_records("agent.tooling.command_scan")
    big = "echo " + ("a" * (MAX_SCAN_INPUT_CHARS + 5))
    result = scannable_command(big)
    assert result == big
    assert any("command_scan" in r.getMessage() for r in records)


def _wrap_bash_c_dq(inner: str) -> str:
    escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
    return 'bash -c "' + escaped + '"'


def test_scannable_command_recursion_reaches_payload_within_depth_budget():
    # Ровно MAX_RECURSION_DEPTH обёрток bash -c: полезная нагрузка ещё достижима.
    cmd = "cat ~/.ssh/id_rsa"
    for _ in range(MAX_RECURSION_DEPTH):
        cmd = _wrap_bash_c_dq(cmd)
    assert "id_rsa" in scannable_command(cmd)


def test_scannable_command_recursion_depth_capped_and_fast():
    # Больше MAX_RECURSION_DEPTH обёрток: полезная нагрузка на такой глубине уже не
    # разворачивается (fail-safe отсечение по глубине), но сканирование не падает и не виснет.
    cmd = "cat ~/.ssh/id_rsa"
    for _ in range(MAX_RECURSION_DEPTH + 5):
        cmd = _wrap_bash_c_dq(cmd)
    assert len(cmd) < MAX_SCAN_INPUT_CHARS

    start = time.time()
    result = scannable_command(cmd)
    elapsed = time.time() - start
    assert elapsed < 0.5
    assert "id_rsa" not in result


def test_scannable_command_wide_expansion_budget_stops_bomb():
    def make_nested(n: int, leaf: str) -> str:
        cmd = leaf
        for _ in range(n):
            cmd = f"$({cmd})"
        return cmd

    parts = [make_nested(50, f"echo x{i}") for i in range(50)]
    cmd = " ".join(parts)

    start = time.time()
    scannable_command(cmd)
    elapsed = time.time() - start
    assert elapsed < 0.5


def test_max_expansion_nodes_is_reasonable():
    assert MAX_EXPANSION_NODES > 0
    assert MAX_RECURSION_DEPTH > 0
    assert MAX_SCAN_INPUT_CHARS > 0
    assert MAX_SCAN_OUTPUT_CHARS >= MAX_SCAN_INPUT_CHARS


def test_unbalanced_command_substitutions_do_not_take_quadratic_time():
    """Ненайденная ``)`` откатывала скан на один символ, и следующий ``$(`` шёл до конца заново.

    На строке у границы MAX_SCAN_INPUT_CHARS это давало ~20 секунд синхронно в event loop.
    Бюджеты рекурсии и расширения тут не срабатывают: всё происходит внутри одного
    прохода токенайзера на глубине 0.
    """
    command = ('"' + "$(" * 9_990 + 'x"')[:MAX_SCAN_INPUT_CHARS - 1]
    started = time.perf_counter()
    scannable_command(command)
    assert time.perf_counter() - started < 1.0


def test_expanded_output_over_limit_falls_back_to_original():
    """Раскрытие обёрток дописывает payload, поэтому патологический вход раздувает результат.

    Вход в 20k вложенных ``$(`` давал 180k на выходе, и паттерны с несколькими ``.*``
    уходили на нём в backtracking. Раздутый результат отбрасывается — смысла, которого
    не было бы в исходной строке, он не несёт.
    """
    command = ("$(" * 6_666 + ")" * 6_666)[:MAX_SCAN_INPUT_CHARS - 1]
    assert scannable_command(command) == command


def test_long_wrapper_chain_does_not_exhaust_python_stack():
    """Обёртки разбираются циклом: рекурсивный вариант падал RecursionError на этой строке."""
    command = ("env A=1 " * 2_500)[:MAX_SCAN_INPUT_CHARS - 1]
    started = time.perf_counter()
    scannable_command(command)
    assert time.perf_counter() - started < 2.0


def test_long_wrapper_chain_before_here_string_still_reveals_payload():
    """RecursionError в разборе here-string ронял ВЕСЬ скан, и обфускация проходила незамеченной.

    ``_segment_consumes_shell_stdin`` оставался рекурсивным, когда остальные разборщики
    обёрток уже стали циклами. Цепочка ``nice`` в несвязанной части строки роняла
    нормализацию целиком — вместе с уже раскрытым ``su""do`` в её начале.
    """
    command = 'su""do id; ' + "nice -n1 " * 1_050 + "bash <<< 'ls'"
    assert len(command) < MAX_SCAN_INPUT_CHARS
    assert "sudo" in scannable_command(command)


def test_long_wrapper_chain_before_pipe_still_reveals_payload():
    """То же для ``_literal_producer_payload``: он разбирает обёртки потребителя пайпа."""
    command = 'su""do id; printf hi | ' + "nice -n1 " * 1_050 + "bash"
    assert len(command) < MAX_SCAN_INPUT_CHARS
    assert "sudo" in scannable_command(command)


def test_nested_env_split_string_does_not_take_quadratic_time():
    """``env -S`` — единственная обёртка, которая не уменьшает вход: она ре-токенизирует значение.

    Цепочка из 2000 вложенных ``env -S`` на 18 КБ (в лимит длины укладывается) занимала
    6 секунд синхронно в event loop, пока разворачивание не ограничили бюджетом.
    """
    def wrap(inner: str, index: int) -> str:
        quote = "'" if index % 2 == 0 else '"'
        return f"env -S {quote}{inner}{quote}"

    payload = "sh"
    for index in range(2_000):
        payload = wrap(payload, index)
    for command in (f"echo hi | {payload}", f"{payload} <<< 'ls'"):
        assert len(command) < MAX_SCAN_INPUT_CHARS
        started = time.perf_counter()
        scannable_command(command)
        assert time.perf_counter() - started < 1.0


def test_env_split_string_within_budget_still_expands():
    """Бюджет не должен ломать реальные (одноуровневые) ``env -S``."""
    assert "sudo" in scannable_command("""printf 'sudo id' | env -S "sh" """)
    assert "printenv" in scannable_command("""env -S "bash" <<< 'printenv' """)


def test_unclosed_heredoc_markers_do_not_take_quadratic_time():
    """Ленивый ``[\\s\\S]*?`` с обратной ссылкой перебирал тело до конца текста на каждой строке.

    На 20 КБ из незакрытых ``<<EOF`` это давало ~1 секунду синхронно в event loop.
    """
    command = ("x<<EOF\n" * (MAX_SCAN_INPUT_CHARS // 7 + 1))[:MAX_SCAN_INPUT_CHARS - 1]
    started = time.perf_counter()
    scannable_command(command)
    assert time.perf_counter() - started < 0.5
