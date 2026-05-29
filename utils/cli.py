from __future__ import annotations

import re
from typing import List, Optional, Tuple

from utils.text import strip_ansi


def build_command(
    cmd_template: List[str],
    prompt: str,
    resume: Optional[str] = None,
    image: Optional[str] = None,
) -> Tuple[List[str], bool]:
    replaced = False
    cmd: List[str] = []
    skip_next = False
    skip_continue = resume is not None
    for part in cmd_template:
        if skip_next:
            skip_next = False
            continue
        if skip_continue and part == "--continue":
            continue
        if "{resume}" in part:
            if resume is None:
                continue
            cmd.append(part.replace("{resume}", resume))
            continue
        if "{image}" in part:
            if image is None:
                if part == "{image}":
                    continue
                cmd.append(part.replace("{image}", ""))
                continue
            cmd.append(part.replace("{image}", image))
            continue
        if part == "--resume" and resume is None:
            skip_next = True
            continue
        if "{prompt}" in part:
            cmd.append(part.replace("{prompt}", prompt))
            replaced = True
        else:
            cmd.append(part)
    use_stdin = not replaced
    return cmd, use_stdin


def detect_prompt_regex(lines: List[str]) -> Optional[str]:
    cleaned = [strip_ansi(line).rstrip("\n") for line in lines]
    cleaned = [line for line in cleaned if line.strip()]
    if not cleaned:
        return None
    tail = cleaned[-6:]
    candidate = tail[-1]
    if len(candidate) > 80:
        return None
    occurrences = sum(1 for line in tail if line == candidate)
    if occurrences >= 2:
        return re.escape(candidate) + r"\s*$"
    return None


def detect_resume_regex(text: str) -> Optional[str]:
    cleaned = strip_ansi(text)
    patterns = [
        (r'"thread_id"\s*:\s*"([^"]+)"', r'"thread_id"\s*:\s*"([^"]+)"'),
        (r'"conversation_id"\s*:\s*"([^"]+)"', r'"conversation_id"\s*:\s*"([^"]+)"'),
        (r'"session_id"\s*:\s*"([^"]+)"', r'"session_id"\s*:\s*"([^"]+)"'),
        (r"resume\s*id\s*[:=]\s*([A-Za-z0-9_-]+)", r"resume\s*id\s*[:=]\s*([A-Za-z0-9_-]+)"),
        (r"--resume\s+(?!latest\b)([A-Za-z0-9._:-]+)", r"--resume\s+(?!latest\b)([A-Za-z0-9._:-]+)"),
    ]

    for pattern, regex in patterns:
        if re.search(pattern, cleaned, flags=re.IGNORECASE):
            return regex
    return None


def resolve_env_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    import os

    return os.path.expandvars(value)
