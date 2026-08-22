"""Build compact cross-CLI transfer capsules and durable evidence packs."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .canonical import CanonicalMessage, CanonicalSession, strip_tool_calls

logger = logging.getLogger(__name__)

DEFAULT_CAPSULE_MAX_CHARS = 24_000
MIN_CAPSULE_MAX_CHARS = 4_000
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ACK_TEXT = "Transfer capsule accepted. I will continue from this compact state."


@dataclass(frozen=True)
class TransferPackage:
    """A compact target session plus paths to the full local evidence."""

    canonical: CanonicalSession
    capsule_text: str
    evidence_dir: Optional[str]


def build_transfer_package(
    canonical: CanonicalSession,
    *,
    target_cli: str,
    workspace: str,
    max_chars: int = DEFAULT_CAPSULE_MAX_CHARS,
) -> TransferPackage:
    """Return a compact target session and persist full transcript evidence."""
    limit = max(MIN_CAPSULE_MAX_CHARS, int(max_chars or DEFAULT_CAPSULE_MAX_CHARS))
    ws = str(workspace or canonical.workspace or "").strip()
    target = str(target_cli or "").strip().lower()
    transfer_id = _transfer_id(canonical, target)

    evidence_dir = (
        _persist_evidence(canonical, workspace=ws, target_cli=target, transfer_id=transfer_id)
        if ws
        else None
    )

    capsule_text = _compose_capsule(
        canonical,
        target_cli=target,
        workspace=ws,
        evidence_dir=evidence_dir,
        max_chars=limit,
    )
    if evidence_dir:
        _write_text(Path(evidence_dir) / "capsule.md", capsule_text)

    now = time.time()
    compact = CanonicalSession(
        source_cli=canonical.source_cli,
        session_id=canonical.session_id,
        workspace=ws,
        messages=[
            CanonicalMessage(role="user", content=capsule_text, timestamp=now),
            CanonicalMessage(role="assistant", content=_ACK_TEXT, timestamp=now),
        ],
        summary=capsule_text,
        extracted_at=canonical.extracted_at,
    )
    return TransferPackage(
        canonical=compact,
        capsule_text=capsule_text,
        evidence_dir=evidence_dir,
    )


def _compose_capsule(
    canonical: CanonicalSession,
    *,
    target_cli: str,
    workspace: str,
    evidence_dir: Optional[str],
    max_chars: int,
) -> str:
    messages = list(canonical.messages or [])
    role_counts = _role_counts(messages)
    evidence_display = _display_path(evidence_dir, workspace) if evidence_dir else "not available"

    header = [
        "# Cross-CLI Session Capsule",
        "",
        "You are continuing work that started in another CLI. Treat this as a compact handoff, not a full transcript.",
        "Do not assume missing details. Inspect the evidence files only when you need older context.",
        "",
        "## Metadata",
        f"- source_cli: {canonical.source_cli or 'unknown'}",
        f"- source_session_id: {canonical.session_id or 'unknown'}",
        f"- target_cli: {target_cli or 'unknown'}",
        f"- workspace: {workspace or canonical.workspace or 'unknown'}",
        f"- original_messages: {len(messages)}",
        f"- role_counts: {_json_dumps(role_counts)}",
        f"- evidence_dir: {evidence_display}",
        f"- evidence_manifest: {evidence_display.rstrip('/') + '/manifest.json' if evidence_dir else 'not available'}",
        f"- full_transcript: {evidence_display.rstrip('/') + '/canonical.jsonl' if evidence_dir else 'not available'}",
        "",
    ]
    if canonical.summary:
        header.extend([
            "## Existing Summary",
            _trim_text(canonical.summary, 2_000),
            "",
        ])

    first_user = _first_message(messages, role="user")
    if first_user:
        header.extend([
            "## Original User Goal",
            _trim_text(first_user.content, 2_000),
            "",
        ])

    tail_intro = [
        "## Recent Transcript Excerpt",
        "Only the most recent messages that fit the transfer budget are included here.",
        "",
    ]
    footer = [
        "",
        "## Continuation Instructions",
        "- Verify the workspace state before changing files.",
        "- Use the evidence files for older context instead of relying on this chat to contain everything.",
        "- Continue from the user's latest unresolved request.",
    ]

    fixed = "\n".join(header + tail_intro + footer)
    available = max_chars - len(fixed) - 512
    excerpt, omitted = _recent_excerpt(messages, max_chars=max(0, available))
    excerpt_lines = excerpt.splitlines() if excerpt else ["No transcript messages fit the capsule budget."]
    if omitted:
        excerpt_lines.insert(0, f"[{omitted} older message(s) omitted from the capsule; see evidence files.]")

    text = "\n".join(header + tail_intro + excerpt_lines + footer).strip() + "\n"
    if len(text) <= max_chars:
        return text
    suffix = "\n\n[Capsule hard-truncated to fit the transfer budget. See evidence files for the full transcript.]\n"
    return text[: max_chars - len(suffix)].rstrip() + suffix


def _recent_excerpt(messages: list[CanonicalMessage], *, max_chars: int) -> tuple[str, int]:
    if max_chars <= 0:
        return "", len(messages)
    blocks: list[str] = []
    total = 0
    included = 0
    for idx, msg in reversed(list(enumerate(messages, start=1))):
        if str(msg.role or "").strip().lower() == "tool":
            # Вывод инструментов целиком лежит в evidence-файлах: бюджет капсулы нужен диалогу.
            continue
        text = _trim_text(strip_tool_calls(msg.content), 1_800)
        if not text:
            continue
        block = f"### Message {idx} ({msg.role})\n{text}\n"
        block_len = len(block)
        if blocks and total + block_len > max_chars:
            break
        if not blocks and block_len > max_chars:
            block = block[:max_chars].rstrip() + "\n[Message truncated.]\n"
            block_len = len(block)
        blocks.append(block)
        total += block_len
        included += 1
    blocks.reverse()
    return "\n".join(blocks).strip(), max(0, len(messages) - included)


def _persist_evidence(
    canonical: CanonicalSession,
    *,
    workspace: str,
    target_cli: str,
    transfer_id: str,
) -> Optional[str]:
    base = Path(workspace) / ".cli-proxy" / "session-transfer" / transfer_id
    try:
        base.mkdir(parents=True, exist_ok=True)
        manifest = {
            "source_cli": canonical.source_cli,
            "source_session_id": canonical.session_id,
            "target_cli": target_cli,
            "workspace": workspace,
            "message_count": len(canonical.messages or []),
            "role_counts": _role_counts(list(canonical.messages or [])),
            "created_at": time.time(),
            "files": {
                "canonical_jsonl": "canonical.jsonl",
                "manifest": "manifest.json",
                "capsule": "capsule.md",
            },
        }
        _write_json(base / "manifest.json", manifest)
        with (base / "canonical.jsonl").open("w", encoding="utf-8") as fh:
            for idx, msg in enumerate(canonical.messages or [], start=1):
                record = {
                    "index": idx,
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp,
                }
                fh.write(_json_dumps(record))
                fh.write("\n")
        return str(base)
    except Exception:
        logger.exception("session_transfer: failed to persist evidence pack")
        return None


def _transfer_id(canonical: CanonicalSession, target_cli: str) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    source = _safe_token(canonical.source_cli or "unknown")
    target = _safe_token(target_cli or "unknown")
    sid = _safe_token(canonical.session_id or "session")
    if len(sid) > 48:
        sid = sid[:48].rstrip("._-")
    return f"{ts}-{source}-to-{target}-{sid}"


def _role_counts(messages: list[CanonicalMessage]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for msg in messages:
        role = str(msg.role or "unknown").strip() or "unknown"
        counts[role] = counts.get(role, 0) + 1
    return counts


def _first_message(messages: list[CanonicalMessage], *, role: str) -> Optional[CanonicalMessage]:
    for msg in messages:
        if msg.role == role and str(msg.content or "").strip():
            return msg
    return None


def _trim_text(text: object, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    suffix = "\n[truncated]"
    return value[: max_chars - len(suffix)].rstrip() + suffix


def _display_path(path: Optional[str], workspace: str) -> str:
    if not path:
        return "not available"
    try:
        rel = Path(path).resolve().relative_to(Path(workspace).resolve())
        return str(rel)
    except Exception:
        return str(path)


def _safe_token(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return "unknown"
    return _SAFE_TOKEN_RE.sub("_", token)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(_json_dumps(payload) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except Exception:
        logger.exception("session_transfer: failed to write %s", path)


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
