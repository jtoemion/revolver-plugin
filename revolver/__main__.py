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
    import tempfile
    from pathlib import Path

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Ensure parent dir is on sys.path for package imports
    _parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _parent not in sys.path:
        sys.path.insert(0, _parent)

    import revolver.bullets as bullets_mod
    import revolver.cylinder as cylinder_mod

    _tmp = tempfile.TemporaryDirectory(prefix="revolver-self-test-")
    _hermes_home = Path(_tmp.name) / ".hermes"
    _hermes_home.mkdir(parents=True, exist_ok=True)
    bullets_mod.HERMES_HOME = _hermes_home
    bullets_mod.EVENT_LOG_FILE = _hermes_home / ".revolver_events.log"
    cylinder_mod.HERMES_HOME = _hermes_home
    cylinder_mod.REVOLVER_YAML = _hermes_home / "revolver.yaml"
    cylinder_mod.STATE_FILE = _hermes_home / ".revolver_state.json"
    cylinder_mod.LOCK_FILE = _hermes_home / ".revolver.lock"
    cylinder_mod.REVOLVER_YAML.write_text(
        "cylinders:\n"
        "  - delegation:\n"
        "      model: m1\n"
        "      provider: p1\n"
        "    bullets:\n"
        "      - key-a\n"
        "      - key: key-b\n"
        "        type: x-api-key\n"
        "        cooldown_seconds: 30\n"
        "error_policy:\n"
        "  advance: [401]\n"
        "  cooldown: [429, 529]\n"
        "  transient: [408, 502, 503]\n",
        encoding="utf-8",
    )

    # Import directly from the submodules for testing
    from revolver.bullets import (
        normalize_bullet,
        normalize_error_policy,
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
        doctor_revolver_config,
        format_graph,
        get_active_delegation,
        get_active_delegation_dict,
        get_health_snapshot,
        load_error_policy,
        load_revolver_yaml,
        load_state,
        resolve_active_delegation,
        save_state,
    )
    from revolver import register

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
    assert n == {"key": "sk-or-v1-bbb", "type": "x-api-key", "cooldown_seconds": 30.0, "label": None}
    n = normalize_bullet({"key": "mm-sk-xxx"})
    assert n == {"key": "mm-sk-xxx", "type": "bearer", "cooldown_seconds": None, "label": None}
    n = normalize_bullet({"key": "sk-or-v1-ccc", "type": "bearer", "cooldown_seconds": 15, "label": "openrouter-1"})
    assert n == {"key": "sk-or-v1-ccc", "type": "bearer", "cooldown_seconds": 15.0, "label": "openrouter-1"}
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
        normalize_bullet("   ")
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "non-empty" in str(e)
    try:
        normalize_bullet({"key": "x", "cooldown_seconds": -1})
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert ">= 0" in str(e)
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

    print("3b. Configurable error policy ...")
    policy = load_error_policy()
    assert classify_error(529, policy) == "cooldown"
    assert classify_error(500, policy) == "unknown"
    merged = normalize_error_policy({"advance": [401, "403"]})
    assert classify_error(403, merged) == "advance"
    try:
        normalize_error_policy({"advance": [401], "cooldown": [401]})
        assert False, "duplicate policy status should fail"
    except ValueError as e:
        assert "appears in both" in str(e)
    print("   OK - custom policy loaded and validated")

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

    print("6b. Active delegation resolver returns an apply contract ...")
    contract = get_active_delegation_dict(
        cyls_test, CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE"))
    assert contract["active"] is True
    assert contract["apply"] == {"model": "m1", "provider": "p1"}
    assert contract["auth"]["type"] == "bearer"
    assert contract["auth"]["key_configured"] is True
    assert contract["auth"]["key"] == "configured (1 chars)"
    secret_contract = resolve_active_delegation(
        cyls_test,
        CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE"),
        include_secret=True,
    )
    assert secret_contract["auth"]["key"] == "a"
    exhausted_contract = get_active_delegation_dict(
        cyls_test, CylinderState(cylinder=99, bullet=0, state="ALL_EXHAUSTED"))
    assert exhausted_contract["active"] is False
    assert exhausted_contract["apply"] is None
    print("   OK - resolver gives hosts direct provider/model fields")

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

    print("7b. Health snapshot is redacted and structured ...")
    health_state = CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE")
    mark_bullet_cooldown(health_state.bullet_cooldowns, 1, 30)
    health = get_health_snapshot(cyls_test, health_state)
    assert health["ok"] is True
    assert health["active_delegation"]["apply"] == {"model": "m1", "provider": "p1"}
    assert health["active_delegation"]["auth"]["key"] == "configured (1 chars)"
    assert health["cooldowns"][0]["bullet"] == 1
    print("   OK - health includes apply contract and cooldowns")

    print("7c. Doctor validates local setup ...")
    doctor = doctor_revolver_config()
    assert doctor["ok"] is True, doctor
    assert doctor["cylinders"] == 1
    assert doctor["bullets"] == 2
    assert doctor["policy"]["cooldown"] == [429, 529]
    print("   OK - doctor reports config, counts, and policy")

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

    print("9b. exhausted cylinder advances to the next usable cylinder ...")
    cyls_chain = [
        CylinderDef(delegation={"model": "m1", "provider": "p1"},
                    bullets=[{"key": "a", "type": "bearer", "cooldown_seconds": None}],
                    cooldown_seconds=60),
        CylinderDef(delegation={"model": "empty", "provider": "p-empty"},
                    bullets=[],
                    cooldown_seconds=60),
        CylinderDef(delegation={"model": "m2", "provider": "p2"},
                    bullets=[{"key": "b", "type": "bearer", "cooldown_seconds": None}],
                    cooldown_seconds=60),
    ]
    s = CylinderState(cylinder=0, bullet=0, state="CYLINDER_EXHAUSTED")
    s, status = advance(cyls_chain, s)
    assert status == "EXHAUSTED", f"expected EXHAUSTED, got {status}"
    assert s.cylinder == 2 and s.bullet == 0 and s.state == "CYLINDER_ACTIVE", \
        f"expected cylinder 2 bullet 0 active, got {s}"
    assert s.bullet_cooldowns == {}, "new cylinder must not inherit previous bullet cooldowns"
    print("   OK - skipped empty cylinder and selected next usable bullet")

    print("9c. exhausted last cylinder becomes ALL_EXHAUSTED ...")
    s = CylinderState(cylinder=2, bullet=0, state="CYLINDER_EXHAUSTED")
    s, status = advance(cyls_chain, s)
    assert status == "ALL_EXHAUSTED", f"expected ALL_EXHAUSTED, got {status}"
    assert s.state == "ALL_EXHAUSTED" and s.cylinder == len(cyls_chain), \
        f"expected terminal exhausted state, got {s}"
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

    print("13. Hook injection tells agents which delegation config to use ...")

    class FakeCtx:
        def __init__(self):
            self.hooks = {}
            self.messages = []
            self.tools = {}

        def on(self, name):
            def _decorator(fn):
                self.hooks[name] = fn
                return fn
            return _decorator

        def inject_message(self, message, role="user"):
            self.messages.append({"message": message, "role": role})

        def register_command(self, *args, **kwargs):
            return None

        def register_tool(self, name, handler, description=""):
            self.tools[name] = {"handler": handler, "description": description}

    # 13a: intra-cylinder 429 must NOT inject (same provider/model, just a new bullet)
    save_state(CylinderState(cylinder=0, bullet=0, state="CYLINDER_ACTIVE"))
    fake_ctx = FakeCtx()
    register(fake_ctx)
    before_13a = len(fake_ctx.messages)
    fake_ctx.hooks["api_request_error"](status_code=429, model="m1", provider="p1")
    assert len(fake_ctx.messages) == before_13a, (
        f"intra-cylinder 429 must not inject, got {len(fake_ctx.messages) - before_13a} messages"
    )
    print("   OK - intra-cylinder 429: no inject (prevents feedback loop)")

    resolved = fake_ctx.tools["resolve_delegation"]["handler"]()
    assert resolved["apply"] == {"model": "m1", "provider": "p1"}, resolved
    assert "key-a" not in str(resolved), resolved
    health = fake_ctx.tools["get_revolver_health"]["handler"]()
    assert health["active_delegation"]["apply"] == {"model": "m1", "provider": "p1"}, health
    doctor = fake_ctx.tools["doctor_revolver"]["handler"]()
    assert doctor["ok"] is True, doctor
    print("   OK - tools return correct routing contract without leaking keys")

    # 13b: ALL_EXHAUSTED transition MUST inject exactly once (provider gone)
    # CYLINDER_EXHAUSTED + threshold met → advance() returns ALL_EXHAUSTED → inject fires.
    save_state(CylinderState(cylinder=0, bullet=0, state="CYLINDER_EXHAUSTED",
                              consecutive_failures=1))
    fake_ctx2 = FakeCtx()
    register(fake_ctx2)
    before_13b = len(fake_ctx2.messages)
    fake_ctx2.hooks["api_request_error"](status_code=401, model="m1", provider="p1")
    assert len(fake_ctx2.messages) > before_13b, "ALL_EXHAUSTED transition must inject"
    injected = fake_ctx2.messages[-1]["message"]
    assert "exhausted" in injected.lower() or "provider" in injected.lower(), injected
    assert "key-a" not in injected and "key-b" not in injected, injected
    print("   OK - ALL_EXHAUSTED transition injects exactly once without leaking keys")

    print("14. ALL_EXHAUSTED loop-break: repeated 429s must not re-inject ...")
    save_state(CylinderState(cylinder=99, bullet=-1, state="ALL_EXHAUSTED"))
    fake_ctx2 = FakeCtx()
    register(fake_ctx2)
    before = len(fake_ctx2.messages)
    fake_ctx2.hooks["api_request_error"](status_code=429, model="x", provider="y")
    fake_ctx2.hooks["api_request_error"](status_code=429, model="x", provider="y")
    fake_ctx2.hooks["api_request_error"](status_code=429, model="x", provider="y")
    assert len(fake_ctx2.messages) == before, (
        f"ALL_EXHAUSTED should suppress inject, got {len(fake_ctx2.messages) - before} extra messages"
    )
    print("   OK - no inject when already ALL_EXHAUSTED")

    # Clean up test state file
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    _tmp.cleanup()

    print("\n=== all tests passed ===")
    sys.exit(0)
