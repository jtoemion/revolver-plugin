"""
Hermes plugin — revolver

A hardened cascading-fallback plugin for delegation config, modelled as a
revolver cylinder chain.

Submodules:
  cylinder — CylinderDef, CylinderState, state persistence, state machine,
             locking, recovery thread, yaml loading, graph rendering
  bullets  — bullet normalization, per-bullet cooldown, error classification,
             health probe, event telemetry log
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional, Tuple

from .bullets import (
    clear_bullet_cooldowns,
    classify_error,
    log_event,
    mark_bullet_cooldown,
    probe_provider,
    read_log,
)
from .cylinder import (
    CylinderDef,
    CylinderState,
    LockContext,
    STATE_DEFAULTS,
    LOCK_FILE,
    REVOLVER_YAML,
    advance,
    cylinders_cache,
    format_graph,
    get_active_delegation,
    get_active_delegation_dict,
    is_cylinder_in_cooldown,
    load_revolver_yaml,
    load_state,
    save_state,
    start_recovery_check,
    stop_recovery_thread,
    _pending_recovery_message,
)

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Plugin entry point
# -------------------------------------------------------------------------

def register(ctx) -> None:
    """
    Hermes plugin entry point — called once at startup.
    """
    logger.info("[revolver] Plugin loading — reading cylinder definitions")

    # Load cylinder definitions (validated once at startup)
    try:
        _cylinders: List[CylinderDef] = load_revolver_yaml()
        logger.info("[revolver] Loaded %d cylinder(s) from revolver.yaml", len(_cylinders))
    except Exception as exc:
        logger.error("[revolver] Failed to load revolver.yaml (%s); plugin disabled", exc)
        _cylinders = []

    # Load or initialise persistent state
    _state: CylinderState = load_state()
    logger.info(
        "[revolver] State: cylinder=%d bullet=%d state=%s",
        _state.cylinder, _state.bullet, _state.state,
    )

    # -------------------------------------------------------------------------
    # Helper: perform a locked state mutation
    # Returns (new_state, ok). ok=False means lock contention (do not block).
    # -------------------------------------------------------------------------
    def _mutate(fn) -> Tuple[CylinderState, bool]:
        """Apply fn(state) inside a lock. Returns (new_state, acquired)."""
        if not _cylinders:
            return _state, True
        try:
            with LockContext(LOCK_FILE):
                current = load_state()
                updated = fn(current)
                save_state(updated)
                return updated, True
        except RuntimeError:
            logger.warning("[revolver] Lock contention during state mutation; skipping")
            return _state, False
        except Exception as exc:
            logger.error("[revolver] State mutation failed (%s)", exc)
            return _state, False

    # -------------------------------------------------------------------------
    # Hook: on_session_start
    # -------------------------------------------------------------------------
    @ctx.on("on_session_start")
    def on_session_start(session_id: str = "") -> None:
        """Log current cylinder/bullet on session start."""
        global _pending_recovery_message

        # Check for pending recovery message from background thread
        if _pending_recovery_message is not None:
            ctx.inject_message(_pending_recovery_message, role="user")
            _pending_recovery_message = None

        if not _cylinders:
            logger.info("[revolver] No cylinders configured")
            return
        s = load_state()
        model, provider, api_key = get_active_delegation(_cylinders, s)
        if model:
            key_preview = (api_key[:8] + "...") if api_key else "(none)"
            logger.info(
                "[revolver] Session %s — active: %s / %s [key=%s] bullet=%d/%d state=%s",
                session_id or "(unknown)", provider, model, key_preview,
                s.bullet, len(_cylinders[s.cylinder].bullets) if s.cylinder < len(_cylinders) else 0,
                s.state,
            )
        else:
            logger.info("[revolver] Session %s — ALL_EXHAUSTED", session_id or "(unknown)")

    # -------------------------------------------------------------------------
    # Hook: api_request_error
    # -------------------------------------------------------------------------
    @ctx.on("api_request_error")
    def on_api_request_error(
        status_code: Optional[int] = None,
        error_type: str = "",
        error_message: str = "",
        model: str = "",
        provider: str = "",
        **kwargs: Any,
    ) -> None:
        """Classify API errors and advance/cooldown/retry accordingly."""
        try:
            _on_api_request_error_impl(
                status_code=status_code,
                error_type=error_type,
                error_message=error_message,
                model=model,
                provider=provider,
                **kwargs,
            )
        except Exception as exc:
            logger.exception(
                "[revolver] Unexpected exception in api_request_error hook: %s", exc
            )
            log_event("error", {
                "status_code": str(status_code) if status_code else "",
                "error_type": error_type,
                "error_message": error_message,
                "model": model,
                "provider": provider,
            })

    def _on_api_request_error_impl(
        status_code: Optional[int],
        error_type: str,
        error_message: str,
        model: str,
        provider: str,
        **kwargs: Any,
    ) -> None:
        if not _cylinders:
            return

        action = classify_error(status_code)
        logger.info(
            "[revolver] api_request_error — status=%s action=%s model=%s provider=%s",
            status_code, action, model, provider,
        )

        _action_for_log = action

        def _make_transition(s: CylinderState) -> CylinderState:
            if action == "advance":
                # 401 — increment consecutive failure counter.
                cyl = _cylinders[s.cylinder] if s.cylinder < len(_cylinders) else None
                threshold = cyl.consecutive_failures_threshold if cyl else 2
                s = CylinderState(
                    cylinder=s.cylinder,
                    bullet=s.bullet,
                    state=s.state,
                    cooldown_until=s.cooldown_until,
                    bullet_cooldowns=s.bullet_cooldowns,
                    consecutive_failures=s.consecutive_failures + 1,
                )
                if s.consecutive_failures >= threshold:
                    cyl = _cylinders[s.cylinder] if s.cylinder < len(_cylinders) else None
                    if cyl and cyl.probe_url:
                        probe_ok = probe_provider(cyl.probe_url)
                        if probe_ok:
                            logger.info(
                                "[revolver] probe_url %s passed — staying on bullet %d, resetting failures",
                                cyl.probe_url, s.bullet,
                            )
                            log_event("probe_passed", {
                                "cylinder": s.cylinder,
                                "bullet": s.bullet,
                                "model": cyl.model,
                                "provider": cyl.provider,
                                "state": s.state,
                                "consecutive_failures": 0,
                                "probe_url": cyl.probe_url,
                            })
                            return CylinderState(
                                cylinder=s.cylinder,
                                bullet=s.bullet,
                                state=s.state,
                                cooldown_until=s.cooldown_until,
                                bullet_cooldowns=s.bullet_cooldowns,
                                consecutive_failures=0,
                            )
                        else:
                            logger.info(
                                "[revolver] probe_url %s failed — proceeding to advance bullet",
                                cyl.probe_url,
                            )
                            log_event("probe_failed", {
                                "cylinder": s.cylinder,
                                "bullet": s.bullet,
                                "model": cyl.model,
                                "provider": cyl.provider,
                                "state": s.state,
                                "consecutive_failures": s.consecutive_failures,
                                "probe_url": cyl.probe_url,
                            })
                    s = CylinderState(
                        cylinder=s.cylinder,
                        bullet=s.bullet,
                        state=s.state,
                        cooldown_until=s.cooldown_until,
                        bullet_cooldowns=s.bullet_cooldowns,
                        consecutive_failures=0,
                    )
                    result_state, status = advance(_cylinders, s)
                    if status == "ALL_COOLDOWN":
                        result_state = CylinderState(
                            cylinder=result_state.cylinder,
                            bullet=result_state.bullet,
                            state="CYLINDER_EXHAUSTED",
                            cooldown_until=result_state.cooldown_until,
                            bullet_cooldowns=result_state.bullet_cooldowns,
                            consecutive_failures=result_state.consecutive_failures,
                        )
                        result_state, _ = advance(_cylinders, result_state)
                    return result_state
                return s
            elif action == "cooldown":
                # 429 — rate-limit. Reset failure counter.
                s = CylinderState(
                    cylinder=s.cylinder,
                    bullet=s.bullet,
                    state=s.state,
                    cooldown_until=s.cooldown_until,
                    bullet_cooldowns=s.bullet_cooldowns,
                    consecutive_failures=0,
                )
                if s.cylinder < len(_cylinders):
                    cyl = _cylinders[s.cylinder]
                    cooldown_from_bullet = cyl.bullets[s.bullet].get("cooldown_seconds") if s.bullet >= 0 else None
                    cooldown_secs = (
                        cooldown_from_bullet
                        if cooldown_from_bullet is not None
                        else cyl.cooldown_seconds
                    )
                    mark_bullet_cooldown(s.bullet_cooldowns, s.bullet, cooldown_secs)
                result_state, status = advance(_cylinders, s)
                if status == "ALL_COOLDOWN":
                    result_state = CylinderState(
                        cylinder=result_state.cylinder,
                        bullet=result_state.bullet,
                        state="CYLINDER_EXHAUSTED",
                        cooldown_until=result_state.cooldown_until,
                        bullet_cooldowns=result_state.bullet_cooldowns,
                        consecutive_failures=result_state.consecutive_failures,
                    )
                    result_state, _ = advance(_cylinders, result_state)
                return result_state
            else:
                # Transient / unknown — do not rotate; retry in place
                return s

        new_state, acquired = _mutate(_make_transition)

        if acquired:
            cyl = _cylinders[new_state.cylinder] if new_state.cylinder < len(_cylinders) else None
            log_ctx = {
                "cylinder": new_state.cylinder,
                "bullet": new_state.bullet,
                "model": cyl.model if cyl else model,
                "provider": cyl.provider if cyl else provider,
                "trigger": str(status_code) if status_code else "",
                "state": new_state.state,
                "consecutive_failures": new_state.consecutive_failures,
            }
            if _action_for_log == "cooldown":
                log_event("cooldown_set", log_ctx)
            elif new_state.state == "CYLINDER_EXHAUSTED":
                log_event("cylinder_exhausted", log_ctx)
            elif new_state.state == "ALL_EXHAUSTED":
                log_event("all_exhausted", log_ctx)
                log_event("recovery_scheduled", {
                    "cylinder": 0,
                    "state": "ALL_EXHAUSTED",
                })
                try:
                    import yaml as _yaml
                    with open(REVOLVER_YAML, "r") as _fh:
                        _raw_yaml = _yaml.safe_load(_fh)
                    recovery_interval = float(
                        _raw_yaml.get("recovery_check_interval_seconds", 300.0)
                        if isinstance(_raw_yaml, dict) else 300.0
                    )
                except Exception:
                    recovery_interval = 300.0
                start_recovery_check(recovery_interval)

        if acquired:
            ctx.inject_message(
                f"[revolver] After {status_code} ({action}): "
                f"cylinder={new_state.cylinder} bullet={new_state.bullet} "
                f"state={new_state.state}",
                role="user",
            )
        else:
            ctx.inject_message(
                f"[revolver] {status_code} ({action}) but lock contention — "
                f"state unchanged; retry manually with /revolver next",
                role="user",
            )

    # -------------------------------------------------------------------------
    # /revolver next
    # -------------------------------------------------------------------------
    def _cmd_next() -> str:
        """Advance one bullet, or cylinder if bullets exhausted."""
        nonlocal _state
        if not _cylinders:
            return "[revolver] No cylinders configured"
        if not _state.cylinder < len(_cylinders):
            return "[revolver] ALL_EXHAUSTED — /revolver reset to recover"

        def _fn(s: CylinderState) -> CylinderState:
            result_state, status = advance(_cylinders, s)
            if status == "ALL_COOLDOWN":
                result_state = CylinderState(
                    cylinder=result_state.cylinder,
                    bullet=result_state.bullet,
                    state="CYLINDER_EXHAUSTED",
                    cooldown_until=result_state.cooldown_until,
                    bullet_cooldowns=result_state.bullet_cooldowns,
                    consecutive_failures=result_state.consecutive_failures,
                )
                result_state, _ = advance(_cylinders, result_state)
            return result_state

        new_state, acquired = _mutate(_fn)
        if not acquired:
            return "[revolver] Lock contention — try again"
        _state = new_state

        cyl = _cylinders[new_state.cylinder] if new_state.cylinder < len(_cylinders) else None
        log_ctx = {
            "cylinder": new_state.cylinder,
            "bullet": new_state.bullet,
            "model": cyl.model if cyl else "",
            "provider": cyl.provider if cyl else "",
            "state": new_state.state,
            "consecutive_failures": new_state.consecutive_failures,
        }
        if new_state.state == "ALL_EXHAUSTED":
            log_event("all_exhausted", log_ctx)
            log_event("recovery_scheduled", {"cylinder": 0, "state": "ALL_EXHAUSTED"})
            try:
                import yaml as _yaml
                with open(REVOLVER_YAML, "r") as _fh:
                    _raw_yaml = _yaml.safe_load(_fh)
                recovery_interval = float(
                    _raw_yaml.get("recovery_check_interval_seconds", 300.0)
                    if isinstance(_raw_yaml, dict) else 300.0
                )
            except Exception:
                recovery_interval = 300.0
            start_recovery_check(recovery_interval)
            return (
                f"[revolver] Advanced to ALL_EXHAUSTED — "
                f"no more cylinders; /revolver reset to recover"
            )
        elif status == "EXHAUSTED":
            log_event("cylinder_exhausted", log_ctx)
        else:
            log_event("bullet_advanced", log_ctx)
        cyl = _cylinders[new_state.cylinder]
        key = cyl.get_bullet_key(new_state.bullet)
        key_info = f" key={key[:8]}..." if key else " key=(none)"
        return (
            f"[revolver] next — cylinder={new_state.cylinder} "
            f"bullet={new_state.bullet}/{len(cyl.bullets)}{key_info} "
            f"state={new_state.state}"
        )

    ctx.register_command(
        "revolver-next",
        _cmd_next,
        description="Advance one bullet, or cylinder if current cylinder is exhausted",
    )

    # -------------------------------------------------------------------------
    # /revolver status
    # -------------------------------------------------------------------------
    def _cmd_status() -> str:
        """Show current cylinder index, bullet index, state."""
        if not _cylinders:
            return "[revolver] No cylinders configured"
        s = load_state()
        model, provider, api_key = get_active_delegation(_cylinders, s)
        key_info = f" api_key={api_key[:8]}..." if api_key else " api_key=(none)"
        cooldown_info = ""
        if s.cooldown_until > 0 and time.time() < s.cooldown_until:
            remaining = int(s.cooldown_until - time.time())
            cooldown_info = f" cooldown={remaining}s remaining"
        elif s.cooldown_until > 0:
            cooldown_info = " cooldown=expired"
        if model:
            return (
                f"[revolver] cylinder={s.cylinder} bullet={s.bullet} "
                f"state={s.state} -> {provider}/{model}{key_info}{cooldown_info}"
            )
        return (
            f"[revolver] state={s.state} — ALL_EXHAUSTED; "
            f"/revolver reset to recover{cooldown_info}"
        )

    ctx.register_command(
        "revolver-status",
        _cmd_status,
        description="Show current cylinder index, bullet index, and state",
    )

    # -------------------------------------------------------------------------
    # /revolver graph
    # -------------------------------------------------------------------------
    def _cmd_graph() -> str:
        """Print the full fallback chain with current position marked \u25cf."""
        if not _cylinders:
            return "[revolver] No cylinders configured"
        s = load_state()
        return "```\n" + format_graph(_cylinders, s) + "\n```"

    ctx.register_command(
        "revolver-graph",
        _cmd_graph,
        description="Print full fallback chain with current position marked by \u25cf",
    )

    # -------------------------------------------------------------------------
    # /revolver reset
    # -------------------------------------------------------------------------
    def _cmd_reset() -> str:
        """Reset to cylinder 0, bullet -1, state CYLINDER_ACTIVE."""
        nonlocal _state
        stop_recovery_thread()

        def _fn(s: CylinderState) -> CylinderState:
            new_state = CylinderState(**STATE_DEFAULTS)
            clear_bullet_cooldowns(new_state.bullet_cooldowns)
            return new_state

        new_state, acquired = _mutate(_fn)
        if not acquired:
            return "[revolver] Lock contention — try again"
        _state = new_state
        log_event("reset", {
            "cylinder": new_state.cylinder,
            "bullet": new_state.bullet,
            "state": new_state.state,
            "consecutive_failures": new_state.consecutive_failures,
        })
        return "[revolver] Reset — cylinder=0 bullet=-1 state=CYLINDER_ACTIVE"

    ctx.register_command(
        "revolver-reset",
        _cmd_reset,
        description="Reset revolver to cylinder 0, bullet -1, state CYLINDER_ACTIVE",
    )

    # -------------------------------------------------------------------------
    # /revolver log
    # -------------------------------------------------------------------------
    def _cmd_log() -> str:
        """Show the last 20 rotation events in a table."""
        entries = read_log(20)
        if not entries:
            return "[revolver] No events logged yet"

        header = f"{'TIME':<20} {'EVENT':<22} {'CYL':>3} {'BULL':>4}  PROVIDER"
        lines = [header, "-" * len(header)]
        for e in entries:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts", 0)))
            event = e.get("event", "?")
            cyl = e.get("cylinder", "")
            bullet = e.get("bullet", "")
            provider = e.get("provider", "")
            lines.append(f"{ts}  {event:<22} {str(cyl):>3} {str(bullet):>4}  {provider}")
        return "\n".join(lines)

    ctx.register_command(
        "revolver-log",
        _cmd_log,
        description="Show the last 20 rotation events",
    )

    # -------------------------------------------------------------------------
    # /revolver tool  — human-readable delegation snapshot
    # -------------------------------------------------------------------------
    def _cmd_tool() -> str:
        """Print the active delegation config in human-readable form."""
        if not _cylinders:
            return "[revolver] No cylinders configured"
        s = load_state()
        result = get_active_delegation_dict(_cylinders, s)
        if "error" in result:
            return f"[revolver] {result['error']}"
        return (
            f"[revolver] Active delegation:\n"
            f"  cylinder : {result['cylinder']}\n"
            f"  bullet   : {result['bullet']}\n"
            f"  state    : {result['state']}\n"
            f"  provider : {result['provider']}\n"
            f"  model    : {result['model']}"
        )

    ctx.register_command(
        "revolver-tool",
        _cmd_tool,
        description="Show the currently active delegation model and provider from revolver",
    )

    # -------------------------------------------------------------------------
    # Tool: get_active_delegation  — exposes active cylinder config to host agent
    # -------------------------------------------------------------------------
    def _tool_get_active_delegation() -> dict:
        """Return active delegation config as {model, provider, cylinder, bullet, state}."""
        if not _cylinders:
            return {"error": "No cylinders configured"}
        s = load_state()
        return get_active_delegation_dict(_cylinders, s)

    ctx.register_tool(
        name="get_active_delegation",
        handler=_tool_get_active_delegation,
        description="Return the currently active delegation model and provider from revolver",
    )

    # -------------------------------------------------------------------------
    # CRUD helpers — revolver.yaml manipulation
    # -------------------------------------------------------------------------

    def _parse_flag_args(args: List[str]) -> Tuple[dict, List[str]]:
        """Split args into --flag value pairs and positional args.
        Returns (kwargs_dict, positional_list).
        """
        kwargs = {}
        positional = []
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith("--"):
                key = a[2:]
                if i + 1 < len(args) and not args[i + 1].startswith("--"):
                    kwargs[key] = args[i + 1]
                    i += 2
                else:
                    kwargs[key] = True
                    i += 1
            else:
                positional.append(a)
                i += 1
        return kwargs, positional

    def _denormalize_bullet(b: dict) -> Any:
        """Return a bullet as a plain string if type=bearer and no cooldown, else dict."""
        key = b.get("key", "")
        btype = b.get("type", "bearer")
        cooldown = b.get("cooldown_seconds")
        if btype == "bearer" and cooldown is None:
            return key
        result: dict = {"key": key}
        if btype != "bearer":
            result["type"] = btype
        if cooldown is not None:
            result["cooldown_seconds"] = cooldown
        return result

    def _cylinder_to_raw(c: CylinderDef) -> dict:
        """Serialize a CylinderDef to a plain dict for YAML."""
        entry = {"delegation": dict(c.delegation)}
        entry["bullets"] = [_denormalize_bullet(b) for b in c.bullets]
        if c.cooldown_seconds != 60:
            entry["cooldown_seconds"] = c.cooldown_seconds
        if c.consecutive_failures_threshold != 2:
            entry["consecutive_failures_threshold"] = c.consecutive_failures_threshold
        if c.probe_url:
            entry["probe_url"] = c.probe_url
        return entry

    def _write_cylinders_yaml(cyl_list: List[CylinderDef]) -> str:
        """Write cylinders to revolver.yaml. Return error msg or empty string."""
        try:
            raw = {"cylinders": [_cylinder_to_raw(c) for c in cyl_list]}
            import yaml as _yaml
            with open(REVOLVER_YAML, "w") as fh:
                _yaml.dump(raw, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
            return ""
        except Exception as exc:
            logger.error("[revolver] Failed to write revolver.yaml (%s)", exc)
            return f"Error writing revolver.yaml: {exc}"

    def _reload_cylinders() -> str:
        """Reload _cylinders from disk. Return error msg or empty string."""
        nonlocal _cylinders
        try:
            _cylinders = load_revolver_yaml()
            logger.info("[revolver] Reloaded %d cylinder(s)", len(_cylinders))
            return ""
        except Exception as exc:
            return f"Failed to reload cylinders: {exc}"

    # -------------------------------------------------------------------------
    # /revolver cylinder — CRUD for cylinders
    # -------------------------------------------------------------------------
    def _cmd_cylinder(raw_args: str) -> str:
        """Manage cylinder definitions: list, add, edit, remove, move."""
        nonlocal _cylinders
        parts = raw_args.strip().split()
        subcmd = parts[0].lower() if parts else "list"

        if subcmd == "list":
            if not _cylinders:
                return "[revolver] No cylinders configured"
            lines = [f"[revolver] Cylinders ({len(_cylinders)}):"]
            for i, c in enumerate(_cylinders):
                bullets = len(c.bullets)
                lines.append(
                    f"  [{i}] {c.provider}/{c.model} "
                    f"({bullets} bullet{'s' if bullets != 1 else ''})"
                    f"{' ● active' if i == load_state().cylinder else ''}"
                )
            return "\n".join(lines)

        elif subcmd == "add":
            # parse: add <model> <provider> [--cooldown N] [--probe-url URL]
            kwargs, rest = _parse_flag_args(parts[1:])
            if len(rest) < 2:
                return "Usage: /revolver cylinder add <model> <provider> [--cooldown N] [--probe-url URL]"
            model, provider = rest[0], rest[1]
            new_cyl = CylinderDef(
                delegation={"model": model, "provider": provider},
                bullets=[],
                cooldown_seconds=int(kwargs.get("cooldown", 60)),
                probe_url=kwargs.get("probe_url"),
            )
            updated = list(_cylinders) + [new_cyl]
            err = _write_cylinders_yaml(updated)
            if err:
                return f"[revolver] {err}"
            msg = _reload_cylinders()
            if msg:
                return f"[revolver] Written but {msg}"
            return f"[revolver] Added cylinder [{len(updated)-1}] {provider}/{model}"

        elif subcmd == "edit":
            # edit <idx> [--model m] [--provider p] [--cooldown N] [--probe-url URL] [--threshold N]
            if not parts[1:]:
                return "Usage: /revolver cylinder edit <idx> [--model m] [--provider p] [--cooldown N] [--probe-url URL] [--threshold N]"
            try:
                idx = int(parts[1])
            except ValueError:
                return f"Invalid index: {parts[1]}"
            if idx < 0 or idx >= len(_cylinders):
                return f"Index {idx} out of range (0-{len(_cylinders)-1})"
            kwargs, _ = _parse_flag_args(parts[2:])
            if not kwargs:
                return "No edits specified. Use --model, --provider, --cooldown, --probe-url, --threshold"
            c = _cylinders[idx]
            delegation = dict(c.delegation)
            if "model" in kwargs:
                delegation["model"] = kwargs["model"]
            if "provider" in kwargs:
                delegation["provider"] = kwargs["provider"]
            updated = list(_cylinders)
            updated[idx] = CylinderDef(
                delegation=delegation,
                bullets=list(c.bullets),
                cooldown_seconds=int(kwargs.get("cooldown", c.cooldown_seconds)),
                consecutive_failures_threshold=int(
                    kwargs.get("threshold", c.consecutive_failures_threshold)
                ),
                probe_url=kwargs.get("probe_url", c.probe_url),
            )
            err = _write_cylinders_yaml(updated)
            if err:
                return f"[revolver] {err}"
            msg = _reload_cylinders()
            if msg:
                return f"[revolver] Written but {msg}"
            return f"[revolver] Edited cylinder [{idx}]"

        elif subcmd == "remove":
            if len(parts) < 2:
                return "Usage: /revolver cylinder remove <idx>"
            try:
                idx = int(parts[1])
            except ValueError:
                return f"Invalid index: {parts[1]}"
            if idx < 0 or idx >= len(_cylinders):
                return f"Index {idx} out of range (0-{len(_cylinders)-1})"
            removed = _cylinders[idx]
            updated = [c for i, c in enumerate(_cylinders) if i != idx]
            if not updated:
                return "[revolver] Cannot remove last cylinder (would leave chain empty)"
            err = _write_cylinders_yaml(updated)
            if err:
                return f"[revolver] {err}"
            # Reset state if current cylinder was removed
            s = load_state()
            new_cyl_idx = min(s.cylinder, len(updated) - 1) if updated else 0
            if s.cylinder != new_cyl_idx:
                def _reset_cyl(s_):
                    return CylinderState(**{**STATE_DEFAULTS, "cylinder": new_cyl_idx})
                _mutate(_reset_cyl)
            msg = _reload_cylinders()
            if msg:
                return f"[revolver] Written but {msg}"
            return f"[revolver] Removed cylinder [{idx}] {removed.provider}/{removed.model}"

        elif subcmd == "move":
            if len(parts) < 3:
                return "Usage: /revolver cylinder move <idx> <new-idx>"
            try:
                src, dst = int(parts[1]), int(parts[2])
            except ValueError:
                return "Indices must be integers"
            if src < 0 or src >= len(_cylinders) or dst < 0 or dst >= len(_cylinders):
                return f"Indices out of range (0-{len(_cylinders)-1})"
            updated = list(_cylinders)
            cyl = updated.pop(src)
            updated.insert(dst, cyl)
            err = _write_cylinders_yaml(updated)
            if err:
                return f"[revolver] {err}"
            msg = _reload_cylinders()
            if msg:
                return f"[revolver] Written but {msg}"
            return f"[revolver] Moved cylinder [{src}] → [{dst}]"

        return "Unknown subcommand. Try: list, add, edit, remove, move"

    ctx.register_command(
        "revolver-cylinder",
        _cmd_cylinder,
        description="Manage cylinders: list, add, edit, remove, move",
        args_hint="<list|add|edit|remove|move> [args]",
    )

    # -------------------------------------------------------------------------
    # /revolver bullet — CRUD for bullets within a cylinder
    # -------------------------------------------------------------------------
    def _cmd_bullet(raw_args: str) -> str:
        """Manage bullet definitions: list, add, edit, remove."""
        nonlocal _cylinders
        parts = raw_args.strip().split()
        if not parts:
            return "Usage: /revolver bullet <list|add|edit|remove> [args]"
        subcmd = parts[0].lower()

        if subcmd == "list":
            if len(parts) < 2:
                return "Usage: /revolver bullet list <cylinder-idx>"
            try:
                idx = int(parts[1])
            except ValueError:
                return f"Invalid cylinder index: {parts[1]}"
            if idx < 0 or idx >= len(_cylinders):
                return f"Cylinder index {idx} out of range (0-{len(_cylinders)-1})"
            c = _cylinders[idx]
            if not c.bullets:
                return f"[revolver] Cylinder [{idx}] has no bullets"
            lines = [f"[revolver] Cylinder [{idx}] {c.provider}/{c.model} — bullets:"]
            for bi, b in enumerate(c.bullets):
                key = b.get("key", "")
                key_preview = key[:12] + "..." if len(key) > 16 else key
                btype = b.get("type", "bearer")
                cooldown = b.get("cooldown_seconds")
                extra = f" type={btype}" if btype != "bearer" else ""
                extra += f" cooldown={cooldown}s" if cooldown else ""
                marker = " ●" if bi == load_state().bullet and idx == load_state().cylinder else ""
                lines.append(f"  [{bi}] {key_preview}{extra}{marker}")
            return "\n".join(lines)

        elif subcmd == "add":
            if len(parts) < 3:
                return "Usage: /revolver bullet add <cylinder-idx> <key> [--type bearer|x-api-key] [--cooldown N]"
            try:
                cyl_idx = int(parts[1])
            except ValueError:
                return f"Invalid cylinder index: {parts[1]}"
            if cyl_idx < 0 or cyl_idx >= len(_cylinders):
                return f"Cylinder index {cyl_idx} out of range (0-{len(_cylinders)-1})"
            kwargs, rest = _parse_flag_args(parts[2:])
            if not rest:
                return "Missing API key argument"
            key = rest[0]
            new_bullet: dict = {"key": key}
            btype = kwargs.get("type", "bearer")
            if btype != "bearer":
                new_bullet["type"] = btype
            if "cooldown" in kwargs:
                new_bullet["cooldown_seconds"] = int(kwargs["cooldown"])
            c = _cylinders[cyl_idx]
            updated = list(_cylinders)
            updated[cyl_idx] = CylinderDef(
                delegation=dict(c.delegation),
                bullets=list(c.bullets) + [new_bullet],
                cooldown_seconds=c.cooldown_seconds,
                consecutive_failures_threshold=c.consecutive_failures_threshold,
                probe_url=c.probe_url,
            )
            err = _write_cylinders_yaml(updated)
            if err:
                return f"[revolver] {err}"
            msg = _reload_cylinders()
            if msg:
                return f"[revolver] Written but {msg}"
            key_preview = key[:12] + "..." if len(key) > 16 else key
            return f"[revolver] Added bullet [{len(c.bullets)}] {key_preview} to cylinder [{cyl_idx}]"

        elif subcmd == "edit":
            if len(parts) < 3:
                return "Usage: /revolver bullet edit <cylinder-idx> <bullet-idx> [--key k] [--type t] [--cooldown N]"
            try:
                cyl_idx = int(parts[1])
                bul_idx = int(parts[2])
            except ValueError:
                return "Indices must be integers"
            if cyl_idx < 0 or cyl_idx >= len(_cylinders):
                return f"Cylinder index {cyl_idx} out of range"
            c = _cylinders[cyl_idx]
            if bul_idx < 0 or bul_idx >= len(c.bullets):
                return f"Bullet index {bul_idx} out of range (0-{len(c.bullets)-1})"
            kwargs, _ = _parse_flag_args(parts[3:])
            if not kwargs:
                return "No edits specified. Use --key, --type, --cooldown"
            old_bullet = c.bullets[bul_idx]
            new_bullet = dict(old_bullet)
            if "key" in kwargs:
                new_bullet["key"] = kwargs["key"]
            if "type" in kwargs:
                new_bullet["type"] = kwargs["type"]
            if "cooldown" in kwargs:
                if kwargs["cooldown"] == "none" or kwargs["cooldown"] == "":
                    new_bullet.pop("cooldown_seconds", None)
                else:
                    new_bullet["cooldown_seconds"] = int(kwargs["cooldown"])
            bullets = list(c.bullets)
            bullets[bul_idx] = new_bullet
            updated = list(_cylinders)
            updated[cyl_idx] = CylinderDef(
                delegation=dict(c.delegation),
                bullets=bullets,
                cooldown_seconds=c.cooldown_seconds,
                consecutive_failures_threshold=c.consecutive_failures_threshold,
                probe_url=c.probe_url,
            )
            err = _write_cylinders_yaml(updated)
            if err:
                return f"[revolver] {err}"
            msg = _reload_cylinders()
            if msg:
                return f"[revolver] Written but {msg}"
            return f"[revolver] Edited bullet [{bul_idx}] in cylinder [{cyl_idx}]"

        elif subcmd == "remove":
            if len(parts) < 3:
                return "Usage: /revolver bullet remove <cylinder-idx> <bullet-idx>"
            try:
                cyl_idx = int(parts[1])
                bul_idx = int(parts[2])
            except ValueError:
                return "Indices must be integers"
            if cyl_idx < 0 or cyl_idx >= len(_cylinders):
                return f"Cylinder index {cyl_idx} out of range"
            c = _cylinders[cyl_idx]
            if bul_idx < 0 or bul_idx >= len(c.bullets):
                return f"Bullet index {bul_idx} out of range (0-{len(c.bullets)-1})"
            bullets = [b for i, b in enumerate(c.bullets) if i != bul_idx]
            updated = list(_cylinders)
            updated[cyl_idx] = CylinderDef(
                delegation=dict(c.delegation),
                bullets=bullets,
                cooldown_seconds=c.cooldown_seconds,
                consecutive_failures_threshold=c.consecutive_failures_threshold,
                probe_url=c.probe_url,
            )
            err = _write_cylinders_yaml(updated)
            if err:
                return f"[revolver] {err}"
            msg = _reload_cylinders()
            if msg:
                return f"[revolver] Written but {msg}"
            return f"[revolver] Removed bullet [{bul_idx}] from cylinder [{cyl_idx}]"

        return "Unknown subcommand. Try: list, add, edit, remove"

    ctx.register_command(
        "revolver-bullet",
        _cmd_bullet,
        description="Manage bullets: list, add, edit, remove",
        args_hint="<list|add|edit|remove> [args]",
    )

    logger.info(
        "[revolver] Registered — hooks: on_session_start, api_request_error; "
        "commands: /revolver next, /revolver status, /revolver graph, /revolver reset, "
        "/revolver tool, /revolver log, /revolver cylinder, /revolver bullet"
    )

# __main__.py has the self-test (python3 -m revolver)
