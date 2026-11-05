"""Central-history JSONL file helpers (24h window), moved verbatim from server.py."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# json reads and writes each line; time sets the cutoff; Path and Any are for paths and hints.
import json
import logging
import time
from pathlib import Path
from typing import Any

# Same logger name as server.py so messages land in the same log.
logger = logging.getLogger("client_sim_dashboard")
# The history file sits in the webui-spoke directory and keeps 24 hours of records.
BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = BASE_DIR / "central_history.jsonl"
HISTORY_HOURS = 24


# ── History file helpers ──────────────────────────────────────────────────────
def _history_cutoff() -> float:
    """Return the oldest timestamp (epoch seconds) that is still inside the history window."""
    return time.time() - HISTORY_HOURS * 3600


def _load_history() -> list[dict[str, Any]]:
    """Load last 24 h from the JSONL file into memory."""
    # No file yet means no history.
    if not HISTORY_FILE.exists():
        return []
    cutoff = _history_cutoff()
    result: list[dict[str, Any]] = []
    # Read every line, keeping valid records newer than the cutoff.
    try:
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("ts", 0) >= cutoff:
                    result.append(record)
            except json.JSONDecodeError:
                # Skip damaged lines instead of failing the whole load.
                pass
    except Exception as exc:
        # An unreadable file is logged and treated as empty.
        logger.warning("Could not read history file: %s", exc)
    return result


# Compact (drop expired lines) only once the file exceeds this size — the old
# behavior read + rewrote the ENTIRE 24h window on every poll.
_COMPACT_MAX_BYTES = 5 * 1024 * 1024


def _append_and_trim_history(new_records: list[dict[str, Any]]) -> None:
    """Append new records (append-only JSONL); compact when the file is large.

    Was: read + filter + rewrite the whole 24h file per poll — O(window) disk
    work every cycle. Now the poll cost is a pure append; expired (>24h) lines
    are left in place between compactions, which is safe because
    ``_load_history`` already filters by the cutoff on read. Compaction
    (``_compact_history``) rewrites the file with only in-window lines once it
    grows past ``_COMPACT_MAX_BYTES``, preserving the same 24h retention."""
    # Append each record as one JSON line.
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            for record in new_records:
                fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        # A write failure is logged; skip compaction this time.
        logger.warning("Could not write history file: %s", exc)
        return

    # Compact only once the file is bigger than the limit.
    try:
        if HISTORY_FILE.stat().st_size > _COMPACT_MAX_BYTES:
            _compact_history()
    except Exception as exc:
        # Compaction problems are logged but never raised.
        logger.warning("Could not compact history file: %s", exc)


def _compact_history() -> None:
    """Rewrite the JSONL file keeping only lines within the 24h window
    (atomic: temp file + replace). Malformed lines are dropped — same as the
    old per-poll trim did."""
    # Keep only valid lines that are inside the window.
    cutoff = _history_cutoff()
    existing: list[str] = []
    try:
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("ts", 0) >= cutoff:
                    existing.append(line)
            except json.JSONDecodeError:
                # Damaged lines are dropped.
                pass
    except Exception as exc:
        # If the file cannot be read, leave it as it is.
        logger.warning("Could not read history file for compaction: %s", exc)
        return
    # Write to a temp file, then swap it in so a crash never leaves a half-written file.
    try:
        tmp = HISTORY_FILE.with_suffix(HISTORY_FILE.suffix + ".tmp")
        tmp.write_text(("\n".join(existing) + "\n") if existing else "",
                       encoding="utf-8")
        tmp.replace(HISTORY_FILE)
    except Exception as exc:
        logger.warning("Could not write compacted history file: %s", exc)
