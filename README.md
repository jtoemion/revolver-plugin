# Revolver Plugin — Cascading Fallback for Hermes Agent Delegation

The Revolver plugin gives Hermes a **self-healing delegation chain**. Think of it as a revolver cylinder: you load multiple "cylinders" (provider + model pairs), each with one or more "bullets" (API keys). When a request fails with an auth error (401) or rate-limit (429), the plugin automatically rotates to the next bullet or cylinder — your agent keeps working without you having to intervene.

```ascii
Cylinder 0: openrouter / poolside/laguna-m.1:free  ● bullet 0/2 active
  bullets: bearer ●, x-api-key ○
Cylinder 1: opencode-zen / deepseek-v4-flash-free  bullet -1/3 pending
  bullets: bearer, bearer, bearer
Cylinder 2: custom:nara / mimo-v2.5-free  bullet -1/2 pending
  bullets: bearer, bearer
Cylinder 3: minimax / MiniMax-M2.7  bullet -1/1 pending
  bullets: x-api-key
```

## Table of Contents

- [Architecture](#architecture)
  - [Core Concepts](#core-concepts)
  - [State Machine](#state-machine)
  - [Persistence](#persistence)
  - [Threading Model](#threading-model)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Cylinder Definitions](#cylinder-definitions)
  - [Custom Providers](#custom-providers)
  - [API Keys](#api-keys)
- [Usage](#usage)
  - [Commands](#commands)
  - [Tool Exposure](#tool-exposure)
  - [Hooks](#hooks)
- [Error Handling](#error-handling)
  - [Status Code Classification](#status-code-classification)
  - [Probe-based Exhaustion](#probe-based-exhaustion)
- [Auto-Recovery](#auto-recovery)
- [Dual-Process Safety](#dual-process-safety)
- [Self-Test](#self-test)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## Architecture

### Core Concepts

| Term | Description |
|------|-------------|
| **Cylinder** | One delegation target (provider + model) with a list of API keys. A cylinder has a cooldown timeout and optional health-check probe URL. |
| **Bullet** | A single API key within a cylinder. Each bullet has an auth type (bearer, x-api-key, custom), an optional per-bullet cooldown override, and an optional label. |
| **State** | The current position in the cylinder chain: which cylinder index, which bullet index, the overall state (CYLINDER_ACTIVE, CYLINDER_EXHAUSTED, ALL_EXHAUSTED), and per-bullet cooldown timestamps. Persisted to disk between sessions. |
| **Rotation** | Moving to the next available bullet in the current cylinder (skipping any that are in cooldown). When all bullets in a cylinder are exhausted or in cooldown, the plugin moves to the next cylinder. |

### State Machine

```
                    ┌──────────────┐
                    │  INSTALLED   │
                    │ (cyl 0, b -1)│
                    └──────┬───────┘
                           │ /revolver reset or first use
                           ▼
                    ┌──────────────┐
              ┌────▶│CYLINDER_ACTIVE│◀────────────┐
              │     │ (current cyl) │              │
              │     └──────┬───────┘              │
              │            │                      │
              │      ┌─────┴─────┐                │
              │      │           │                │
              │  401 error  429 error             │
              │   (advance)  (cooldown            │
              │              + advance)           │
              │      │           │                │
              │      ▼           ▼                │
              │  ┌──────────┐  ┌───────────┐     │
              │  │ADVANCED  │  │COOLDOWN   │     │
              │  │next bullet│  │set + next │     │
              │  └─────┬────┘  └─────┬─────┘     │
              │        │             │            │
              │        ▼             ▼            │
              │  last bullet?   all in cooldown?  │
              │        │             │            │
              │        ▼             ▼            │
              │  ┌────────────┐  ┌────────────┐  │
              │  │CYLINDER    │  │ALL_COOLDOWN│  │
              │  │EXHAUSTED   │  │(wait)      │  │
              │  └──────┬─────┘  └──────┬──────┘  │
              │         │               │          │
              │         ▼               ▼          │
              │  next cylinder     same cylinder   │
              │         │               │          │
              │         └───────┬───────┘          │
              │                 │                  │
              │         no more cylinders?         │
              │                 │                  │
              │                 ▼                  │
              │         ┌──────────────┐           │
              │         │ALL_EXHAUSTED │───────────┘
              │         │(auto-recover)│    recovery
              │         └──────────────┘    succeeds
              │
              └────────────────────────────────────┘
                     /revolver reset from anywhere
```

### Persistence

Three files live under `~/.hermes/`:

| File | Purpose |
|------|---------|
| `revolver.yaml` | Cylinder definitions — you edit this file (or use `/revolver cylinder`/`/revolver bullet` commands). |
| `.revolver_state.json` | Runtime state — current cylinder index, bullet index, state machine status, cooldown timestamps, consecutive failure counter. Auto-updated on every rotation. |
| `.revolver_events.log` | Append-only structured JSON event log — every rotation, cooldown, exhaustion, and recovery attempt is recorded with timestamp. Max 10,000 lines, auto-trimmed to 5,000. |
| `.revolver.lock` | File-based lock (flock) ensuring only one Hermes process modifies state at a time. 5-second timeout, 10-second stale lock break. |

State survives Hermes restarts, system reboots, and session switches. If you kill the process mid-rotation, the atomic write (write-to-tmp + rename) ensures the state file is never corrupted.

### Threading Model

When all cylinders are exhausted (`ALL_EXHAUSTED`), the plugin spawns a **daemon background thread** that periodically probes cylinder 0's health URL (if configured). If the probe succeeds, it issues a reset request that takes effect on the next Hermes session start via `inject_message`. The thread uses a threading.Event-based wait loop (not busy-poll) and cleans up on `/revolver reset`.

---

## Installation

### Prerequisites

- Hermes Agent (any recent version — tested on 2.x+)
- `PyYAML` (usually pre-installed with Hermes; if missing: `pip install pyyaml`)

### Install via `hermes plugins install` (recommended)

```bash
# One-liner — clones the repo and prompts to enable
hermes plugins install jtoemion/revolver-plugin

# Or skip the prompt and enable immediately
hermes plugins install jtoemion/revolver-plugin --enable
```

### Manual installation

```bash
# Clone anywhere
git clone https://github.com/jtoemion/revolver-plugin.git

# Symlink into Hermes plugins dir
ln -sf "$(pwd)/revolver-plugin" ~/.hermes/plugins/revolver

# Or copy directly
cp -r revolver-plugin ~/.hermes/plugins/revolver

# Enable the plugin
hermes plugins enable revolver
```

### Verify installation

```bash
hermes plugins list | grep revolver
# → revolver  enabled  1.0.0  Cascading fallback plugin …  user

# Run the self-test
python3 ~/.hermes/plugins/revolver/__main__.py
# → === revolver self-test ===
# → 1. Loading revolver.yaml ...
# →    OK — N cylinder(s) loaded
# → ...
# → === all tests passed ===
```

---

## Configuration

### Cylinder Definitions

Create `~/.hermes/revolver.yaml` (the self-test above fails with a clear message if this file is missing). The `/revolver cylinder` and `/revolver bullet` commands can also manage this file at runtime.

A minimal config with one cylinder:

```yaml
cylinders:
  - delegation:
      model: my-model-name
      provider: my-provider
    bullets:
      - sk-...my-api-key
```

Full reference:

```yaml
cylinders:
  # ── Cylinder 0 ──────────────────────────────────────────────────────────
  - delegation:
      model: poolside/laguna-m.1:free
      provider: openrouter
    bullets:
      - sk-or-...key1                          # plain string → bearer type
      - key: sk-or-...key2                     # dict with explicit config
        type: x-api-key
        cooldown_seconds: 30
        label: my-openrouter-key               # optional label for graph display
    cooldown_seconds: 60                       # default cooldown for this cylinder
    consecutive_failures_threshold: 2          # 401s before advancing (default: 2)
    probe_url: https://openrouter.ai/api/v1/auth/key  # health check URL

  # ── Cylinder 1: empty (skipped) ─────────────────────────────────────────
  - delegation:
      model: deepseek-v4-flash-free
      provider: opencode-zen
    bullets: []                                # no keys → this cylinder is skipped

  # ── Cylinder 2: custom provider ─────────────────────────────────────────
  - delegation:
      model: mimo-v2.5-free
      provider: custom:nara
    bullets:
      - sk-nry-...key1
      - sk-nry-...key2
```

### Cylinder-level fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `delegation.model` | **yes** | — | Model identifier string (e.g. `poolside/laguna-m.1:free`) |
| `delegation.provider` | **yes** | — | Provider name (e.g. `openrouter`, `custom:nara`, `minimax`) |
| `bullets` | **yes** | — | List of API key entries (strings or dicts) |
| `cooldown_seconds` | no | `60` | Default cooldown applied on 429 for bullets without their own override |
| `consecutive_failures_threshold` | no | `2` | Number of consecutive 401s before advancing to the next bullet |
| `probe_url` | no | `None` | Health-check URL. The plugin performs a HEAD request before marking this cylinder exhausted. If the probe passes, the failure counter resets and the plugin retries the same bullet. |

### Bullet formats

**Plain string** — fastest config:

```yaml
bullets:
  - sk-or-...aaaa
  - sk-or-...bbbb
```

**Dict** — when you need to set auth type, per-bullet cooldown, or a label:

```yaml
bullets:
  - key: sk-or-...cccc
    type: x-api-key
    cooldown_seconds: 30
    label: backup-router-key
```

| Bullet field | Required | Default | Description |
|-------------|----------|---------|-------------|
| `key` | **yes** | — | The API key string (must be non-empty) |
| `type` | no | `"bearer"` | One of `bearer`, `x-api-key`, `custom` |
| `cooldown_seconds` | no | cylinder's `cooldown_seconds` | Override for this specific key on 429 |
| `label` | no | `None` | Human-readable label shown in `/revolver graph` |

### Custom Providers

If you use a provider that isn't built into Hermes (like a custom OpenAI-compatible endpoint), you need two things:

**1. Add the provider definition to `~/.hermes/config.yaml`:**

```yaml
custom_providers:
  - name: nara
    base_url: https://router.example.com/v1
    key_env: NARA_API_KEY
    api_mode: chat_completions
```

**2. Set the API key in `~/.hermes/.env`:**

```bash
echo 'NARA_API_KEY=sk-nry-...your-key-here' >> ~/.hermes/.env
```

**3. Reference it in revolver.yaml as `custom:<name>`:**

```yaml
  - delegation:
      model: mimo-v2.5-free
      provider: custom:nara
```

### API Keys

API keys go **only** in the `bullets` list of `revolver.yaml`. They are NOT stored in `config.yaml` or `.env` (unless you use a custom provider, where the key_env must point to an env var).

---

## Usage

The Revolver plugin is passive by design — it sits in the background and watches for `api_request_error` events. But it exposes several slash commands and one tool for inspection and manual control.

### Commands

All commands are available as slash commands in any Hermes session.

#### `/revolver status`

Print the current position and delegation target.

```
[revolver] cylinder=0 bullet=1 state=CYLINDER_ACTIVE -> openrouter/poolside/laguna-m.1:free api_key=sk-or-... key_info
```

If a cooldown is active, it shows remaining seconds. If `ALL_EXHAUSTED`, it tells you to `/revolver reset`.

#### `/revolver next`

Manually advance one bullet (or cylinder if the current cylinder is exhausted).

```
[revolver] next — cylinder=0 bullet=1/2 key=sk-or-... state=CYLINDER_ACTIVE
```

Use this after you've fixed a credential issue without waiting for an API error. Also useful for testing.

#### `/revolver graph`

Render the full chain as ASCII art with the current position marked with a filled circle (●).

```
Cylinder 0: openrouter / poolside/laguna-m.1:free  ● bullet 0/2 active  cf=1
  bullets: bearer ●, x-api-key ○
Cylinder 1: opencode-zen / deepseek-v4-flash-free  bullet -1/3 pending
  bullets: bearer, bearer, bearer
Cylinder 2: custom:nara / mimo-v2.5-free  bullet -1/2 pending
  bullets: bearer, bearer
Cylinder 3: minimax / MiniMax-M2.7  bullet -1/1 pending
  bullets: x-api-key
```

Bullet indicators:
- **●** = active (current position)
- **⊙** = in cooldown (timer running, not available yet)
- **○** = exhausted (already used), or pending (ahead)

#### `/revolver reset`

Reset the state machine to cylinder 0, bullet -1, `CYLINDER_ACTIVE`. Stops any running recovery thread and clears all bullet cooldowns.

```
[revolver] Reset — cylinder=0 bullet=-1 state=CYLINDER_ACTIVE
```

This is the "pull the trigger on a fresh chamber" command. Use it when you've rotated API keys or a provider has come back online.

#### `/revolver log`

Show the last 20 rotation events as a table:

```
TIME                 EVENT                    CYL  BULL  PROVIDER
─────────────────────────────────────────────────────────────────
2026-06-29 18:22:21  bullet_advanced            0    1   openrouter
2026-06-29 18:23:45  cooldown_set               0    1   openrouter
2026-06-29 18:25:10  cylinder_exhausted          0    0   openrouter
2026-06-29 18:25:10  bullet_advanced            1    0   opencode-zen
```

#### `/revolver tool`

Human-readable delegation snapshot:

```
[revolver] Active delegation:
  cylinder : 0
  bullet   : 1
  state    : CYLINDER_ACTIVE
  provider : openrouter
  model    : poolside/laguna-m.1:free
```

#### `/revolver cylinder`

CRUD operations on cylinder definitions. Modifies `~/.hermes/revolver.yaml` on the fly.

```
# List all cylinders
/revolver cylinder list

# Add a new cylinder at the end
/revolver cylinder add <model> <provider> [--cooldown N] [--probe-url URL]

# Edit an existing cylinder
/revolver cylinder edit <idx> [--model m] [--provider p] [--cooldown N] [--threshold N] [--probe-url URL]

# Remove a cylinder (cannot remove the last one)
/revolver cylinder remove <idx>

# Move a cylinder to a different position
/revolver cylinder move <src-idx> <dst-idx>
```

Examples:

```
/revolver cylinder add deepseek-v4-flash-free opencode-zen --cooldown 60
→ [revolver] Added cylinder [3] opencode-zen/deepseek-v4-flash-free

/revolver cylinder move 3 1
→ [revolver] Moved cylinder [3] → [1]
```

#### `/revolver bullet`

CRUD operations on bullets within a cylinder.

```
# List bullets in a cylinder
/revolver bullet list <cylinder-idx>

# Add a bullet
/revolver bullet add <cylinder-idx> <key> [--type bearer|x-api-key] [--cooldown N]

# Edit a bullet
/revolver bullet edit <cylinder-idx> <bullet-idx> [--key k] [--type t] [--cooldown N]

# Remove a bullet
/revolver bullet remove <cylinder-idx> <bullet-idx>
```

Examples:

```
/revolver bullet add 2 sk-my-new-key --type x-api-key --cooldown 30
→ [revolver] Added bullet [2] sk-my-new... to cylinder [2]

/revolver bullet remove 2 1
→ [revolver] Removed bullet [1] from cylinder [2]
```

### Tool Exposure

The plugin exposes one tool to the Hermes agent itself (not the user):

**`get_active_delegation`** — Returns a JSON dict: `{model, provider, cylinder, bullet, state}`. The host agent can call this to dynamically route delegation config to subagents. It's how the orchestrator plugin picks the right model/provider for spawned workers.

Example return:

```json
{"model": "poolside/laguna-m.1:free", "provider": "openrouter",
 "cylinder": 0, "bullet": 1, "state": "CYLINDER_ACTIVE"}
```

### Hooks

Two hooks register automatically when the plugin loads:

#### `on_session_start`

Fires when a Hermes session starts. Logs the current cylinder, bullet, and active delegation target. Also checks for pending recovery messages from the background recovery thread and injects them into the conversation.

#### `api_request_error`

Fires when the Hermes agent receives an API error from any provider. Classifies the error by status code and:
- **401 (auth failure):** Increments consecutive failure counter. If counter reaches `consecutive_failures_threshold` (default 2), it runs the health probe (if configured). If probe fails, advances to the next bullet/cylinder. If probe passes, resets failure counter and stays.
- **429 (rate-limit):** Sets a per-bullet cooldown, resets failure counter, and advances to the next available bullet.
- **408/502/503 (transient):** Does NOT rotate. Logs the event and returns — the agent's built-in retry handles it.
- **Any other code:** Treated as transient (no rotation).

After each mutation, the hook injects a user-role message into the session so the agent is aware of the rotation.

---

## Error Handling

### Status Code Classification

| Code | Action | What happens |
|------|--------|-------------|
| 401 | `advance` | Increments failure counter. At threshold: probes if configured, then advances bullet. Immediate rotation — never retry the same key that just failed auth. |
| 429 | `cooldown` | Sets a cooldown on that specific bullet (per-bullet override or cylinder default). Advances to next available bullet. The cooldown bullet becomes available again after N seconds. |
| 408 | `transient` | No rotation. The provider may be overloaded; retry in place. |
| 502 | `transient` | No rotation. Upstream gateway error; retry in place. |
| 503 | `transient` | No rotation. Service unavailable; retry in place. |
| others | `unknown` | Treated as transient; no rotation. |

### Probe-based Exhaustion

When a cylinder hits `consecutive_failures_threshold` consecutive 401 errors and has a `probe_url` configured, the plugin runs a HEAD health check before aborting that bullet:

- **Probe passes (2xx/3xx):** The failure counter resets and the plugin stays on the same bullet. The provider is online but the specific credential was rejected — retrying the same key won't help, but the decision to advance is left to the consecutive_failures_threshold.
- **Probe fails (timeout/4xx/5xx):** The bullet is exhausted and the plugin advances.

---

## Auto-Recovery

When ALL cylinders are exhausted, the Revolver plugin:

1. Sets state to `ALL_EXHAUSTED` and logs the event.
2. Starts a **daemon background thread** that periodically (default 300s / 5 minutes; configurable via `recovery_check_interval_seconds` in `revolver.yaml` at the top level) probes cylinder 0's `probe_url`.
3. If the probe succeeds → logs `recovery_success`, schedules a reset via `_request_reset()`.
4. On the next Hermes session, the `on_session_start` hook sees the pending recovery message and injects it as a user message: `★ Recovery SUCCESS — cylinder 0 is back online; reset complete`.

If cylinder 0 has no `probe_url`, the recovery thread still runs but logs that it's waiting without probing — you'll need to `/revolver reset` manually.

---

## Dual-Process Safety

The Revolver uses file-based locking (`flock` on `.revolver.lock`) to prevent two Hermes processes from corrupting state simultaneously:

- **5-second acquisition timeout** — if another process holds the lock longer than this, the call returns with a "lock contention" warning. The state is not modified.
- **10-second stale lock detection** — if a lock file is older than 10s, the new process breaks it, assuming the holder crashed.
- **Atomic writes** — state is written to `.revolver_state.json.tmp` first, then renamed over the real file. A crash during write leaves the previous state intact.

---

## Self-Test

The plugin ships with a comprehensive 12-test self-test suite:

```bash
python3 ~/.hermes/plugins/revolver/__main__.py
```

Tests cover:
1. revolver.yaml loading and validation
2. State round-trip (save → load)
3. Bullet normalization (all formats, error cases)
4. Bullet type and key accessors
5. Error classification (all 7 code paths)
6. Bullet wrap-around (infinite cycling)
7. Graph rendering (with/without cooldowns, exhaustion)
8. Active delegation lookup
9. Cooldown helpers (mark, check, clear)
10. Cooldown-aware advance (skips unavailable bullets)
11. 429 cooldown trigger
12. 401 immediate advance
13. Graph cooldown indicators (●, ⊙, ○)

The self-test uses a temporary state file and cleans up after itself.

---

## Uninstalling

```bash
# Disable the plugin
hermes plugins disable revolver

# Uninstall
hermes plugins remove revolver

# Or if manually installed:
rm -rf ~/.hermes/plugins/revolver

# Optional: remove state and config files
rm -f ~/.hermes/.revolver_state.json
rm -f ~/.hermes/.revolver_events.log
rm -f ~/.hermes/.revolver.lock
rm -f ~/.hermes/revolver.yaml          # ⚠️ this removes your cylinder config
```

---

## Troubleshooting

### "No cylinders configured"
→ Create `~/.hermes/revolver.yaml` with at least one cylinder. Copy from `revolver.example.yaml`.

### "revolver.yaml not found at /home/user/.hermes/revolver.yaml"
→ Same fix as above. The file is mandatory.

### Plugin doesn't load / "not enabled" shown by default
→ Run `hermes plugins enable revolver`. Git-installed plugins are NOT auto-enabled by default (use `--enable` during install to skip this step).

### Plugin enabled but no `/revolver` commands
→ Commands only appear in new sessions. Start a fresh session (`exit` then `hermes` again, or `/reset` in CLI).

### Self-test fails
→ Check that:
  - `~/.hermes/revolver.yaml` exists and is valid YAML
  - `PyYAML` is installed (`pip install pyyaml`)
  - The file has a `cylinders:` key at the top level
  - Each cylinder has `delegation.model` and `delegation.provider`
  - Bullet keys are non-empty strings

### "ALL_EXHAUSTED — no more cylinders"
→ You've exhausted all configured cylinders without recovery. Either:
  - Wait for auto-recovery (if cylinder 0 has a `probe_url` configured)
  - Run `/revolver reset` to start fresh
  - Add more cylinders or API keys

### "Lock contention — try again"
→ Another Hermes process is currently modifying state. Try again in a few seconds. If persistent, check for stale `.revolver.lock` files:
```bash
rm -f ~/.hermes/.revolver.lock
```

### Cooldowns not being respected
→ Verify that `cooldown_seconds` is set on the cylinder or individual bullets. The default is 60s if unset.

### `api_request_error` hook not firing
→ The hook only fires when the Hermes agent itself makes API calls (model inference). It does NOT fire for `web_search`, `web_extract`, or other non-inference API calls. This is expected.

### Recovery thread not starting
→ Recovery only starts when ALL cylinders are exhausted (`ALL_EXHAUSTED` state). Check state with `/revolver status`. Also verify cylinder 0 has a `probe_url` — without it, the thread runs but can't probe.

---

## Development

### Repository structure

```
revolver-plugin/
├── README.md                   ← this file
├── LICENSE                     ← MIT
├── revolver.example.yaml       ← example configuration
├── plugin.yaml                 ← Hermes plugin manifest
└── revolver/
    ├── __init__.py             ← Plugin entry point, hooks, commands, CRUD
    ├── cylinder.py             ← CylinderDef, state machine, persistence, locking
    ├── bullets.py              ← Bullet normalization, cooldowns, error classification
    └── __main__.py             ← Self-test runner
```

### Adding features

The plugin follows a modular architecture:
- **`bullets.py`** — Pure data helpers. No dependency on Hermes context or state machine. Add new error classifications, bullet types, or probe logic here.
- **`cylinder.py`** — State machine + persistence. Add new state transitions, graph formats, or recovery strategies here. Depends only on `bullets.py`.
- **`__init__.py`** — Hermes integration layer. Add new hooks, commands, or tools here. The `register(ctx)` function at line 56 is the entry point.

### Code invariants

1. **Never import from `__init__.py` inside `cylinder.py` or `bullets.py`** — the submodules must be independently testable.
2. **State mutations must go through the file lock** (`LockContext` in `cylinder.py`). Direct writes to `.revolver_state.json` outside the lock will race.
3. **`normalize_bullet` must accept both string and dict** — the YAML format allows both, and the CRUD commands generate both.

### Running tests

```bash
# From the plugin directory or anywhere
python3 ~/.hermes/plugins/revolver/__main__.py

# Or from the repo root
python3 -m revolver
```

The self-test is a single file with no test framework dependency. It exits with code 0 on pass, 1 on failure.

---

## License

MIT — see [LICENSE](LICENSE).
