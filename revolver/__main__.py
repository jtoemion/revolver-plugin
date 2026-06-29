"""
Revolver self-test runner.

Usage:
    python3 -m revolver              # (from ~/.hermes/plugins/)
    python3 ~/.hermes/plugins/revolver/__main__.py

Tests cylinder + bullets modules independently without a Hermes environment.
"""

if __name__ == "__main__":
    import sys
    import os

    # Ensure parent dir is on sys.path for package imports
    _parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _parent not in sys.path:
        sys.path.insert(0, _parent)

    # Import directly from the submodules for testing
    from revolver.bullets import (
        normalize_bullet,
        mark_bullet_cooldown,
        is_bullet_available,
        clear_bullet_cooldowns,
        classify_error,
    )
    from revolver.cylinder import (
        CylinderDef,
        CylinderState,
        STATE_FILE,
        advance,
        format_graph,
        get_active_delegation,
        load_revolver_yaml,
        load_state,
        save_state,
    )

    # Smoke-test: load yaml and state without a full Hermes environment
    print("=== revolver self-test ===")

    print("1. Loading revolver.yaml ...")
    try:
        cyls = load_revolver_yaml()
        print(f"   OK — {len(cyls)} cylinder(s) loaded")
        for i, c in enumerate(cyls):
            types = [c.get_bullet_type(k) for k in range(len(c.bullets))]
            print(f"      [{i}] {c.provider}/{c.model}  bullets={len(c.bullets)}  cooldown={c.cooldown_seconds}s")
            print(f"           types={types}")
    except Exception as e:
        print(f"   FAIL: {e}")
        sys.exit(1)

    print("2. State round-trip ...")
    s = CylinderState(cylinder=0, bullet=-1, state="CYLINDER_ACTIVE", cooldown_until=0.0)
    save_state(s)
    loaded = load_state()
    assert loaded.cylinder == s.cylinder, f"cylinder mismatch: {loaded.cylinder} != {s.cylinder}"
    assert loaded.bullet == s.bullet
    assert loaded.state == s.state
    print(f"   OK — state={loaded}")

    print("2b. normalize_bullet ...")
    n = normalize_bullet("sk-or-v1-aaa")
    assert n == {"key": "sk-or-v1-aaa", "type": "bearer", "cooldown_seconds": None, "label": None}
    n = normalize_bullet({"key": "sk-or-v1-bbb", "type": "x-api-key", "cooldown_seconds": 30})
    assert n == {"key": "sk-or-v1-bbb", "type": "x-api-key", "cooldown_seconds": 30, "label": None}
    n = normalize_bullet({"key": "mm-sk-xxx"})
    assert n == {"key": "mm-sk-xxx", "type": "bearer", "cooldown_seconds": None, "label": None}
    n = normalize_bullet({"key": "sk-or-v1-ccc", "type": "bearer", "cooldown_seconds": 15, "label": "openrouter-1"})
    assert n == {"key": "sk-or-v1-ccc", "type": "bearer", "cooldown_seconds": 15, "label": "openrouter-1"}
    n = normalize_bullet({"key": "sk-or-v1-ddd", "label": 123})
    assert n == {"key": "sk-or-v1-ddd", "type": "bearer", "cooldown_seconds": None, "label": None}
    try:
        normalize_bullet({"key": "x", "type": "unknown"})
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "unknown" in str(e)
    try:
        normalize_bullet({"key": ""})
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "non-empty" in str(e)
    try:
        normalize_bullet(123)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "string or dict" in str(e)
    print("   OK — all normalisations and validations correct")

    print("2c. get_bullet_type ...")
    cyl = CylinderDef(
        delegation={"model": "m1", "provider": "p1"},
        bullets=[
            {"key": "a", "type": "bearer", "cooldown_seconds": None, "label": None},
            {"key": "b", "type": "x-api-key", "cooldown_seconds": 10, "label": "bullet-b"},
        ],
        cooldown_seconds=60,
    )
    assert cyl.get_bullet_type(0) == "bearer"
    assert cyl.get_bullet_type(1) == "x-api-key"
    assert cyl.get_bullet_type(99) == "bearer"
    assert cyl.get_bullet_key(0) == "a"
    assert cyl.get_bullet_key(1) == "b"
    print("   OK")

    print("3. Error classification ...")
    checks = [(401, "advance"), (429, "cooldown"), (408, "transient"), (502, "transient"),
              (503, "transient"), (500, "unknown"), (None, "unknown")]
    for code, expected in checks:
        result = classify_error(code)
        assert result == expected, f"classify_error({code}) = {result}, expected {expected}"
    print(f"   OK — all {len(checks)} classifications correct")

    print("4. advance wraps infinitely (no exhaustion tracking) ...")
    cyls_wrap = [
        CylinderDef(delegation={"model": "m1", "provider": "p1"},
                    bullets=[{"key": "a", "type": "bearer", "cooldown_seconds": None},
                             {"key": "b", "type": "bearer", "cooldown_seconds": None}],
                    cooldown_seconds=60),
    ]
    s = CylinderState(cylinder=0, bullet=-1, state="CYLINDER_ACTIVE")
    expected = [0, 1, 0, 1, 0, 1, 0, 1]
    for i, exp_bullet in enumerate(expected):
        s, status = advance(cyls_wrap, s)
        ok = s.bullet == exp_bullet and status == "ADVANCED" and s.state == "CYLINDER_ACTIVE"
        print(f"   [{i}] bullet {s.bullet} (expected {exp_bullet}) — {'OK' if ok else 'FAIL'}")
        if not ok:
            sys.exit(1)
    print("   OK — wraps infinitely without exhaustion")

    print("5. Graph rendering ...")
    cyls_test = [
        CylinderDef(delegation={"model": "m1", "provider": "p1"},
                    bullets=[{"key": "a", "type": "bearer", "cooldown_seconds": None, "label": "A"},
                             {"key": "b", "type": "bearer", "cooldown_seconds": None, "label": "B"}],
                    cooldown_seconds=60),
        CylinderDef(delegation={"model": "m2", "provider": "p2"}, bullets=[], cooldown_seconds=30),
        CylinderDef(delegation={"model": "m3", "provider": "p3"},
                    bullets=[{"key": "c", "type": "bearer", "cooldown_seconds": None, "label": "C"}],
                    cooldown_seconds=120),
    ]
    s = CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE")
    graph = format_graph(cyls_test, s)
    assert "\u25cf" in graph, "graph must contain \u25cf marker"
    assert "Cylinder 0" in graph
    assert "bullets:" in graph
    print(f"   OK\n{graph}")

    print("6. Active delegation lookup ...")
    model, provider, api_key = get_active_delegation(
        cyls_test, CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE"))
    assert model == "m1" and provider == "p1" and api_key == "a", f"got {model}/{provider}/{api_key}"
    model, provider, api_key = get_active_delegation(
        cyls_test, CylinderState(cylinder=1, bullet=-1, state="CYLINDER_ACTIVE"))
    assert model == "m2" and provider == "p2" and api_key is None, f"got {model}/{provider}/{api_key}"
    model, provider, api_key = get_active_delegation(
        cyls_test, CylinderState(cylinder=99, bullet=0, state="ALL_EXHAUSTED"))
    assert model is None, f"ALL_EXHAUSTED must return None, got {model}"
    print("   OK")

    print("7. Bullet cooldown helpers ...")
    state = CylinderState()
    assert is_bullet_available(state.bullet_cooldowns, 0), "bullet 0 available initially"
    assert is_bullet_available(state.bullet_cooldowns, 1), "bullet 1 available initially"
    mark_bullet_cooldown(state.bullet_cooldowns, 0, 9999.0)
    assert not is_bullet_available(state.bullet_cooldowns, 0), "bullet 0 unavailable after cooldown"
    assert is_bullet_available(state.bullet_cooldowns, 1), "bullet 1 still available"
    clear_bullet_cooldowns(state.bullet_cooldowns)
    assert is_bullet_available(state.bullet_cooldowns, 0), "bullet 0 available after clear"
    assert is_bullet_available(state.bullet_cooldowns, 1), "bullet 1 available after clear"
    print("   OK")

    print("8. advance finds available bullet when one is in cooldown ...")
    cyls_2 = [
        CylinderDef(delegation={"model": "m1", "provider": "p1"},
                    bullets=[{"key": "a", "type": "bearer", "cooldown_seconds": None},
                             {"key": "b", "type": "bearer", "cooldown_seconds": None}],
                    cooldown_seconds=60),
    ]
    s = CylinderState(cylinder=0, bullet=-1, state="CYLINDER_ACTIVE")
    s, status = advance(cyls_2, s)
    assert s.bullet == 0 and status == "ADVANCED"
    mark_bullet_cooldown(s.bullet_cooldowns, 1, 9999.0)
    s, status = advance(cyls_2, s)
    assert s.bullet == 0 and status == "ADVANCED", \
        f"expected ADVANCED with bullet=0, got bullet={s.bullet} status={status}"
    print("   OK — bullet 1 in cooldown: advance finds bullet 0 (ADVANCED)")

    print("9. advance returns ALL_COOLDOWN when all bullets in cooldown ...")
    s = CylinderState(cylinder=0, bullet=-1, state="CYLINDER_ACTIVE")
    mark_bullet_cooldown(s.bullet_cooldowns, 0, 9999.0)
    mark_bullet_cooldown(s.bullet_cooldowns, 1, 9999.0)
    s, status = advance(cyls_2, s)
    assert status == "ALL_COOLDOWN", f"expected ALL_COOLDOWN, got status={status}"
    print("   OK")

    print("10. 429 cooldown triggers per-bullet cooldown + advance ...")
    cyls_429 = [
        CylinderDef(delegation={"model": "m1", "provider": "p1"},
                    bullets=[{"key": "a", "type": "bearer", "cooldown_seconds": 30},
                             {"key": "b", "type": "bearer", "cooldown_seconds": None}],
                    cooldown_seconds=60),
    ]
    s = CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE")
    cooldown_from_bullet = cyls_429[0].bullets[0].get("cooldown_seconds")
    assert cooldown_from_bullet == 30
    mark_bullet_cooldown(s.bullet_cooldowns, 0, cooldown_from_bullet)
    s, status = advance(cyls_429, s)
    assert s.bullet == 1 and status == "ADVANCED", \
        f"expected bullet=1 ADVANCED, got bullet={s.bullet} status={status}"
    assert "0" in s.bullet_cooldowns and not is_bullet_available(s.bullet_cooldowns, 0)
    print("   OK — 429 sets cooldown on bullet 0 (30s) and advances to bullet 1")

    print("11. 401 advances immediately without setting cooldown ...")
    s = CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE")
    s, status = advance(cyls_429, s)
    assert s.bullet == 1 and status == "ADVANCED"
    assert "0" not in s.bullet_cooldowns
    print("   OK — 401 advances without cooldown")

    print("12. Graph renders cooldown indicators ...")
    s = CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE")
    mark_bullet_cooldown(s.bullet_cooldowns, 1, 9999.0)
    graph = format_graph(cyls_429, s)
    assert "\u25cf" in graph and "\u2299" in graph and "\u25cb" not in graph, \
        f"graph should show \u25cf and \u2299, got:\n{graph}"
    assert "bearer \u25cf" in graph and "bearer \u2299" in graph, \
        f"graph should show bearer \u25cf and bearer \u2299, got:\n{graph}"
    print(f"   OK\n{graph}")
    s2 = CylinderState(cylinder=0, bullet=0, state="CYLINDER_EXHAUSTED")
    graph2 = format_graph(cyls_429, s2)
    assert "\u25cb" in graph2 and "\u2299" not in graph2, \
        f"exhausted cylinder should show \u25cb only (no \u2299), got:\n{graph2}"
    print(f"   exhausted OK\n{graph2}")

    # Clean up test state file
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    print("\n=== all tests passed ===")
    sys.exit(0)
