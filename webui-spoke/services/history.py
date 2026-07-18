"""Central-history JSONL file helpers (24h window), moved verbatim from server.py."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("client_sim_dashboard")
BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = BASE_DIR / "central_history.jsonl"
HISTORY_HOURS = 24


# ── History file helpers ──────────────────────────────────────────────────────
def _history_cutoff() -> float:
    return time.time() - HISTORY_HOURS * 3600


def _load_history() -> list[dict[str, Any]]:
    """Load last 24 h from the JSONL file into memory."""
    if not HISTORY_FILE.exists():
        return []
    cutoff = _history_cutoff()
    result: list[dict[str, Any]] = []
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
                pass
    except Exception as exc:
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
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            for record in new_records:
                fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("Could not write history file: %s", exc)
        return

    try:
        if HISTORY_FILE.stat().st_size > _COMPACT_MAX_BYTES:
            _compact_history()
    except Exception as exc:
        logger.warning("Could not compact history file: %s", exc)


def _compact_history() -> None:
    """Rewrite the JSONL file keeping only lines within the 24h window
    (atomic: temp file + replace). Malformed lines are dropped — same as the
    old per-poll trim did."""
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
                pass
    except Exception as exc:
        logger.warning("Could not read history file for compaction: %s", exc)
        return
    try:
        tmp = HISTORY_FILE.with_suffix(HISTORY_FILE.suffix + ".tmp")
        tmp.write_text(("\n".join(existing) + "\n") if existing else "",
                       encoding="utf-8")
        tmp.replace(HISTORY_FILE)
    except Exception as exc:
        logger.warning("Could not write compacted history file: %s", exc)
