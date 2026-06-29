"""
Revolver — bullets module

Bullet normalization, per-bullet cooldown management, error classification,
health probe, and event telemetry logging.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERMES_HOME = Path.home() / ".hermes"
EVENT_LOG_FILE = HERMES_HOME / ".revolver_events.log"
_MAX_LOG_LINES = 10_000
_TRIM_LOG_LINES = 5_000

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_BULLET_TYPES = {"bearer", "x-api-key", "custom"}


# ---------------------------------------------------------------------------
# Bullet normalization
# ---------------------------------------------------------------------------


def normalize_bullet(b: Any) -> dict:
    """
    Normalize a bullet entry to a canonical dict.

    Accepts:
      - str  -> {"key": <str>, "type": "bearer", "cooldown_seconds": None, "label": None}
      - dict -> {"key": <key>, "type": <type>, "cooldown_seconds": <cooldown_seconds>, "label": <label>}

    Raises ValueError for invalid dicts (missing key, unknown type, empty key).
    label is optional and stored as-is (may be None or a string).
    """
    if isinstance(b, str):
        return {"key": b, "type": "bearer", "cooldown_seconds": None, "label": None}
    if isinstance(b, dict):
        key = b.get("key", "")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("bullet 'key' must be a non-empty string")
        btype = b.get("type", "bearer")
        if btype not in VALID_BULLET_TYPES:
            raise ValueError(
                f"bullet 'type' must be one of {sorted(VALID_BULLET_TYPES)}, got {btype!r}"
            )
        return {
            "key": key.strip(),
            "type": btype,
            "cooldown_seconds": b.get("cooldown_seconds"),
            "label": b.get("label") if isinstance(b.get("label"), str) else None,
        }
    raise ValueError(f"bullet must be a string or dict, got {type(b).__name__}")


# --------------------------------------------------------------------------
# Bullet cooldown helpers
# --------------------------------------------------------------------------


def mark_bullet_cooldown(bullet_cooldowns: dict, bullet_idx: int, duration: float) -> None:
    """Set a cooldown on the given bullet index, expiring at time.time() + duration."""
    if bullet_cooldowns is None:
        return
    bullet_cooldowns[str(bullet_idx)] = time.time() + duration


def is_bullet_available(bullet_cooldowns: dict, bullet_idx: int) -> bool:
    """
    Return True if the bullet index has no active cooldown.
    Returns False if cooldown exists and has not yet expired.
    """
    if not bullet_cooldowns:
        return True
    key = str(bullet_idx)
    if key not in bullet_cooldowns:
        return True
    return time.time() >= bullet_cooldowns[key]


def clear_bullet_cooldowns(bullet_cooldowns: dict) -> None:
    """Remove all per-bullet cooldowns."""
    bullet_cooldowns.clear()


def bullet_cooldown_str(bullet_cooldowns: dict, bullet_idx: int) -> str:
    """
    Return a human-readable cooldown remaining string for a bullet index.
    Returns '' if the bullet has no active cooldown.
    """
    key = str(bullet_idx)
    if not bullet_cooldowns or key not in bullet_cooldowns:
        return ""
    remaining = bullet_cooldowns[key] - time.time()
    if remaining <= 0:
        return ""
    return f" [{int(remaining)}s]"


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------


def classify_error(status_code: Optional[int]) -> str:
    """
    Classify an HTTP status code into an error handling action.

    Returns one of:
      advance   — auth failure (401). Advance bullet immediately. Never retry same.
      cooldown  — rate-limit (429). Bullet may be fine. Set cooldown; retry in place.
      transient — network/server error (408, 502, 503). Do NOT rotate. Retry in place.
      unknown   — anything else. Treat as transient (retry in place).
    """
    if status_code == 401:
        return "advance"
    if status_code == 429:
        return "cooldown"
    if status_code in (408, 502, 503):
        return "transient"
    return "unknown"


# --------------------------------------------------------------------------
# Health probe
# --------------------------------------------------------------------------


def probe_provider(url: str, timeout: float = 5.0) -> bool:
    """
    Make a HEAD request to `url`. Return True if the response status is 2xx or 3xx.
    Return False on any error (timeout, connection refused, 4xx, 5xx).
    Uses the standard library only (urllib.request).
    """
    import urllib.request

    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


# --------------------------------------------------------------------------
# Event telemetry log
# --------------------------------------------------------------------------


def log_event(event_type: str, context: dict) -> None:
    """
    Append a structured JSON line to the event log file.

    Log file: ~/.hermes/.revolver_events.log
    Max lines: _MAX_LOG_LINES (10 000). When exceeded, trim to _TRIM_LOG_LINES
    (5 000) oldest entries.

    Context fields included as-is in the JSON entry, plus:
      ts  — unix timestamp (float)
      event — event_type string
    """
    entry = {"ts": time.time(), "event": event_type, **context}
    try:
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        with open(EVENT_LOG_FILE, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:
        logger.warning("[revolver] Could not write event log (%s)", exc)
        return

    # Lightweight rotation trigger: only scan when file could exceed limit.
    try:
        size = EVENT_LOG_FILE.stat().st_size
        if size > _MAX_LOG_LINES * 80:
            with open(EVENT_LOG_FILE, "rb") as fh:
                data = fh.read()
            lines = data.count(b"\n")
            if lines > _MAX_LOG_LINES:
                parts = data.split(b"\n")
                with open(EVENT_LOG_FILE, "wb") as fh:
                    fh.write(b"\n".join(parts[-_TRIM_LOG_LINES:]) + b"\n")
                logger.info(
                    "[revolver] Log rotated: %d lines -> %d kept", lines, _TRIM_LOG_LINES
                )
    except Exception as exc:
        logger.warning("[revolver] Could not rotate event log (%s)", exc)


def read_log(n: int = 20) -> List[dict]:
    """
    Read the last n entries from the event log file.
    Returns a list of dicts (oldest first).
    """
    try:
        if not EVENT_LOG_FILE.exists():
            return []
        with open(EVENT_LOG_FILE, "r") as fh:
            lines = fh.readlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        return entries[-n:]
    except Exception:
        return []
