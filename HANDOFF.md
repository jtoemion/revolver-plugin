# Revolver Plugin — Handoff Document

## What It Is

Revolver is a Hermes plugin that manages **delegation config rotation**. It sits between Hermes's main agent and the provider API layer, intercepting API errors and rotating through a chain of pre-configured provider+model+API-key combinations so the agent keeps running instead of hard-failing on a 401 or 429.

Think of it as a mechanical revolver cylinder: load N chambers (cylinders), each with 1+ bullets (API keys), and the plugin automatically advances to the next round when the current one misfires.

---

## How It Integrates With Hermes Config

### The Three Config Files

```
┌─────────────────────────────────────────────────────┐
│                  ~/.hermes/config.yaml               │
│                                                     │
│  model:                                             │
│    default: deepseek-v4-flash                        │
│    provider: opencode-go        ← MAIN SESSION      │
│                                                     │
│  custom_providers:               ← REQUIRED for     │
│    - name: nara                    custom endpoints │
│      base_url: https://.../v1                       │
│      key_env: NARA_API_KEY                          │
│                                                     │
│  falls back to openrouter/minimax at the            │
│  MAIN AGENT level (not delegation)                  │
└─────────────────────────────────────────────────────┘
         │
         │ model + provider (not touched by revolver)
         ▼
┌─────────────────────────────────────────────────────┐
│           Hermes Main Agent Session                  │
│  Uses: model.default / model.provider from config    │
│  Falls back: fallback_providers list                 │
│  Revolver does NOT rotate the MAIN AGENT's model.   │
└─────────────────────────────────────────────────────┘
         │
         │ delegate_task() spawns subagents with:
         │ {model, provider, api_key}
         ▼
┌─────────────────────────────────────────────────────┐
│  ~/.hermes/revolver.yaml  ← DECLARATIVE CONFIG      │
│                                                     │
│  cylinders:        ← ordered fallback chain         │
│    - delegation:                                     │
│        model: poolside/laguna-m.1:free               │
│        provider: openrouter                          │
│      bullets: [key1, key2]                           │
│    - delegation:                                     │
│        model: deepseek-v4-flash-free                 │
│        provider: opencode-zen                        │
│      bullets: [key1, key2, key3]                     │
│    - delegation:                                     │
│        model: mimo-v2.5-free                         │
│        provider: custom:nara                         │
│      bullets: [key1, key2]                           │
└─────────────────────────────────────────────────────┘
         │
         │ revolver.get_active_delegation()
         ▼
┌─────────────────────────────────────────────────────┐
│  ~/.hermes/.revolver_state.json  ← RUNTIME STATE     │
│                                                     │
│  {                                                   │
│    cylinder: 0,      ← which cylinder is active      │
│    bullet: 1,         ← which bullet is active       │
│    state: "CYLINDER_ACTIVE",                         │
│    cooldown_until: 0.0,                              │
│    bullet_cooldowns: {"0": 9999999999.0},            │
│    consecutive_failures: 0,                          │
│    recovery_thread_active: false                     │
│  }                                                   │
└─────────────────────────────────────────────────────┘
```

### The Delegation Contract

The revolver exposes ONE tool to the agent: `get_active_delegation()`.

When the orchestrator plugin or any code calls `delegate_task()`, it reads the delegation config from revolver:

```python
# Internal flow (pseudocode)
delegation = revolver.get_active_delegation()
# Returns: {"model": "poolside/...", "provider": "openrouter",
#           "cylinder": 0, "bullet": 1, "state": "CYLINDER_ACTIVE"}
# The orchestrator merges this into the subagent's model/provider
```

This is how subagents get their provider/model set dynamically. The main agent keeps using whatever model is configured in `config.yaml` — revolver only affects **delegation** (subagent spawning).

### Custom Providers

When revolver.yaml uses `provider: custom:nara`, Hermes needs a matching entry in `config.yaml`:

```yaml
# ~/.hermes/config.yaml
custom_providers:
  - name: nara
    base_url: https://router.bynara.id/v1
    key_env: NARA_API_KEY
    api_mode: chat_completions
```

The `custom:` prefix tells Hermes to look up the named custom provider definition. Without this, `custom:nara` won't resolve and the cylinder is unusable.

---

## Rotation Mechanics

### What Triggers a Rotation

The plugin hooks into `api_request_error` — a Hermes hook that fires when an LLM API call returns an error. NOT all HTTP requests — only model inference calls.

```
┌──────────┐     api_request_error(401, ...)
│ Hermes   │─────────────────────────────────▶┌──────────┐
│ Agent    │                                   │ Revolver │
│          │◀─────────────────────────────────│ Plugin   │
│          │  inject_message("[revolver]       └──────────┘
│          │   After 401 (advance):               │
│          │   cylinder=0 bullet=1                │
│          │   state=CYLINDER_ACTIVE")             │
└──────────┘                                      │
                                                  ▼
                                          ┌────────────────┐
                                          │ State mutation │
                                          │ with file lock │
                                          └────────────────┘
```

### Classification → Action

| HTTP Code | Classification | What Revolver Does |
|-----------|---------------|-------------------|
| **401** | `advance` | Increments consecutive failure counter. If counter >= threshold (default 2): runs health probe (if configured), then advances to next bullet. Resets failure counter. |
| **429** | `cooldown` | Sets per-bullet cooldown (from bullet's `cooldown_seconds` or cylinder default). Resets failure counter. Advances to next available bullet. |
| **408, 502, 503** | `transient` | Does NOT rotate. Logs and returns — agent's built-in retry handles it. |
| **others** | `unknown` | Treated as transient. No rotation. |

### The Advance Algorithm

```
advance(cylinders, state):
  1. Start at current position
  2. Find next bullet index (cycling forward, skipping cooldown bullets)
  3. If found → update state to new bullet, return ADVANCED
  4. If all bullets in cooldown → return ALL_COOLDOWN
     (caller decides: mark cylinder exhausted and advance to next cylinder)
```

Key detail: **advance wraps infinitely** within a cylinder. There's no exhaustion tracking at the advance() level — the caller (the hook in `__init__.py`) decides when to move to the next cylinder based on ALL_COOLDOWN or too many 401s.

### Cylinder Exhaustion

```
if advance() returns ALL_COOLDOWN:
    → mark cylinder as CYLINDER_EXHAUSTED
    → advance to next cylinder (index + 1)
    → if no more cylinders → state = ALL_EXHAUSTED → start recovery thread
```

### Full Rotation Sequence (Example)

```
Start: cylinder=0, bullet=-1 (before first use)

           advance()
              │
              ▼
Initial:  cylinder=0, bullet=0  ● active
  ↓ 401 × threshold (probe fails)
Next:    cylinder=0, bullet=1  ● active
  ↓ 429 (rate limit) — cooldown on bullet 1
Next:    cylinder=0, bullet=0  ● active (bullet 0 available, bullet 1 ⊙ cooldown)
  ↓ 401 × threshold
Next:    cylinder=1, bullet=0  ● active (cylinder 0 exhausted)
  ↓ 401 × threshold  
Next:    cylinder=2, bullet=0  ● active
  ↓ 401 × threshold
Next:    cylinder=3, bullet=0  ● active (minimax — last resort)
  ↓ 401
Next:    ALL_EXHAUSTED → recovery thread starts
         ↓ probe succeeds after 300s
         auto-reset at next session start
```

---

## State Persistence

### `.revolver_state.json` — Runtime Position

```json
{
  "cylinder": 0,
  "bullet": 1,
  "state": "CYLINDER_ACTIVE",
  "cooldown_until": 0.0,
  "bullet_cooldowns": {
    "0": 1751234567.890,
    "1": 0.0
  },
  "consecutive_failures": 2,
  "recovery_thread_active": false
}
```

Written atomically (tmp file → rename). Locked via `flock` for cross-process safety.

### `.revolver_events.log` — Audit Trail

Structured JSONL, one event per line:
```json
{"ts": 1751234567.890, "event": "bullet_advanced", "cylinder": 0, "bullet": 1, "model": "...", "provider": "...", "trigger": "401", "state": "CYLINDER_ACTIVE"}
```

Auto-trims to 5,000 lines when it exceeds 10,000.

### `.revolver.lock` — Cross-Process Safety

- 5-second acquisition timeout
- 10-second stale lock detection (breaks locks older than 10s)
- Uses POSIX `flock` — works across processes on the same host

---

## File Layout

```
~/.hermes/
├── config.yaml                 # Hermes config — model, providers, custom_providers
├── .env                        # API keys (including NARA_API_KEY for custom:nara)
├── revolver.yaml               # ← YOU MAINTAIN THIS — cylinder definitions
├── plugins/
│   └── revolver/               # ← Plugin source (installed via hermes plugins install)
│       ├── plugin.yaml
│       ├── __init__.py         # Entry point: hooks, commands, CRUD
│       ├── cylinder.py         # State machine, persistence, locking
│       ├── bullets.py          # Bullet normalization, error classification
│       └── __main__.py         # Self-test
├── .revolver_state.json        # ← AUTO-GENERATED — runtime state
├── .revolver_events.log        # ← AUTO-GENERATED — audit trail
└── .revolver.lock              # ← AUTO-GENERATED — lock file (ephemeral)
```

---

## Commands Reference

| Command | What it does |
|---------|-------------|
| `/revolver status` | Current cylinder/bullet/state + active delegation |
| `/revolver next` | Manually advance one bullet |
| `/revolver graph` | ASCII fallback chain with ● ⊙ ○ markers |
| `/revolver reset` | Reset to cylinder 0, bullet -1, CYLINDER_ACTIVE |
| `/revolver log` | Last 20 events as a table |
| `/revolver tool` | Delegation snapshot (model/provider) |
| `/revolver cylinder list\|add\|edit\|remove\|move` | CRUD for cylinders |
| `/revolver bullet list\|add\|edit\|remove` | CRUD for bullets within a cylinder |

---

## Important Constraints

1. **Main agent model is NOT rotated.** Revolver only affects `delegate_task()` subagent spawning. The main agent's model is whatever `config.yaml` says.

2. **`api_request_error` only fires for LLM inference calls.** Not web_search, web_extract, curl, or other HTTP calls the agent makes.

3. **Config changes in revolver.yaml need a new session** (`/reset` or restart) to take effect — they're loaded once in `register()`.

4. **Lock contention is non-fatal.** If another Hermes process holds the lock, the rotation is skipped and a warning is logged. You can always manually advance with `/revolver next`.

5. **The recovery thread is a daemon thread.** It doesn't block Hermes shutdown. If Hermes exits, the thread dies. Recovery state persists in `.revolver_state.json` so it resumes on restart.

---

## Key Files in the Repo

| File | Role |
|------|------|
| `revolver/__init__.py` (934 lines) | `register()` entry point, all hooks, all 8 commands, CRUD helpers, YAML serialization |
| `revolver/cylinder.py` (645 lines) | `CylinderDef`, `CylinderState`, `advance()`, `format_graph()`, `LockContext`, persistence, recovery thread |
| `revolver/bullets.py` (219 lines) | `normalize_bullet()`, cooldown helpers, `classify_error()`, `probe_provider()`, `log_event()` |
| `revolver/__main__.py` (243 lines) | 12-test self-test suite, no external test framework |

The `__init__.py` is the thickest file because it contains all the Hermes integration code — command registration, hook handlers, and the CRUD commands that manipulate revolver.yaml at runtime. The submodules (`cylinder.py`, `bullets.py`) are pure-python with no Hermes dependency and can be tested standalone.

---

## Deployed State (as of last config)

```
[0] openrouter     / poolside/laguna-m.1:free        2 bullets
[1] opencode-zen   / deepseek-v4-flash-free          3 bullets
[2] custom:nara    / mimo-v2.5-free                  2 bullets
[3] minimax        / MiniMax-M2.7                    1 bullet
```

Opencode-zen and nara have 3/2 real API keys each. Openrouter and minimax have placeholder/demo keys from the original template.
