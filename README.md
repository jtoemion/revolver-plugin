# 🔫 Revolver — Never Miss Your Shot

> A reliability layer for Hermes agent delegation. Load your providers. Pull the trigger. Keep firing.

When a delegated model call fails, most setups die quietly. Revolver doesn't. It rotates through your API keys and fallback providers automatically, tells your agent exactly which one to use next, and self-heals when everything comes back online.

```
Cylinder 0: openrouter / poolside/laguna-m.1:free  ● bullet 1/2 active
  bullets: bearer ●, x-api-key ⊙ [47s]
Cylinder 1: opencode-zen / deepseek-v4-flash-free  bullet -1/3 pending
  bullets: bearer, bearer, bearer
Cylinder 2: custom:nara / mimo-v2.5-free           bullet -1/2 pending
  bullets: bearer, bearer
Cylinder 3: minimax / MiniMax-M2.7                 bullet -1/1 pending
  bullets: x-api-key
```

Six cylinders, twelve bullets, one command to rule them all.

---

## Table of Contents

- [How it works](#how-it-works)
- [Core concepts](#core-concepts)
- [State machine](#state-machine)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Cylinders](#cylinders)
  - [Bullets](#bullets)
  - [Error policy](#error-policy)
  - [Custom providers](#custom-providers)
- [Commands](#commands)
- [Tools](#tools)
- [Hooks](#hooks)
- [Error handling](#error-handling)
- [Auto-recovery](#auto-recovery)
- [Dual-process safety](#dual-process-safety)
- [Observability](#observability)
- [Self-test & CI](#self-test--ci)
- [Persistence](#persistence)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## How it works

Revolver sits silently in the background and listens for `api_request_error` events from Hermes. When one fires, it classifies the error and acts:

- **401?** That key is dead. Advance to the next bullet, no questions asked.
- **429?** That key is tired. Cool it down and move on.
- **502/503/408?** Transient noise. Stay put, let Hermes retry.
- **Everything else?** Also transient. Don't overreact.

After every rotation, it injects a message directly into your agent session saying *"use this provider, this model, this key auth type — go."* No silent failures. No stale delegation targets. Just clear, enforceable routing.

---

## Core concepts

| Term | What it is |
|------|-----------|
| **Cylinder** | One delegation target: a provider + model pair with one or more API keys. Think of it as a gun chamber loaded with bullets. |
| **Bullet** | A single API key inside a cylinder. Each bullet has an auth type, an optional cooldown, and an optional label. |
| **State** | The plugin's current position: which cylinder, which bullet, overall health (`CYLINDER_ACTIVE`, `ALL_COOLDOWN`, `ALL_EXHAUSTED`), and per-bullet cooldown timestamps. Persisted to disk. |
| **Rotation** | Moving to the next available bullet (skipping cooled-down ones). When all bullets in a cylinder are gone, the gun advances to the next chamber. |
| **apply contract** | The `apply.provider` / `apply.model` fields returned by `resolve_delegation` — the direct instruction your host agent must follow for its next delegated call. |

---

## State machine

```
                    ┌──────────────┐
                    │  INSTALLED   │
                    │ (cyl 0, b -1)│
                    └──────┬───────┘
                           │ first use or /revolver reset
                           ▼
                    ┌──────────────┐
              ┌────▶│CYLINDER_ACTIVE│◀───────────────┐
              │     └──────┬───────┘                 │
              │            │                         │
              │      ┌─────┴──────┐                  │
              │      │            │                   │
              │  401 (advance)  429 (cooldown)        │
              │      │            │                   │
              │      ▼            ▼                   │
              │  next bullet   cooldown set           │
              │      │         + advance              │
              │      │            │                   │
              │      ▼            ▼                   │
              │   all bullets exhausted?              │
              │             │                         │
              │             ▼                         │
              │      ┌──────────────┐                 │
              │      │  EXHAUSTED   │                 │
              │      │  (this cyl)  │                 │
              │      └──────┬───────┘                 │
              │             │ next cylinder available? │
              │             ▼                         │
              │      yes ──→ repeat from top          │
              │      no  ──→                          │
              │             ▼                         │
              │      ┌──────────────┐                 │
              │      │ALL_EXHAUSTED │─── recovery ───▶│
              │      │ (auto-probe) │    succeeds      │
              │      └──────────────┘                  │
              │                                        │
              └────────── /revolver reset ─────────────┘
```

---

## Installation

### Prerequisites

- Hermes Agent (any recent 2.x+ version)
- Python 3.9+
- `PyYAML` — usually bundled with Hermes; if not: `pip install pyyaml`

### Recommended: `hermes plugins install`

```bash
hermes plugins install jtoemion/revolver-plugin --enable
```

### Manual

```bash
git clone https://github.com/jtoemion/revolver-plugin.git
ln -sf "$(pwd)/revolver-plugin" ~/.hermes/plugins/revolver
hermes plugins enable revolver
```

### pip (for CI, testing, or direct use)

```bash
pip install -e ".[dev]"   # dev extras include pytest
revolver-selftest          # runs the full self-test suite
```

### Verify

```bash
hermes plugins list | grep revolver
# → revolver  enabled  1.0.0  Reliability layer …

python -m revolver
# → === revolver self-test ===
# → ...
# → === all tests passed ===
```

---

## Configuration

Create `~/.hermes/revolver.yaml`. All runtime CRUD commands read and write this file live.

### Cylinders

A minimal single-cylinder config:

```yaml
cylinders:
  - delegation:
      model: my-model
      provider: my-provider
    bullets:
      - sk-my-api-key
```

Full reference with all options:

```yaml
cylinders:
  # ── Cylinder 0: primary ──────────────────────────────────────────────────
  - delegation:
      model: poolside/laguna-m.1:free
      provider: openrouter
    bullets:
      - sk-or-...key1                          # plain string → bearer auth
      - key: sk-or-...key2                     # dict for explicit config
        type: x-api-key
        cooldown_seconds: 30                   # per-bullet cooldown override
        label: my-openrouter-backup
    cooldown_seconds: 60                       # default cooldown on 429
    consecutive_failures_threshold: 2          # 401s before advancing
    probe_url: https://openrouter.ai/api/v1/auth/key

  # ── Cylinder 1: fallback ─────────────────────────────────────────────────
  - delegation:
      model: deepseek-v4-flash-free
      provider: opencode-zen
    bullets:
      - sk-zen-...key1
      - sk-zen-...key2
      - sk-zen-...key3

  # ── Cylinder 2: custom provider ──────────────────────────────────────────
  - delegation:
      model: mimo-v2.5-free
      provider: custom:nara
    bullets:
      - sk-nry-...key1
```

#### Cylinder fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `delegation.model` | **yes** | — | Model identifier (e.g. `poolside/laguna-m.1:free`) |
| `delegation.provider` | **yes** | — | Provider name (e.g. `openrouter`, `custom:nara`) |
| `bullets` | **yes** | — | API key entries — strings or dicts |
| `cooldown_seconds` | no | `60` | Default 429 cooldown for bullets without a per-bullet override |
| `consecutive_failures_threshold` | no | `2` | 401s in a row before the plugin advances instead of retrying |
| `probe_url` | no | `None` | HEAD health-check URL — used before exhausting a cylinder and by auto-recovery |

### Bullets

Plain string (fastest):

```yaml
bullets:
  - sk-or-...aaaa
  - sk-or-...bbbb
```

Dict (when you need auth type, per-bullet cooldown, or a label):

```yaml
bullets:
  - key: sk-or-...cccc
    type: x-api-key
    cooldown_seconds: 30
    label: backup-router-key
```

#### Bullet fields

| Field | Required | Default | Description |
|------|----------|---------|-------------|
| `key` | **yes** | — | The API key (non-empty string) |
| `type` | no | `bearer` | One of `bearer`, `x-api-key`, `custom` |
| `cooldown_seconds` | no | cylinder's `cooldown_seconds` | Per-bullet 429 cooldown override (≥ 0) |
| `label` | no | `None` | Human-readable label shown in `/revolver graph` |

### Error policy

By default Revolver classifies errors as:

| Code | Action |
|------|--------|
| 401 | `advance` — rotate immediately |
| 429 | `cooldown` — cool the bullet, then rotate |
| 408, 502, 503 | `transient` — stay put, retry in place |
| anything else | `unknown` — treated as transient |

You can override this in `revolver.yaml` at the top level:

```yaml
error_policy:
  advance:   [401, 403]          # add 403 to immediate-rotate list
  cooldown:  [429, 529]          # add custom 529 rate-limit code
  transient: [408, 500, 502, 503]

cylinders:
  - ...
```

Rules:
- Each status code can appear in **at most one** list — duplicates are rejected at load time.
- Any code not listed in any list is treated as `transient`.
- Partial overrides are merged with the defaults — you only need to specify the lists you want to change.

### Custom providers

For any provider not built into Hermes (e.g. a custom OpenAI-compatible endpoint):

**1. Define it in `~/.hermes/config.yaml`:**

```yaml
custom_providers:
  - name: nara
    base_url: https://router.example.com/v1
    key_env: NARA_API_KEY
    api_mode: chat_completions
```

**2. Set the key in `~/.hermes/.env`:**

```bash
echo 'NARA_API_KEY=sk-nry-...your-key' >> ~/.hermes/.env
```

**3. Reference it in `revolver.yaml` as `custom:<name>`:**

```yaml
- delegation:
    model: mimo-v2.5-free
    provider: custom:nara
```

---

## Commands

All slash commands are available in any live Hermes session.

### `/revolver status`

Current position and active delegation target.

```
[revolver] cylinder=0 bullet=1 state=CYLINDER_ACTIVE -> openrouter/poolside/laguna-m.1:free
           api_key=configured (64 chars) cooldown=none
```

### `/revolver graph`

Full ASCII fallback chain with current position (●), cooled bullets (⊙), and exhausted bullets (○).

```
Cylinder 0: openrouter / poolside/laguna-m.1:free  ● bullet 1/2 active  cf=1
  bullets: bearer [primary] ●, x-api-key [backup] ⊙ [47s]
Cylinder 1: opencode-zen / deepseek-v4-flash-free  bullet -1/3 pending
  bullets: bearer, bearer, bearer
```

### `/revolver next`

Manually advance one bullet (or cylinder). Good for rotating after a credential fix without waiting for an actual error.

```
[revolver] next — cylinder=0 bullet=1/2 key=configured (64 chars) state=CYLINDER_ACTIVE
```

### `/revolver reset`

Pull the trigger on a fresh chamber. Resets to cylinder 0, bullet -1, stops any recovery thread, clears all cooldowns.

```
[revolver] Reset — cylinder=0 bullet=-1 state=CYLINDER_ACTIVE
```

### `/revolver health`

Active routing, per-bullet cooldowns, and recent event counts — everything you need to know at a glance.

```
[revolver] Health
  ok       : True
  state    : CYLINDER_ACTIVE
  cylinders: 4
  apply    : provider=openrouter model=poolside/laguna-m.1:free
  cooldown : bullet=0 remaining=47s auth=x-api-key
  events   : bullet_advanced=3, cooldown_set=1
```

### `/revolver doctor`

Validates your local setup without touching anything or exposing any secrets. Catches missing config files, empty cylinders, unsafe file permissions, non-HTTP probe URLs, and policy conflicts.

```
[revolver] Doctor
  ok       : True
  config   : /home/user/.hermes/revolver.yaml
  cylinders: 4
  bullets  : 9
  warning  : Cylinder 1 has no bullets and will be skipped
  findings : 1 warning
```

### `/revolver log`

Last 20 rotation events as a table.

```
TIME                 EVENT                CYL  BULL  PROVIDER
─────────────────────────────────────────────────────────────
2026-06-29 18:22:21  bullet_advanced        0    1   openrouter
2026-06-29 18:23:45  cooldown_set           0    1   openrouter
2026-06-29 18:25:10  cylinder_exhausted     0    —   openrouter
2026-06-29 18:25:10  bullet_advanced        1    0   opencode-zen
```

### `/revolver tool`

Human-readable version of the routing contract (same data as `resolve_delegation`).

```
[revolver] Active delegation:
  cylinder : 0
  bullet   : 1
  state    : CYLINDER_ACTIVE
  provider : openrouter
  model    : poolside/laguna-m.1:free
  apply    : provider=openrouter model=poolside/laguna-m.1:free
```

### `/revolver cylinder`

Live CRUD for cylinder definitions (writes `revolver.yaml` on the fly).

```bash
/revolver cylinder list
/revolver cylinder add <model> <provider> [--cooldown N] [--probe-url URL]
/revolver cylinder edit <idx> [--model m] [--provider p] [--cooldown N] [--threshold N]
/revolver cylinder remove <idx>
/revolver cylinder move <src-idx> <dst-idx>
```

```
/revolver cylinder add deepseek-v4-flash-free opencode-zen --cooldown 60
→ [revolver] Added cylinder [3] opencode-zen/deepseek-v4-flash-free

/revolver cylinder move 3 1
→ [revolver] Moved cylinder [3] → [1]
```

### `/revolver bullet`

Live CRUD for bullets within a cylinder.

```bash
/revolver bullet list <cylinder-idx>
/revolver bullet add <cylinder-idx> <key> [--type bearer|x-api-key] [--cooldown N]
/revolver bullet edit <cylinder-idx> <bullet-idx> [--key k] [--type t] [--cooldown N]
/revolver bullet remove <cylinder-idx> <bullet-idx>
```

```
/revolver bullet add 2 sk-my-new-key --type x-api-key --cooldown 30
→ [revolver] Added bullet [2] to cylinder [2]
```

---

## Tools

The plugin exposes four tools directly to the Hermes agent (not the user). Integrations should prefer these over parsing chat messages.

### `resolve_delegation`

The canonical routing contract. Returns the exact `provider` and `model` the host must apply to the next delegated request. This is the control-plane API.

```json
{
  "active": true,
  "model": "poolside/laguna-m.1:free",
  "provider": "openrouter",
  "cylinder": 0,
  "bullet": 1,
  "state": "CYLINDER_ACTIVE",
  "apply": {
    "provider": "openrouter",
    "model": "poolside/laguna-m.1:free"
  },
  "auth": {
    "type": "bearer",
    "key_configured": true,
    "key": "configured (64 chars)"
  }
}
```

When `ALL_EXHAUSTED`, `active` is `false` and `apply` is `null`.

### `get_active_delegation`

Back-compatible alias for `resolve_delegation`. Returns the same redacted routing contract.

### `get_revolver_health`

Structured health snapshot: active routing, per-bullet cooldowns, and recent event counts. Use for dashboards, monitors, or agent self-checks.

### `doctor_revolver`

Structured setup report: config path, cylinder/bullet counts, error policy, errors, and warnings. Keys never appear in the output.

---

## Hooks

Two hooks register automatically at plugin load.

### `on_session_start`

Logs the current delegation target at session start. Also delivers any pending recovery messages from the background thread — when cylinder 0 comes back online between sessions, this is how the agent finds out.

### `api_request_error`

The main event. Fires on every provider API error. Classifies by status code according to the active error policy and acts:

- **`advance`** (default: 401): Increments the consecutive-failure counter. At threshold: runs the probe if configured. Probe passes → stay (provider is alive, just a bad key). Probe fails or not configured → advance bullet.
- **`cooldown`** (default: 429): Marks the current bullet with a cooldown. Advances to the next available bullet. The cooled bullet becomes available again after N seconds.
- **`transient`** (default: 408, 502, 503): No rotation. Log the event and let Hermes' built-in retry handle it.
- **`unknown`**: Treated as transient.

After every rotation, the hook injects an explicit user-role message: `"delegation config changed — Use provider=X model=Y for the next delegated request."` The agent knows immediately. No guessing.

---

## Error handling

### Status code reference

| Code | Default action | What Revolver does |
|------|---------------|---------------------|
| 401 | `advance` | Counts failures. At threshold: probe → advance bullet (or cylinder if last bullet). |
| 429 | `cooldown` | Sets per-bullet cooldown. Advances to next available bullet. |
| 408 | `transient` | Stays put. Logs event. |
| 502 | `transient` | Stays put. Logs event. |
| 503 | `transient` | Stays put. Logs event. |
| other | `unknown` | Treated as transient. |

All of these are overridable via `error_policy` in `revolver.yaml`.

### Probe-based exhaustion

When a cylinder hits `consecutive_failures_threshold` consecutive 401s and has a `probe_url` set:

- **Probe passes (2xx/3xx):** Provider is alive — the specific key was rejected. Failure counter resets; stays on the same bullet.
- **Probe fails (timeout/4xx/5xx):** Provider is down. Advance to the next bullet (or cylinder).

---

## Auto-recovery

When every last cylinder is exhausted:

1. State becomes `ALL_EXHAUSTED`. Event is logged.
2. A **daemon background thread** starts. It probes cylinder 0's `probe_url` every 300 seconds (configurable via `recovery_check_interval_seconds` at the top level of `revolver.yaml`).
3. Probe succeeds → `recovery_success` is logged, a reset is queued.
4. On the next session start, `on_session_start` sees the queued message and injects: `★ Recovery SUCCESS — cylinder 0 is back online; reset complete.`

No `probe_url` on cylinder 0? The thread still runs but can't probe. You'll need to `/revolver reset` manually.

---

## Dual-process safety

Multiple Hermes processes can run concurrently without corrupting state:

- **File locking** via `flock` (POSIX) or `msvcrt.locking` (Windows) on `.revolver.lock`.
- **5-second acquisition timeout** — if another process holds the lock, the call backs off with a warning. State is not modified.
- **10-second stale lock detection** — if the lock file is older than 10s, the new process breaks it (assuming the holder crashed).
- **Atomic state writes** — state goes to `.revolver_state.json.tmp` first, then `os.replace()` swaps it in. A mid-write crash leaves the previous state intact.
- **Secure file creation** — `revolver.yaml` is written with `0o600` permissions. `/revolver doctor` warns if group/other bits are set.

---

## Observability

### Event log

Every rotation, cooldown, exhaustion, probe, and recovery is appended to `~/.hermes/.revolver_events.log` as structured JSON. The log self-trims at 10,000 lines (to 5,000). Use `/revolver log` to read the last 20, or tail/grep the raw file.

### `/revolver health`

Live operational snapshot: current state, active delegation, all active cooldowns with remaining seconds, and a count of recent events by type. Safe to call from a monitor or automation — no secrets in the output.

### `/revolver doctor`

Pre-flight check for your setup. Run this after changing `revolver.yaml`, after key rotation, or whenever something feels off. Catches:

- Missing or malformed config file
- Empty cylinders (will be silently skipped at runtime)
- Non-HTTP probe URLs
- `revolver.yaml` readable by group/other on POSIX systems
- `error_policy` conflicts (same status code in two lists)

---

## Self-test & CI

The plugin ships with a comprehensive hermetic self-test — no external services, no persistent files, no side effects:

```bash
# Run directly
python -m revolver

# Via pytest (after pip install -e ".[dev]")
pytest
```

The suite covers:

| # | What's tested |
|---|--------------|
| 1 | `revolver.yaml` loading and validation |
| 2 | State round-trip (save → load) |
| 2b | Bullet normalization (all formats, all error cases) |
| 3 | Error classification (all 7 default paths) |
| 3b | Configurable error policy (custom codes, duplicate detection) |
| 4 | Bullet wrap-around (infinite cycling) |
| 5 | Graph rendering |
| 6 | Active delegation lookup |
| 6b | `resolve_delegation` apply contract and secret redaction |
| 7 | Cooldown helpers (mark, check, clear) |
| 7b | `get_health_snapshot` structure and redaction |
| 7c | `doctor_revolver_config` findings |
| 8 | Cooldown-aware advance (skips unavailable bullets) |
| 9 | `ALL_COOLDOWN` when every bullet is cooling |
| 9b | Chain skip — exhausted cylinder jumps to next usable one |
| 9c | `ALL_EXHAUSTED` when last cylinder is exhausted |
| 10 | 429 triggers per-bullet cooldown + advance |
| 11 | 401 advances without setting cooldown |
| 12 | Graph cooldown indicators (●, ⊙, ○) |
| 13 | Hook injection: explicit routing, no key leakage |

CI runs the full matrix on every push:

```
ubuntu-latest  × Python 3.9, 3.12
windows-latest × Python 3.9, 3.12
```

---

## Persistence

Three files live under `~/.hermes/`:

| File | Purpose |
|------|---------|
| `revolver.yaml` | Cylinder definitions. Edit directly or via commands. Written with `0o600`. |
| `.revolver_state.json` | Runtime state: cylinder index, bullet index, state, cooldowns, failure counter. Updated atomically on every rotation. |
| `.revolver_events.log` | Append-only structured JSON event log. Max 10,000 lines, trims to 5,000. |
| `.revolver.lock` | Cross-process file lock. |

State survives restarts, reboots, and session switches. The tmpfile-then-rename write pattern ensures the state file is never in a half-written state after a crash.

---

## Troubleshooting

**"No cylinders configured"**
→ Create `~/.hermes/revolver.yaml` with at least one cylinder. Run `/revolver doctor` for a full setup check.

**"revolver.yaml not found"**
→ The file is mandatory. Copy `revolver.example.yaml` from the repo as a starting point.

**Plugin enabled but no `/revolver` commands**
→ Commands only appear in new sessions. Exit and restart Hermes, or run `/reset`.

**`/revolver doctor` reports warnings about empty cylinders**
→ Cylinders without bullets are silently skipped at runtime. Add bullets or remove the cylinder.

**`ALL_EXHAUSTED — no more cylinders`**
→ Every configured cylinder is exhausted. Options: wait for auto-recovery (if cylinder 0 has `probe_url`), run `/revolver reset`, or add more cylinders / bullets.

**"Lock contention — try again"**
→ Another Hermes process is holding the state lock. Wait a few seconds and retry. If persistent:
```bash
rm -f ~/.hermes/.revolver.lock
```

**`api_request_error` hook not firing**
→ The hook only fires on model inference calls made by the Hermes agent itself. It does not fire for `web_search`, `web_extract`, or similar tool calls. This is expected.

**Recovery thread not starting**
→ Recovery only starts in `ALL_EXHAUSTED` state. Check with `/revolver status`. Without a `probe_url` on cylinder 0, the thread runs but cannot probe — you'll need to `/revolver reset` manually.

**Self-test fails**
→ Ensure:
- `PyYAML` is installed (`pip install pyyaml`)
- `~/.hermes/revolver.yaml` exists and has a `cylinders:` key
- Each cylinder has `delegation.model` and `delegation.provider`
- All bullet keys are non-empty strings

---

## Uninstalling

```bash
hermes plugins disable revolver
hermes plugins remove revolver

# Optional: clean up state files
rm -f ~/.hermes/.revolver_state.json
rm -f ~/.hermes/.revolver_events.log
rm -f ~/.hermes/.revolver.lock
rm -f ~/.hermes/revolver.yaml   # ⚠ removes your cylinder config
```

---

## Development

### Repository layout

```
revolver-plugin/
├── README.md
├── PRODUCT.md                  ← product framing and design decisions
├── LICENSE
├── plugin.yaml                 ← Hermes plugin manifest
├── pyproject.toml              ← pip packaging + pytest config
├── revolver.example.yaml       ← example configuration
├── revolver/
│   ├── __init__.py             ← plugin entry point: hooks, commands, tools
│   ├── cylinder.py             ← state machine, persistence, locking, health
│   ├── bullets.py              ← bullet normalization, cooldowns, error policy
│   ├── cli.py                  ← revolver-selftest entry point
│   └── __main__.py             ← self-test runner
└── tests/
    └── test_selftest.py        ← pytest wrapper
```

### Module boundaries

- **`bullets.py`** — pure data helpers; no Hermes context, no state machine. Error policy, bullet normalization, cooldown helpers, and event log live here.
- **`cylinder.py`** — state machine and persistence. Depends only on `bullets.py`. Never import from `__init__.py` here.
- **`__init__.py`** — Hermes integration layer. `register(ctx)` is the entry point. All hooks, commands, and tools are wired here.

### Key invariants

1. **State mutations must go through `LockContext`** — direct writes to `.revolver_state.json` outside the lock will race with other processes.
2. **`normalize_bullet` accepts both `str` and `dict`** — the YAML format allows both, and CRUD commands generate both.
3. **`resolve_active_delegation` defaults to `include_secret=False`** — the safe path is the default. Never pass `True` to a tool handler.
4. **`classify_error` expects a pre-normalized policy dict** — call `normalize_error_policy()` once at load time, not per-request.

### Running tests

```bash
python -m revolver          # self-test directly
pytest                      # via pytest (requires pip install -e ".[dev]")
```

---

## License

MIT — see [LICENSE](LICENSE).
