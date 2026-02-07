import os
from typing import Dict


def _strip_inline_comment_unquoted(value: str) -> str:
    """
    Minimal .env parser behavior:
    - allow inline comments if they start with ' #' (space then #)
    - do not strip '#' when it is part of the value (e.g. 'abc#1') or inside quotes.
    """
    if " #" not in value:
        return value
    # Split on the first occurrence only.
    head, _comment = value.split(" #", 1)
    return head.rstrip()


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        return v[1:-1]
    return v


def parse_dotenv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        # Keep surrounding whitespace in the value (after '=') reasonable.
        value = value.lstrip()

        # Comment stripping only for unquoted values.
        if not (value.startswith('"') or value.startswith("'")):
            value = _strip_inline_comment_unquoted(value)

        out[key] = _unquote(value)
    return out


def load_dotenv(path: str, override: bool = False) -> Dict[str, str]:
    """
    Loads KEY=VALUE pairs from a .env file into os.environ.

    - override=False: do not overwrite existing environment variables
    Returns dict of variables that were set (or would be set).
    """
    if not path:
        return {}
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return {}

    parsed = parse_dotenv(content)
    applied: Dict[str, str] = {}
    for k, v in parsed.items():
        if not override and k in os.environ:
            continue
        os.environ[k] = v
        applied[k] = v
    return applied


def load_dotenv_near(path: str, filename: str = ".env", override: bool = False) -> Dict[str, str]:
    """
    Convenience: load dotenv from the directory of a given file path.
    """
    if not path:
        return {}
    base_dir = os.path.dirname(os.path.abspath(path))
    return load_dotenv(os.path.join(base_dir, filename), override=override)
