"""Activity log writer for the dashboard "feed" tab (yorishiro fork).

The autonomous things the gateway does on its own — heartbeat speech /
idle gestures, proactive greetings, presence state transitions, smart-home
commands — previously only surfaced as ``logger.info`` lines in journald.
That is fine for tailing a terminal but useless as a durable, structured
record. This module is the missing piece: a fire-and-forget JSONL appender
(one line per event) that those call sites write to, plus a small reader
the ``GET /control/activity`` endpoint tails for the dashboard.

It deliberately mirrors :mod:`stackchan_mcp.event_log`: same atomic-rotation
discipline, same "persistence failures are swallowed at WARNING so the live
path is never broken" contract. The difference is an explicit off switch —
``STACKCHAN_ACTIVITY_LOG=off`` disables logging entirely (the feed simply
shows nothing new) — and a slightly longer default retention window.

Each line carries:

``ts_unix``
    Wall-clock epoch the event was recorded (what the feed sorts/labels by).
``source``
    Who acted: ``heartbeat`` | ``proactive`` | ``presence`` | ``home``.
    (``cron`` and ``report`` items are merged in by the endpoint from other
    on-disk sources; they are not written here.)
``kind``
    What kind of act: ``speak`` | ``gesture`` | ``transition`` | ``command``.
``subtype`` / ``status`` / ``text`` / ``detail`` / ``duration_ms``
    Optional context. ``status`` defaults to ``"ok"``; ``"skipped"`` /
    ``"error"`` are used when an act was suppressed or failed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Final

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH: Final[Path] = Path.home() / ".stackchan" / "activity_log.jsonl"
PATH_ENV_VAR: Final[str] = "STACKCHAN_ACTIVITY_LOG"
RETENTION_ENV_VAR: Final[str] = "STACKCHAN_ACTIVITY_RETENTION_DAYS"
#: Sentinel that turns the activity log off entirely (case-insensitive).
DISABLED_VALUE: Final[str] = "off"
RETENTION_DAYS: Final[int] = 14


def resolve_log_path() -> Path | None:
    """Return the active activity log path, or ``None`` when disabled.

    Honors ``STACKCHAN_ACTIVITY_LOG``: a path override (``~`` expanded), the
    literal ``"off"`` to disable, or unset for the default
    ``~/.stackchan/activity_log.jsonl``.
    """
    override = os.environ.get(PATH_ENV_VAR)
    if override is None:
        return DEFAULT_LOG_PATH
    if override.strip().lower() == DISABLED_VALUE:
        return None
    if not override.strip():
        return DEFAULT_LOG_PATH
    return Path(override).expanduser()


def _retention_seconds() -> int:
    raw = os.environ.get(RETENTION_ENV_VAR)
    days = RETENTION_DAYS
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                days = parsed
        except ValueError:
            logger.warning("activity_log: bad %s=%r; using default", RETENTION_ENV_VAR, raw)
    return days * 24 * 60 * 60


def append(
    source: str,
    kind: str,
    *,
    subtype: str | None = None,
    status: str = "ok",
    text: str | None = None,
    detail: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    path: Path | None = None,
    ts_unix: float | None = None,
) -> None:
    """Append a single activity event to the JSONL log (fire-and-forget).

    ``source`` and ``kind`` are required; the rest are optional context and
    are omitted from the line when ``None``. Disabled
    (``STACKCHAN_ACTIVITY_LOG=off``) is a silent no-op. All disk / encode
    errors are caught and logged at WARNING — a logging failure must never
    propagate into the heartbeat / proactive / device-command path.
    """
    if path is None:
        path = resolve_log_path()
    if path is None:  # disabled
        return
    if ts_unix is None:
        ts_unix = time.time()
    line: dict[str, Any] = {
        "ts_unix": ts_unix,
        "source": source,
        "kind": kind,
        "status": status,
    }
    if subtype is not None:
        line["subtype"] = subtype
    if text is not None:
        line["text"] = text
    if detail is not None:
        line["detail"] = detail
    if duration_ms is not None:
        line["duration_ms"] = duration_ms
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        logger.warning("activity_log: failed to append to %s: %s", path, exc)


def read_recent(
    limit: int = 50,
    *,
    source: str | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` recent entries, newest first.

    Optionally filtered to a single ``source``. A missing / disabled / empty
    log yields ``[]``. Malformed lines are skipped. The activity log is
    low-volume (a handful of events a day, pruned to a couple of weeks), so a
    full read + sort is cheap; callers run it off the event loop anyway.
    """
    if path is None:
        path = resolve_log_path()
    if path is None or not path.exists() or limit <= 0:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = obj.get("ts_unix")
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    continue
                if source is not None and obj.get("source") != source:
                    continue
                rows.append(obj)
    except (OSError, PermissionError) as exc:
        logger.warning("activity_log: failed to read %s: %s", path, exc)
        return []
    rows.sort(key=lambda r: r["ts_unix"], reverse=True)
    return rows[:limit]


def rotate_old_entries(
    *,
    path: Path | None = None,
    now_unix: float | None = None,
) -> None:
    """Prune entries older than the retention window (once, at startup).

    Same atomic write-temp + ``os.replace`` discipline as
    :func:`stackchan_mcp.event_log.rotate_old_entries`. A missing / disabled
    log is a no-op; any disk error is logged at WARNING and swallowed so a
    broken log can never block startup.
    """
    if path is None:
        path = resolve_log_path()
    if path is None or not path.exists():
        return
    if now_unix is None:
        now_unix = time.time()
    cutoff = now_unix - _retention_seconds()

    kept: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = obj.get("ts_unix")
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    continue
                if ts >= cutoff:
                    kept.append(stripped + "\n")
    except (OSError, PermissionError) as exc:
        logger.warning("activity_log: failed to read %s for rotation: %s", path, exc)
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as tmp:
            tmp.writelines(kept)
            tmp.flush()
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    except (OSError, PermissionError) as exc:
        logger.warning("activity_log: failed to rotate %s: %s", path, exc)
