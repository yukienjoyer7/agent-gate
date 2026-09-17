# Upstream provenance

Source: https://github.com/NafisNaufal/agentgate
Revision: 9b919c604d2a0955fff26b4f05045281514c0220 (2026-09-14)
Version: 0.3.0
License declared by upstream's pyproject.toml: MIT.
The source revision contains no separate LICENSE file.

Imported components: decision.py, schemas.py, risk.py, sanitizer.py,
detectors/, and policy/ (including all four JSON policy packs).
These files are kept verbatim to make upstream updates reviewable.

Integration-specific files: __init__.py exports only the embedded core;
audit.py requires an injected host audit store instead of creating a separate
PostgreSQL connection. The host adapter supplies durable, sanitized evaluation
records before a decision can reach an executor.

Upstream's planner, CLI, router, executors, scenarios, and benchmark commands
are not included. The application retains its own implementations and the
agentgate console entry point. No separately installed agentgate distribution
is required or supported as a dependency (both projects share that name).
