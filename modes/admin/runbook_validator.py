"""
Валидация script-based runbook'а: синтаксис, checksum, структура steps.

Проверки:
    - структура frontmatter: есть steps, каждый step имеет name/script/checksum/target_hint
    - файл скрипта лежит под scripts_dir(workdir, rb_id) (не симлинк, regular file)
    - реальный SHA1 совпадает с тем, что записано в frontmatter
    - bash -n <file> (синтаксический парсер) — если bash доступен
    - shellcheck если установлен (опционально)

Возвращается структура:
    {
      "ok": bool,
      "checks": [{"step": name, "bash_n": "ok"|"fail: ...", "shellcheck": "...", "checksum": "ok"|"mismatch"}],
      "errors": [strings],
      "warnings": [strings],
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .runbook_builder import scripts_dir
from .runbooks import Runbook, load_runbooks

_log = logging.getLogger(__name__)

VALIDATOR_TIMEOUT_SEC = 10.0


class RunbookValidatorError(RuntimeError):
    pass


@dataclass
class StepCheck:
    step: str
    script: str
    checksum: str = "unknown"
    bash_n: str = "skipped"
    shellcheck: str = "skipped"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "script": self.script,
            "checksum": self.checksum,
            "bash_n": self.bash_n,
            "shellcheck": self.shellcheck,
        }


@dataclass
class ValidationReport:
    ok: bool = True
    rb_id: str = ""
    checks: List[StepCheck] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "rb_id": self.rb_id,
            "checks": [c.to_dict() for c in self.checks],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _run_cmd(argv: List[str], *, timeout: float = VALIDATOR_TIMEOUT_SEC) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _load_runbook(workdir: str, rb_id: str) -> Runbook:
    for rb in load_runbooks(workdir):
        if rb.id == rb_id:
            return rb
    raise RunbookValidatorError(f"runbook not found: {rb_id}")


def _parse_checksum(raw: Any) -> Optional[str]:
    s = str(raw or "").strip()
    if not s:
        return None
    if ":" not in s:
        return s
    algo, digest = s.split(":", 1)
    if algo.lower() != "sha1":
        return None
    return digest.strip() or None


async def validate_runbook(workdir: str, rb_id: str) -> ValidationReport:
    report = ValidationReport(rb_id=rb_id)
    try:
        rb = _load_runbook(workdir, rb_id)
    except RunbookValidatorError as exc:
        report.ok = False
        report.errors.append(str(exc))
        return report

    steps = rb.metadata.get("steps") if isinstance(rb.metadata, Mapping) else None
    if not isinstance(steps, list) or not steps:
        report.ok = False
        report.errors.append("runbook has no steps in frontmatter")
        return report

    sdir = scripts_dir(workdir, rb_id)
    if not sdir.is_dir():
        report.ok = False
        report.errors.append(f"scripts directory missing: {sdir}")
        return report

    bash_available = shutil.which("bash") is not None
    shellcheck_available = shutil.which("shellcheck") is not None
    if not bash_available:
        report.warnings.append("bash not found — syntax checks skipped")
    if not shellcheck_available:
        report.warnings.append("shellcheck not found — static analysis skipped")

    for raw_step in steps:
        if not isinstance(raw_step, Mapping):
            report.ok = False
            report.errors.append(f"step is not a mapping: {raw_step!r}")
            continue
        step_name = str(raw_step.get("name") or "").strip()
        script_name = str(raw_step.get("script") or "").strip()
        checksum_raw = raw_step.get("checksum")
        if not step_name or not script_name:
            report.ok = False
            report.errors.append(f"step has no name/script: {raw_step!r}")
            continue
        check = StepCheck(step=step_name, script=script_name)

        script_path = sdir / script_name
        # Проверка что файл существует и лежит строго внутри sdir (никаких ..).
        try:
            resolved = script_path.resolve(strict=True)
        except FileNotFoundError:
            check.bash_n = "fail: file missing"
            check.checksum = "missing"
            report.errors.append(f"script missing for step {step_name}: {script_path}")
            report.ok = False
            report.checks.append(check)
            continue

        if os.path.islink(script_path):
            check.checksum = "symlink"
            report.errors.append(f"script is symlink (not allowed): {script_path}")
            report.ok = False
            report.checks.append(check)
            continue

        try:
            resolved.relative_to(sdir.resolve())
        except ValueError:
            check.checksum = "out-of-scope"
            report.errors.append(f"script path escapes scripts dir: {resolved}")
            report.ok = False
            report.checks.append(check)
            continue

        expected = _parse_checksum(checksum_raw)
        actual = _sha1_of_file(resolved)
        if expected is None:
            check.checksum = "unspecified"
            report.warnings.append(f"step {step_name}: checksum missing in frontmatter")
        elif expected == actual:
            check.checksum = "ok"
        else:
            check.checksum = f"mismatch (expected {expected[:8]}… got {actual[:8]}…)"
            report.errors.append(
                f"step {step_name}: checksum mismatch for {script_name}",
            )
            report.ok = False

        if bash_available:
            rc, _, stderr = await _run_cmd(["bash", "-n", str(resolved)])
            if rc == 0:
                check.bash_n = "ok"
            else:
                check.bash_n = f"fail: {stderr.strip()[:200]}"
                report.errors.append(f"step {step_name}: bash -n failed")
                report.ok = False

        if shellcheck_available:
            rc, _stdout, stderr = await _run_cmd(["shellcheck", "-S", "error", str(resolved)])
            if rc == 0:
                check.shellcheck = "ok"
            else:
                check.shellcheck = f"warn: {stderr.strip()[:200] or _stdout.strip()[:200]}"
                report.warnings.append(f"step {step_name}: shellcheck reported issues")

        report.checks.append(check)

    return report


__all__ = [
    "RunbookValidatorError",
    "StepCheck",
    "VALIDATOR_TIMEOUT_SEC",
    "ValidationReport",
    "validate_runbook",
]
