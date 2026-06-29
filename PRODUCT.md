# Revolver Product Brief

Revolver is a reliability layer for Hermes agent delegation. It keeps long-running
agent work moving by routing around failed, rate-limited, or exhausted provider
credentials.

## Product Promise

When a model provider or key fails, Revolver selects the next usable delegation
target and exposes a clear routing contract that the host can apply.

## Core Surfaces

- `resolve_delegation`: control-plane API for provider/model routing.
- `get_revolver_health`: operational snapshot for agents, monitors, and dashboards.
- `doctor_revolver`: setup and safety validation.
- `/revolver graph`, `/revolver health`, `/revolver doctor`: human inspection.

## Product Principles

- Routing state must be enforceable through tools, not only explained in chat.
- Secrets must never appear in logs, status commands, or tool responses.
- Local config should be easy to validate before an agent depends on it.
- Failure behavior should be policy-driven and predictable.
- Tests must run without touching real `~/.hermes` config or keys.

## Next Product Milestones

- Add a first-class Hermes before-request adapter that applies `resolve_delegation`.
- Add encrypted or OS-keychain-backed key storage.
- Add richer policy rules for cost caps, paid-provider approval, and weighted routing.
- Add a local dashboard backed by `get_revolver_health`.
- Publish versioned releases with migration notes.
