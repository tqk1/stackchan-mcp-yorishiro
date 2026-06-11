"""Note (file) tools for the Hermes agent (Phase D).

yorishiro fork specific module (not intended for upstream PR).

Gives Hermes a small, sandboxed place to persist text — memos taken
during voice conversations, search summaries, shopping lists. All
notes live as plain files under one directory so the user can read
them with any editor and the agent cannot touch anything else on the
host.

Environment variables:

- ``STACKCHAN_NOTES_DIR`` — note directory. Defaults to
  ``~/.stackchan/notes``. Created on first write.

Safety: note names are restricted to a single path component (no
separators, no ``..``, no leading dot), the resolved path must stay
inside the notes directory, and one note is capped at
:data:`MAX_NOTE_BYTES`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_NOTES_DIR = "~/.stackchan/notes"

#: Hard ceiling for one note file. Notes are agent memos, not data
#: dumps; 256 KiB is far beyond any realistic voice-driven note.
MAX_NOTE_BYTES = 256 * 1024

#: Extensions a note may carry; a bare name gets ``.md`` appended.
ALLOWED_SUFFIXES = (".md", ".txt")

#: MCP tool names backed by this module (kept in sync with the HTTP
#: daemon's BYPASS_TOOLS — these never touch the ESP32).
TOOL_NAMES = frozenset({"write_note", "read_note", "list_notes"})


def notes_dir() -> Path:
    return Path(
        os.getenv("STACKCHAN_NOTES_DIR", "") or DEFAULT_NOTES_DIR
    ).expanduser()


def _safe_note_path(name: str) -> Path:
    """Validate a note name and return its absolute path.

    Raises ValueError for anything that could escape the notes
    directory or surprise the user (hidden files, odd extensions).
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 100:
        raise ValueError("name too long (max 100 characters)")
    if "/" in name or "\\" in name or os.sep in name:
        raise ValueError("name must not contain path separators")
    if name.startswith(".") or ".." in name:
        raise ValueError("name must not contain '..' or start with '.'")
    if not name.lower().endswith(ALLOWED_SUFFIXES):
        name += ".md"

    base = notes_dir().resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise ValueError("name resolves outside the notes directory")
    return path


def write_note(name: str, content: str, append: bool = False) -> dict[str, Any]:
    """Create or update one note. Returns {ok, name, path, bytes}."""
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    path = _safe_note_path(name)
    data = content.encode("utf-8")
    existing = path.stat().st_size if (append and path.exists()) else 0
    if existing + len(data) > MAX_NOTE_BYTES:
        raise ValueError(
            f"note would exceed {MAX_NOTE_BYTES} bytes "
            f"(existing={existing}, new={len(data)})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab" if append else "wb") as fp:
        fp.write(data)
    return {
        "ok": True,
        "name": path.name,
        "path": str(path),
        "bytes": path.stat().st_size,
        "appended": bool(append),
    }


def read_note(name: str) -> dict[str, Any]:
    """Read one note. Returns {ok, name, content}."""
    path = _safe_note_path(name)
    if not path.exists():
        raise ValueError(f"note not found: {path.name}")
    if path.stat().st_size > MAX_NOTE_BYTES:
        raise ValueError(f"note exceeds {MAX_NOTE_BYTES} bytes; refusing to read")
    content = path.read_text("utf-8", errors="replace")
    return {"ok": True, "name": path.name, "content": content}


def list_notes() -> dict[str, Any]:
    """List notes with size and mtime. Returns {ok, dir, notes: [...]}."""
    base = notes_dir()
    notes = []
    if base.is_dir():
        for path in sorted(base.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if not path.name.lower().endswith(ALLOWED_SUFFIXES):
                continue
            st = path.stat()
            notes.append(
                {
                    "name": path.name,
                    "bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
    return {"ok": True, "dir": str(base), "notes": notes}
