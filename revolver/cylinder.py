"""
Revolver — cylinder module

Cylinder definitions (CylinderDef, CylinderState), state persistence,
state machine transitions (_advance), graph rendering, file-based locking,
recovery thread, and revolver.yaml loading.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

from .bullets import (
    bullet_cooldown_str,
    describe_secret,
    is_bullet_available,
    log_event,
    normalize_bullet,
    normalize_error_policy,
    probe_provider,
    read_log,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERMES_HOME = Path.home() / ".hermes"
REVOLVER_YAML = HERMES_HOME / "revolver.yaml"
STATE_FILE = HERMES_HOME / ".revolver_state.json"
LOCK_FILE = HERMES_HOME / ".revolver.lock"

# Lock parameters
_LOCK_TIMEOUT_SEC = 5.0   # how long to wait to acquire lock
_LOCK_STALE_SEC = 10.0    # treat lock as stale after this many seconds

# ---------------------------------------------------------------------------
# Module-level globals — recovery thread state
# ---------------------------------------------------------------------------

_recovery_thread: Optional[threading.Thread] = None
_recovery_stop = threading.Event()
# Pending recovery message to deliver to user in the next session
_pending_recovery_message: Optional[str] = None
# Module-level cylinder cache updated by register()
cylinders_cache: List["CylinderDef"] = []


# ---------------------------------------------------------------------------
# Persistent state (JSON-serialisable)
# ---------------------------------------------------------------------------

STATE_DEFAULTS = {
    "cylinder": 0,
    "bullet": -1,
    "state": "CYLINDER_ACTIVE",
    "cooldown_until": 0.0,
    "bullet_cooldowns": {},
    "consecutive_failures": 0,
    "recovery_thread_active": False,
}


@dataclass
class CylinderState:
    """Snapshot of the current revolver state."""

    cylinder: int = 0
    bullet: int = -1
    state: str = "CYLINDER_ACTIVE"
    cooldown_until: float = 0.0
    # bullet index (str) -> unix timestamp when cooldown expires
    bullet_cooldowns: Dict[str, float] = None
    # consecutive 401s on the current bullet before advancing
    consecutive_failures: int = 0
    # advisory flag: a recovery thread is running to auto-reset from ALL_EXHAUSTED
    recovery_thread_active: bool = False

    def __post_init__(self) -> None:
        if self.bullet_cooldowns is None:
            self.bullet_cooldowns = {}


# ---------------------------------------------------------------------------
# Cylinder definition (loaded from revolver.yaml)
# ---------------------------------------------------------------------------


@dataclass
class CylinderDef:
    """One cylinder in the fallback chain."""

    delegation: Dict[str, str]          # {model, provider}
    bullets: List[Dict[str, Any]]       # always normalized: {key, type, cooldown_seconds}
    cooldown_seconds: int = 60
    consecutive_failures_threshold: int = 2   # N 401s before advancing bullet
    probe_url: Optional[str] = None      # optional health probe URL before exhausting

    @property
    def model(self) -> str:
        return self.delegation.get("model", "")

    @property
    def provider(self) -> str:
        return self.delegation.get("provider", "")

    def get_bullet_key(self, idx: int) -> Optional[str]:
        """Return the API key string for bullet idx, or None."""
        if idx < 0 or idx >= len(self.bullets):
            return None
        return self.bullets[idx].get("key", "") or None

    def get_bullet_type(self, idx: int) -> str:
        """Return the auth type for bullet idx ('bearer', 'x-api-key', 'custom')."""
        if idx < 0 or idx >= len(self.bullets):
            return "bearer"
        return self.bullets[idx].get("type", "bearer")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_state() -> CylinderState:
    """Load state from the JSON file. Return defaults if missing or invalid."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r") as fh:
                raw = json.load(fh)
            return CylinderState(
                cylinder=int(raw.get("cylinder", 0)),
                bullet=int(raw.get("bullet", -1)),
                state=str(raw.get("state", "CYLINDER_ACTIVE")),
                cooldown_until=float(raw.get("cooldown_until", 0.0)),
                bullet_cooldowns={
                    str(k): float(v)
                    for k, v in raw.get("bullet_cooldowns", {}).items()
                },
                consecutive_failures=int(raw.get("consecutive_failures", 0)),
                recovery_thread_active=bool(raw.get("recovery_thread_active", False)),
            )
    except Exception as exc:
        logger.warning("[revolver] Could not load state (%s); using defaults", exc)
    return CylinderState(**STATE_DEFAULTS)


def save_state(state: CylinderState) -> None:
    """Persist state to the JSON file."""
    try:
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(asdict(state), fh)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        logger.error("[revolver] Could not save state (%s)", exc)


# ---------------------------------------------------------------------------
# revolver.yaml loading and validation
# ---------------------------------------------------------------------------


def load_revolver_yaml() -> List[CylinderDef]:
    """
    Load and validate revolver.yaml.

    Validation:
      - Each cylinder must have delegation.model and delegation.provider.
      - Bullets must be strings or dicts with a `key` field.
      - No duplicate cylinders (same model+provider).

    Returns a list of CylinderDef objects.

    Raises ValueError if the file is missing or validation fails.
    """
    import yaml

    if not REVOLVER_YAML.exists():
        raise ValueError(f"revolver.yaml not found at {REVOLVER_YAML}")

    with open(REVOLVER_YAML, "r") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "cylinders" not in raw:
        raise ValueError("revolver.yaml must have a top-level 'cylinders' key")

    cylinders_raw: List[Dict[str, Any]] = raw["cylinders"]
    if not isinstance(cylinders_raw, list):
        raise ValueError("'cylinders' must be a list")

    seen: Dict[Tuple[str, str], int] = {}
    parsed: List[CylinderDef] = []

    for i, c in enumerate(cylinders_raw):
        if not isinstance(c, dict):
            raise ValueError(f"Cylinder {i} is not a dict")

        delegation: Dict[str, str] = c.get("delegation", {})
        if not isinstance(delegation, dict):
            raise ValueError(f"Cylinder {i}: 'delegation' must be a dict")
        model = delegation.get("model", "").strip()
        provider = delegation.get("provider", "").strip()
        if not model:
            raise ValueError(f"Cylinder {i}: delegation.model is required")
        if not provider:
            raise ValueError(f"Cylinder {i}: delegation.provider is required")

        bullets_raw = c.get("bullets", [])
        if not isinstance(bullets_raw, list):
            raise ValueError(f"Cylinder {i}: 'bullets' must be a list")
        bullets: List[Dict[str, Any]] = []
        for j, b in enumerate(bullets_raw):
            try:
                bullets.append(normalize_bullet(b))
            except ValueError as ve:
                raise ValueError(f"Cylinder {i} bullet {j}: {ve}") from None

        cooldown = int(c.get("cooldown_seconds", 60))
        if cooldown < 0:
            raise ValueError(f"Cylinder {i}: cooldown_seconds must be >= 0")

        threshold = int(c.get("consecutive_failures_threshold", 2))
        if threshold < 1:
            raise ValueError(f"Cylinder {i}: consecutive_failures_threshold must be >= 1")

        probe_url = c.get("probe_url")
        if probe_url is not None and not isinstance(probe_url, str):
            raise ValueError(f"Cylinder {i}: probe_url must be a string if provided")

        key = (model, provider)
        if key in seen:
            raise ValueError(
                f"Duplicate cylinder: model={model!r} provider={provider!r} "
                f"(first seen at index {seen[key]})"
            )
        seen[key] = i

        parsed.append(CylinderDef(
            delegation=delegation,
            bullets=bullets,
            cooldown_seconds=cooldown,
            consecutive_failures_threshold=threshold,
            probe_url=probe_url,
        ))

    return parsed


def _load_raw_yaml() -> dict:
    """Read and parse revolver.yaml once; return the raw dict."""
    import yaml

    if not REVOLVER_YAML.exists():
        raise ValueError(f"revolver.yaml not found at {REVOLVER_YAML}")
    with open(REVOLVER_YAML, "r") as fh:
        raw = yaml.safe_load(fh)
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("revolver.yaml must be a dict")
    return raw or {}


def load_error_policy() -> dict:
    """Load top-level error_policy from revolver.yaml, merged with defaults."""
    raw = _load_raw_yaml()
    return normalize_error_policy(raw.get("error_policy"))


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


class LockContext:
    """Context manager for file-based locking. Raises RuntimeError if contended."""

    def __init__(self, path: Path, timeout: float = _LOCK_TIMEOUT_SEC):
        self._path = path
        self._timeout = timeout
        self._fd: Optional[int] = None

    def __enter__(self) -> Path:
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
        pid = os.getpid()
        ts = f"{pid}:{time.time():.3f}"

        def _try_acquire(blocking: bool) -> bool:
            try:
                if fcntl is not None:
                    mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                    fcntl.flock(self._fd, mode)
                elif msvcrt is not None:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                    msvcrt.locking(self._fd, mode, 1)
                else:
                    raise OSError("no supported file locking API")
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.ftruncate(self._fd, 0)
                os.write(self._fd, ts.encode())
                os.fsync(self._fd)
                return True
            except (IOError, OSError):
                return False

        # Try non-blocking first
        if _try_acquire(blocking=False):
            return self._path

        # Check if existing lock is stale (>10 s old)
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            content = os.read(self._fd, 256).decode().strip()
            if content:
                parts = content.split(":")
                if len(parts) == 2:
                    lock_pid = int(parts[0])
                    lock_time = float(parts[1])
                    if time.time() - lock_time > _LOCK_STALE_SEC:
                        logger.warning(
                            "[revolver] Stale lock from PID %d; breaking it", lock_pid
                        )
                        if fcntl is not None:
                            fcntl.flock(self._fd, fcntl.LOCK_UN)
                        elif msvcrt is not None:
                            os.lseek(self._fd, 0, os.SEEK_SET)
                            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                        os.close(self._fd)
                        self._fd = os.open(
                            str(self._path), os.O_CREAT | os.O_RDWR | os.O_TRUNC
                        )
                        _try_acquire(blocking=False)
                        return self._path
        except Exception:
            pass

        # Block for up to _LOCK_TIMEOUT_SEC
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            if _try_acquire(blocking=False):
                return self._path
            time.sleep(0.1)

        # Couldn't acquire
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
        raise RuntimeError(
            f"[revolver] Could not acquire lock {self._path} "
            f"within {_LOCK_TIMEOUT_SEC}s — another process holds it"
        )

    def __exit__(self, *args: Any) -> None:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None


# ---------------------------------------------------------------------------
# Core state transition logic — _advance
# ---------------------------------------------------------------------------


def advance(
    cylinders: List[CylinderDef],
    state: CylinderState,
    bullets_cooldown: dict = None,
) -> Tuple[CylinderState, str]:
    """
    Advance through the fallback chain.

    Status values:
      ADVANCED: selected another bullet in the current cylinder.
      EXHAUSTED: moved to the next cylinder.
      ALL_COOLDOWN: current cylinder has bullets, but all are cooling down.
      ALL_EXHAUSTED: no remaining cylinders have usable bullets.
    """
    idx = state.cylinder
    bidx = state.bullet
    n_cylinders = len(cylinders)

    if n_cylinders == 0 or idx >= n_cylinders or state.state == "ALL_EXHAUSTED":
        return CylinderState(
            cylinder=max(idx, n_cylinders),
            bullet=-1,
            state="ALL_EXHAUSTED",
            cooldown_until=0.0,
            bullet_cooldowns={},
            consecutive_failures=0,
        ), "ALL_EXHAUSTED"

    cooldown = bullets_cooldown if bullets_cooldown is not None else state.bullet_cooldowns

    def _find_available(cyl: CylinderDef, start_after: int, active_cooldowns: dict) -> int:
        n_bullets = len(cyl.bullets)
        if n_bullets == 0:
            return -1
        start = (max(start_after, -1) + 1) % n_bullets
        current = start
        for _ in range(n_bullets):
            if is_bullet_available(active_cooldowns, current):
                return current
            current = (current + 1) % n_bullets
        return -1

    if state.state != "CYLINDER_EXHAUSTED":
        cyl = cylinders[idx]
        if cyl.bullets:
            found_idx = _find_available(cyl, bidx, cooldown if cooldown is not None else {})
            if found_idx >= 0:
                return CylinderState(
                    cylinder=idx,
                    bullet=found_idx,
                    state="CYLINDER_ACTIVE",
                    cooldown_until=state.cooldown_until,
                    bullet_cooldowns=cooldown if cooldown is not None else {},
                    consecutive_failures=state.consecutive_failures,
                ), "ADVANCED"
            return CylinderState(
                cylinder=idx,
                bullet=bidx,
                state="CYLINDER_ACTIVE",
                cooldown_until=state.cooldown_until,
                bullet_cooldowns=cooldown if cooldown is not None else {},
                consecutive_failures=state.consecutive_failures,
            ), "ALL_COOLDOWN"

    for next_idx in range(idx + 1, n_cylinders):
        found_idx = _find_available(cylinders[next_idx], -1, {})
        if found_idx >= 0:
            return CylinderState(
                cylinder=next_idx,
                bullet=found_idx,
                state="CYLINDER_ACTIVE",
                cooldown_until=0.0,
                bullet_cooldowns={},
                consecutive_failures=0,
            ), "EXHAUSTED"

    return CylinderState(
        cylinder=n_cylinders,
        bullet=-1,
        state="ALL_EXHAUSTED",
        cooldown_until=0.0,
        bullet_cooldowns={},
        consecutive_failures=0,
    ), "ALL_EXHAUSTED"


# ---------------------------------------------------------------------------
# Graph rendering
# ---------------------------------------------------------------------------


def format_graph(cylinders: List[CylinderDef], state: CylinderState) -> str:
    """
    Render an ASCII representation of the full fallback chain.

    Current position is marked with \u25cf.
    Bullet indicators:
      \u25cf = active (current)
      \u2299 = in cooldown (available but timer running)
      \u25cb = exhausted (behind current, already used)
    """
    lines: List[str] = []
    for i, cyl in enumerate(cylinders):
        n = len(cyl.bullets)
        label = f"Cylinder {i}: {cyl.provider} / {cyl.model}"

        if i == state.cylinder:
            # Current cylinder
            if state.state == "CYLINDER_ACTIVE":
                marker = "\u25cf"
                cf_str = f"  cf={state.consecutive_failures}" if state.consecutive_failures > 0 else ""
                if state.bullet == -1:
                    if n == 0:
                        status = f"  {marker} no bullets (pending){cf_str}"
                    else:
                        status = f"  {marker} bullet -1/{n} pending{cf_str}"
                else:
                    status = f"  {marker} bullet {state.bullet}/{n} active{cf_str}"
            elif state.state == "CYLINDER_EXHAUSTED":
                marker = "\u25cf"
                status = f"  {marker} exhausted"
            else:
                marker = "\u25cf"
                status = f"  {marker} {state.state.lower()}"
            lines.append(f"{label}  {status}")
        elif i < state.cylinder:
            # Past cylinders
            lines.append(f"{label}  \u25cb exhausted")
        else:
            # Future cylinders
            if n == 0:
                status = "  bullet -1/0 pending"
            else:
                status = f"  bullet -1/{n} pending"
            lines.append(f"{label}{status}")

        # Bullet detail with status indicators
        if n > 0:
            bullet_parts: List[str] = []
            for k in range(n):
                bdef = cyl.bullets[k]
                btype = bdef.get("type", "bearer")
                label_text = bdef.get("label")
                label_str = f" [{label_text}]" if label_text else ""
                if i == state.cylinder and state.state == "CYLINDER_ACTIVE":
                    if k == state.bullet:
                        cooldown_str = bullet_cooldown_str(state.bullet_cooldowns, k)
                        bullet_parts.append(f"{btype}{label_str} \u25cf{cooldown_str}")
                    elif is_bullet_available(state.bullet_cooldowns, k):
                        bullet_parts.append(f"{btype}{label_str} \u25cb")
                    else:
                        cooldown_str = bullet_cooldown_str(state.bullet_cooldowns, k)
                        bullet_parts.append(f"{btype}{label_str} \u2299{cooldown_str}")
                elif i == state.cylinder and state.state == "CYLINDER_EXHAUSTED":
                    bullet_parts.append(f"{btype}{label_str} \u25cb")
                else:
                    bullet_parts.append(f"{btype}{label_str}")
            lines.append(f"  bullets: {', '.join(bullet_parts)}")

    if state.state == "ALL_EXHAUSTED":
        lines.append("")
        lines.append("  \u26a0  ALL_EXHAUSTED — /revolver reset to recover")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public helpers (used by hooks and commands)
# ---------------------------------------------------------------------------


def get_active_delegation(
    cylinders: List[CylinderDef], state: CylinderState
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (model, provider, api_key) for the current cylinder/bullet."""
    if state.state == "ALL_EXHAUSTED" or state.cylinder >= len(cylinders):
        return None, None, None
    cyl = cylinders[state.cylinder]
    api_key = cyl.get_bullet_key(state.bullet) if state.bullet >= 0 else None
    return cyl.model, cyl.provider, api_key


def get_active_delegation_dict(
    cylinders: List[CylinderDef], state: CylinderState
) -> dict:
    """Return active delegation config as a redacted routing contract."""
    return resolve_active_delegation(cylinders, state, include_secret=False)


def resolve_active_delegation(
    cylinders: List[CylinderDef], state: CylinderState, include_secret: bool = False
) -> dict:
    """Return the active delegation contract for host routing integrations."""
    if not cylinders:
        return {"error": "No cylinders configured"}
    if state.state == "ALL_EXHAUSTED" or state.cylinder >= len(cylinders):
        return {
            "active": False,
            "model": "",
            "provider": "",
            "cylinder": state.cylinder,
            "bullet": state.bullet,
            "state": state.state,
            "apply": None,
            "auth": None,
        }
    cyl = cylinders[state.cylinder]
    del_cfg = cyl.delegation
    api_key = cyl.get_bullet_key(state.bullet) if state.bullet >= 0 else None
    auth: Dict[str, Any] = {
        "type": cyl.get_bullet_type(state.bullet),
        "key_configured": bool(api_key),
        "key": api_key if include_secret else describe_secret(api_key),
    }
    return {
        "active": True,
        "model": del_cfg.get("model", ""),
        "provider": del_cfg.get("provider", ""),
        "cylinder": state.cylinder,
        "bullet": state.bullet,
        "state": state.state,
        "apply": {
            "model": del_cfg.get("model", ""),
            "provider": del_cfg.get("provider", ""),
        },
        "auth": auth,
    }


def is_cylinder_in_cooldown(cylinders: List[CylinderDef], state: CylinderState) -> bool:
    """Return True if the current cylinder is still in its cooldown period."""
    if state.state == "ALL_EXHAUSTED":
        return False
    if state.cooldown_until <= 0:
        return False
    return time.time() < state.cooldown_until


def get_health_snapshot(cylinders: List[CylinderDef], state: CylinderState) -> dict:
    """Return a redacted operational health snapshot for commands and tools."""
    delegation = resolve_active_delegation(cylinders, state, include_secret=False)
    events = read_log(50)
    event_counts: Dict[str, int] = {}
    for event in events:
        name = str(event.get("event", "unknown"))
        event_counts[name] = event_counts.get(name, 0) + 1
    cooldowns: List[dict] = []
    if state.cylinder < len(cylinders):
        cyl = cylinders[state.cylinder]
        for raw_idx, expires_at in state.bullet_cooldowns.items():
            try:
                idx = int(raw_idx)
            except ValueError:
                continue
            remaining = max(0, int(float(expires_at) - time.time()))
            if remaining > 0:
                cooldowns.append({
                    "bullet": idx,
                    "remaining_seconds": remaining,
                    "auth_type": cyl.get_bullet_type(idx),
                })
    return {
        "ok": bool(delegation.get("active")),
        "state": state.state,
        "active_delegation": delegation,
        "cylinders": len(cylinders),
        "current_cylinder_bullets": (
            len(cylinders[state.cylinder].bullets) if state.cylinder < len(cylinders) else 0
        ),
        "cooldowns": cooldowns,
        "recent_event_counts": event_counts,
        "last_event": events[-1] if events else None,
    }


def doctor_revolver_config() -> dict:
    """Validate local Revolver setup and return redacted findings."""
    result = {
        "ok": True,
        "config_path": str(REVOLVER_YAML),
        "state_path": str(STATE_FILE),
        "errors": [],
        "warnings": [],
        "cylinders": 0,
        "bullets": 0,
        "policy": None,
    }
    try:
        raw_yaml = _load_raw_yaml()
        cylinders = load_revolver_yaml()
        policy = normalize_error_policy(raw_yaml.get("error_policy"))
        result["cylinders"] = len(cylinders)
        result["bullets"] = sum(len(c.bullets) for c in cylinders)
        result["policy"] = policy
        if not cylinders:
            result["warnings"].append("No cylinders configured")
        for idx, cyl in enumerate(cylinders):
            if not cyl.bullets:
                result["warnings"].append(f"Cylinder {idx} has no bullets and will be skipped")
            if cyl.probe_url and not cyl.probe_url.startswith(("http://", "https://")):
                result["warnings"].append(f"Cylinder {idx} has a non-HTTP probe_url")
    except Exception as exc:
        result["ok"] = False
        result["errors"].append(str(exc))

    if REVOLVER_YAML.exists():
        try:
            mode = REVOLVER_YAML.stat().st_mode & 0o777
            if os.name != "nt" and mode & 0o077:
                result["warnings"].append("revolver.yaml is readable by group/other; use chmod 600")
        except OSError as exc:
            result["warnings"].append(f"Could not inspect config permissions: {exc}")
    else:
        result["ok"] = False
        result["errors"].append(f"Missing config file: {REVOLVER_YAML}")

    if result["errors"]:
        result["ok"] = False
    return result


# -------------------------------------------------------------------------
# Recovery thread — ALL_EXHAUSTED auto-recovery
# -------------------------------------------------------------------------


def stop_recovery_thread() -> None:
    """Stop any running recovery thread and wait for it to exit."""
    global _recovery_thread, _recovery_stop
    if _recovery_thread is None or not _recovery_thread.is_alive():
        return
    _recovery_stop.set()
    _recovery_thread.join(timeout=5.0)
    _recovery_thread = None


def _request_reset(message: Optional[str] = None) -> None:
    """Request a reset on the next session start."""
    global _pending_recovery_message
    stop_recovery_thread()
    _pending_recovery_message = message
    logger.info("[revolver] Reset scheduled for next session start")


def start_recovery_check(interval: float = 300.0) -> None:
    """Start a background thread that periodically probes cylinder 0."""
    global _recovery_thread, _recovery_stop, cylinders_cache

    if _recovery_thread is not None and _recovery_thread.is_alive():
        return

    _recovery_stop.clear()
    _recovery_thread = threading.Thread(
        target=_recovery_loop,
        args=(interval,),
        daemon=True,
        name="revolver-recovery",
    )
    _recovery_thread.start()
    logger.info("[revolver] Recovery thread started (interval=%.0fs)", interval)


def _recovery_loop(interval: float) -> None:
    """Periodically probe cylinder 0 until it recovers or is stopped."""
    global _recovery_stop, cylinders_cache

    while not _recovery_stop.is_set():
        _recovery_stop.wait(interval)
        if _recovery_stop.is_set():
            break

        # Check current state
        try:
            state = load_state()
        except Exception:
            continue
        if state.state != "ALL_EXHAUSTED":
            logger.info("[revolver] Recovery loop: state=%s — stopping", state.state)
            break

        # Load cylinder 0 config
        try:
            cylinders = load_revolver_yaml()
        except Exception:
            continue
        if not cylinders:
            continue
        cyl0 = cylinders[0]
        probe_url = cyl0.probe_url or ""

        if probe_url:
            logger.info("[revolver] Recovery probe: checking %s", probe_url)
            if probe_provider(probe_url):
                logger.warning(
                    "[revolver] \u2605 Recovery SUCCESS — cylinder 0 (%s/%s) is back online; scheduling reset",
                    cyl0.provider, cyl0.model,
                )
                log_event("recovery_success", {
                    "cylinder": 0,
                    "model": cyl0.model,
                    "provider": cyl0.provider,
                    "probe_url": probe_url,
                })
                _request_reset(
                    f"[revolver] \u2605 Recovery SUCCESS — cylinder 0 ({cyl0.provider}/{cyl0.model}) "
                    f"is back online; reset complete"
                )
                break
            else:
                logger.info("[revolver] Recovery probe failed for %s", probe_url)
                log_event("recovery_attempt_failed", {
                    "cylinder": 0,
                    "model": cyl0.model,
                    "provider": cyl0.provider,
                    "probe_url": probe_url,
                })
        else:
            logger.info(
                "[revolver] Recovery loop: cylinder 0 has no probe_url, waiting %.0fs",
                interval,
            )

    logger.info("[revolver] Recovery thread exiting")
