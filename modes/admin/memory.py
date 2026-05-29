from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

import yaml

from .snapshot_store import server_dir

_log = logging.getLogger(__name__)

DEFAULT_COMPACT_THRESHOLD = 500
DEFAULT_COMPACT_KEEP_TAIL = 50

NOTES_FILE = "notes.md"
FACTS_FILE = "facts.yaml"

_NOTE_HEADER_RE = re.compile(
    r"^##\s+(?P<ts>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2})?Z?)"
    r"\s*(?:\[(?P<source>[^\]]+)\])?"
    r"(?P<tags>(?:\s+#\S+)*)\s*$"
)


class ServerMemoryError(RuntimeError):
    """Raised when server memory operation fails."""


@dataclass
class NoteEntry:
    ts: str
    source: str
    text: str


def memory_dir(workdir: str, server_id: str) -> Path:
    return server_dir(workdir, server_id) / "memory"


def facts_path(workdir: str, server_id: str) -> Path:
    return memory_dir(workdir, server_id) / FACTS_FILE


def notes_path(workdir: str, server_id: str) -> Path:
    return memory_dir(workdir, server_id) / NOTES_FILE


class ServerMemory:
    """
    Memory-store для конкретного сервера:
      - facts.yaml — структурированные key/value факты
      - notes.md   — append-only журнал (периодически схлопывается)
    """

    def __init__(self, workdir: str, server_id: str) -> None:
        self.workdir = str(workdir or "")
        self.server_id = str(server_id or "")
        self._dir = memory_dir(self.workdir, self.server_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._facts_path = self._dir / FACTS_FILE
        self._notes_path = self._dir / NOTES_FILE

    @property
    def facts_file(self) -> Path:
        return self._facts_path

    @property
    def notes_file(self) -> Path:
        return self._notes_path

    # --- facts ---

    def get_facts(self) -> Dict[str, Any]:
        if not self._facts_path.is_file():
            return {}
        try:
            data = yaml.safe_load(self._facts_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ServerMemoryError(f"invalid YAML in {self._facts_path}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ServerMemoryError(f"facts file {self._facts_path} is not a mapping")
        return dict(data)

    def update_fact(self, key: str, value: Any, *, by: Optional[str] = None) -> Dict[str, Any]:
        clean_key = self._normalize_key(key)
        facts = self.get_facts()
        previous = facts.get(clean_key)
        facts[clean_key] = value
        meta = facts.setdefault("_meta", {})
        if isinstance(meta, Mapping):
            meta = dict(meta)
        else:
            meta = {}
        meta["updated_at"] = _now_iso()
        if by:
            meta.setdefault("updated_by", {})
            if isinstance(meta["updated_by"], Mapping):
                updated_by = dict(meta["updated_by"])
            else:
                updated_by = {}
            updated_by[clean_key] = str(by)
            meta["updated_by"] = updated_by
        facts["_meta"] = meta
        self._write_facts(facts)
        return {"key": clean_key, "prev": previous, "value": value}

    def delete_fact(self, key: str) -> bool:
        clean_key = self._normalize_key(key)
        facts = self.get_facts()
        if clean_key not in facts:
            return False
        facts.pop(clean_key, None)
        meta = facts.get("_meta") if isinstance(facts.get("_meta"), Mapping) else {}
        updated_by = dict(meta.get("updated_by") or {}) if isinstance(meta, Mapping) else {}
        updated_by.pop(clean_key, None)
        if isinstance(meta, Mapping):
            new_meta = dict(meta)
            new_meta["updated_by"] = updated_by
            new_meta["updated_at"] = _now_iso()
            facts["_meta"] = new_meta
        self._write_facts(facts)
        return True

    def _write_facts(self, facts: Mapping[str, Any]) -> None:
        _atomic_dump_yaml(self._facts_path, dict(facts))

    # --- notes ---

    def get_notes(self) -> str:
        if not self._notes_path.is_file():
            return ""
        return self._notes_path.read_text(encoding="utf-8")

    def append_note(
        self,
        text: str,
        *,
        source: str = "manual",
        tags: Optional[List[str]] = None,
        ts: Optional[str] = None,
    ) -> NoteEntry:
        body = (text or "").strip()
        if not body:
            raise ServerMemoryError("note text is empty")
        src = str(source or "manual").strip() or "manual"
        ts_value = str(ts or _now_iso())
        tag_suffix = ""
        if tags:
            clean = [str(t).strip() for t in tags if str(t or "").strip()]
            if clean:
                tag_suffix = " " + " ".join(f"#{t}" for t in clean)
        header = f"## {ts_value} [{src}]{tag_suffix}".rstrip()
        block = f"{header}\n{body}\n\n"
        existing = self.get_notes()
        self._notes_path.write_text(existing + block, encoding="utf-8")
        return NoteEntry(ts=ts_value, source=src, text=body)

    def iter_note_entries(self) -> Iterator[NoteEntry]:
        raw = self.get_notes()
        if not raw:
            return
        current_header: Optional[tuple[str, str]] = None
        buffer: List[str] = []

        def _flush() -> Optional[NoteEntry]:
            if current_header is None:
                return None
            text = "\n".join(line for line in buffer).strip()
            if not text:
                return None
            return NoteEntry(ts=current_header[0], source=current_header[1], text=text)

        for raw_line in raw.splitlines():
            m = _NOTE_HEADER_RE.match(raw_line)
            if m:
                flushed = _flush()
                if flushed is not None:
                    yield flushed
                current_header = (m.group("ts"), (m.group("source") or "unknown"))
                buffer = []
            else:
                buffer.append(raw_line)
        flushed = _flush()
        if flushed is not None:
            yield flushed

    def notes_stats(self) -> Dict[str, int]:
        entries = list(self.iter_note_entries())
        return {
            "entries": len(entries),
            "bytes": len(self.get_notes().encode("utf-8")),
        }

    def should_compact(
        self,
        *,
        threshold_entries: int = DEFAULT_COMPACT_THRESHOLD,
    ) -> bool:
        stats = self.notes_stats()
        return stats["entries"] > int(threshold_entries)

    def compact_notes(
        self,
        *,
        threshold_entries: int = DEFAULT_COMPACT_THRESHOLD,
        keep_tail: int = DEFAULT_COMPACT_KEEP_TAIL,
        llm_summarizer: Optional[Callable[[List[NoteEntry]], str]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        entries = list(self.iter_note_entries())
        total = len(entries)
        keep_tail = max(1, int(keep_tail))
        if not force and total <= int(threshold_entries):
            return {"compacted": False, "entries_before": total, "entries_after": total}

        head = entries[:-keep_tail] if total > keep_tail else []
        tail = entries[-keep_tail:] if total > keep_tail else list(entries)
        if not head:
            return {"compacted": False, "entries_before": total, "entries_after": total}

        summary_text = self._summarize(head, llm_summarizer=llm_summarizer)
        summary_block_ts = _now_iso()
        parts: List[str] = []
        parts.append(f"<!-- summary until {summary_block_ts}: {len(head)} entries collapsed -->")
        parts.append(f"## {summary_block_ts} [compactor]")
        parts.append(summary_text)
        parts.append("")
        parts.append("<!-- live entries -->")
        for entry in tail:
            parts.append(f"## {entry.ts} [{entry.source}]")
            parts.append(entry.text)
            parts.append("")

        self._notes_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
        return {
            "compacted": True,
            "entries_before": total,
            "entries_after": len(tail) + 1,  # +1 summary entry
            "collapsed": len(head),
            "summary_ts": summary_block_ts,
        }

    def _summarize(
        self,
        entries: List[NoteEntry],
        *,
        llm_summarizer: Optional[Callable[[List[NoteEntry]], str]],
    ) -> str:
        if llm_summarizer is not None:
            try:
                result = llm_summarizer(list(entries))
                if result and str(result).strip():
                    return str(result).strip()
            except Exception:
                _log.exception("server memory: llm summarizer failed, falling back to naive")
        # Fallback: по источникам и датам.
        by_source: Dict[str, int] = {}
        for e in entries:
            by_source[e.source] = by_source.get(e.source, 0) + 1
        lines = [
            f"Схлопнуто {len(entries)} заметок.",
            "Источники: " + ", ".join(f"{src}={cnt}" for src, cnt in sorted(by_source.items())),
        ]
        first = entries[0]
        last = entries[-1]
        lines.append(f"Период: {first.ts} → {last.ts}.")
        return "\n".join(lines)

    # --- helpers ---

    @staticmethod
    def _normalize_key(key: str) -> str:
        clean = str(key or "").strip()
        if not clean:
            raise ServerMemoryError("fact key is empty")
        if clean == "_meta":
            raise ServerMemoryError("fact key '_meta' is reserved")
        return clean


def _atomic_dump_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            data,
            fh,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            indent=2,
        )
    os.replace(tmp, path)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = [
    "DEFAULT_COMPACT_KEEP_TAIL",
    "DEFAULT_COMPACT_THRESHOLD",
    "NoteEntry",
    "ServerMemory",
    "ServerMemoryError",
    "facts_path",
    "memory_dir",
    "notes_path",
]
